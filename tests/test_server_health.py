"""
tests/test_server_health.py — the Server page's readings and verdicts (pure, no database).

The parsers are fed text copied from beverly-pi; checks() and resilience() are
fed snapshots built by snap() below, one fault at a time.
"""
import copy

from app import server_health as sh

PROC_STAT = """cpu  4705 356 584 3699 23 23 0 0 0 0
cpu0 1393280 32966 572056 18719810 6200 0 2160 0 0 0
intr 114930548 113199788 3 0 5 263 0 0 0
"""

MEMINFO = """MemTotal:        3886900 kB
MemFree:          484300 kB
MemAvailable:    2245000 kB
Buffers:           61000 kB
Cached:          1950000 kB
SwapTotal:       2097148 kB
SwapFree:        2092000 kB
"""

NET_DEV = """Inter-|   Receive                                                |  Transmit
 face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed
    lo: 328807128859 90829644    0    0    0     0          0         0 328807128859 90829644    0    0    0     0       0          0
  eth0:       0       0    0    0    0     0          0         0        0       0    0    0    0     0       0          0
 wlan0: 71435356630 135565339    0 71831    0     0          0  26078572 15647112335 101848575    0    0    0     0       0          0
"""

BLOCK_STAT = "  637686    16804 11533658   829747  3085492  1185406 186274098 66947756        0  9155988 67786485      340      121 47590026     8982        0        0"

TUNNEL_YML = """tunnel: 97f95f60-3dee-4861-aaee-70d7cb5f8afb
credentials-file: /etc/cloudflared/97f95f60.json

ingress:
  - hostname: beverly.echothermcontrols.com
    service: http://localhost:8000
  - hostname: fleet.echothermcontrols.com   # the fleet portal
    service: http://localhost:8090
  - service: http_status:404
"""

METRICS = """# HELP cloudflared_tunnel_ha_connections Number of active ha connections
cloudflared_tunnel_ha_connections 4
cloudflared_tunnel_request_errors 37
cloudflared_tunnel_response_by_code{status_code="200"} 22868
cloudflared_tunnel_response_by_code{status_code="302"} 61
"""

SS = """LISTEN 0      4096   127.0.0.1:20241      0.0.0.0:*
LISTEN 0      4096   127.0.0.1:20243      0.0.0.0:*    users:(("cloudflared",pid=1408945,fd=8))
LISTEN 0      2048   127.0.0.1:8090       0.0.0.0:*    users:(("gunicorn",pid=1417823,fd=3),("gunicorn",pid=1449,fd=3))
"""

SHOW = """Id=fleet-portal.service
ActiveState=active
NRestarts=0

Id=beverly.service
ActiveState=failed
NRestarts=3
"""


def test_parsers_read_what_the_pi_prints():
    cpu = sh.parse_cpu(PROC_STAT)
    assert cpu == {"busy": 4705 + 356 + 584 + 23 + 0 + 0, "total": 4705 + 356 + 584 + 3699 + 23 + 23, "iowait": 23}
    later = {"busy": cpu["busy"] + 30, "total": cpu["total"] + 100, "iowait": cpu["iowait"] + 5}
    assert sh.cpu_pct(later, cpu) == (30.0, 5.0)
    assert sh.cpu_pct(cpu, None) == (None, None)

    mi = sh.parse_meminfo(MEMINFO)
    assert mi["MemTotal"] == 3886900 * 1024 and mi["MemAvailable"] == 2245000 * 1024

    nd = sh.parse_net_dev(NET_DEV)
    assert nd["wlan0"]["rx"] == 71435356630 and nd["wlan0"]["rx_drop"] == 71831 and nd["wlan0"]["tx"] == 15647112335
    assert nd["eth0"]["rx"] == 0

    assert sh.parse_blockstat(BLOCK_STAT) == {"read": 11533658 * 512, "written": 186274098 * 512}
    assert sh.parse_blockstat("") is None


def test_throttle_bits_say_now_and_since_boot():
    assert sh.decode_throttled(0) == {"now": [], "since_boot": []}
    d = sh.decode_throttled(0x50005)        # under-voltage now + throttled now; both also since boot
    assert d["now"] == ["under-voltage (power supply too weak)", "CPU throttled"]
    assert d["since_boot"] == ["under-voltage (power supply too weak)", "CPU throttled"]
    assert sh.decode_throttled(0x10000) == {"now": [], "since_boot": ["under-voltage (power supply too weak)"]}


def test_tunnel_config_metrics_and_ports():
    cfg = sh.parse_tunnel_config(TUNNEL_YML)
    assert cfg["tunnel"] == "97f95f60-3dee-4861-aaee-70d7cb5f8afb"
    assert cfg["routes"] == [("beverly.echothermcontrols.com", "http://localhost:8000"),
                             ("fleet.echothermcontrols.com", "http://localhost:8090")]
    m = sh.parse_tunnel_metrics(METRICS)
    assert m == {"requests": 22868 + 61 + 37, "errors": 37, "connections": 4}
    assert sh.parse_tunnel_metrics("") == {"requests": None, "errors": None, "connections": None}
    ss = sh.parse_ss_listen(SS)
    assert ("127.0.0.1", 20241, None, None) in ss and ("127.0.0.1", 20243, 1408945, "cloudflared") in ss
    units = sh.parse_systemctl_show(SHOW)
    assert [u["Id"] for u in units] == ["fleet-portal.service", "beverly.service"]
    assert units[1]["ActiveState"] == "failed"


def test_memory_per_service_counts_shared_memory_once():
    procs = [{"unit": "postgresql@17-main.service", "own": 10, "shared": 500},
             {"unit": "postgresql@17-main.service", "own": 20, "shared": 480},
             {"unit": "fleet-portal.service", "own": 300, "shared": 0},
             {"unit": None, "own": 999, "shared": 0}]
    assert sh.unit_memory(procs) == {"postgresql@17-main.service": 530, "fleet-portal.service": 300}


def test_words_for_sizes_and_times():
    assert sh.size(None) == "—" and sh.size(512) == "512 B" and sh.size(1536) == "1.5 KB"
    assert sh.size(30164570112) == "28.1 GB"
    assert sh.rate(2048) == "2.0 KB/s"
    assert sh.ago(30) == "30 s" and sh.ago(600) == "10 min" and sh.ago(3 * 3600 + 120) == "3 h 2 min"
    assert sh.ago(20 * 86400 + 5 * 3600) == "20 d 5 h"


GB = 2 ** 30


def snap(**over):
    """A healthy snapshot shaped like collect()'s, then the overrides."""
    s = {
        "host": "beverly-pi", "t": 1_800_000_000.0, "interval_s": 60,
        "system": {"host": "beverly-pi", "cores": 4, "load": [0.5, 0.6, 0.6], "uptime_s": 86400 * 20,
                   "clock_synced": True, "reboot_required": False},
        "cpu": {"pct": 12.0, "iowait_pct": 1.0},
        "heat": {"temp_c": 47.0, "throttled": 0, "now": [], "since_boot": []},
        "memory": {"total": 4 * GB, "available": 2 * GB, "used": 2 * GB, "used_pct": 50.0,
                   "swap_total": 2 * GB, "swap_used": 0, "swap_pct": 0.0},
        "storage": {"mounts": [], "root": {"mount": "/", "used_pct": 35.0, "free": 18 * GB, "device": "/dev/mmcblk0p2"},
                    "boot_kind": "SD card / eMMC", "boot_disk": "mmcblk0",
                    "disks": {"mmcblk0": {"write_bps": 1000.0, "written_per_day": 4.4 * GB}}},
        "network": {"ifaces": [{"name": "eth0", "kind": "Ethernet", "state": "down", "default": False, "rx_bps": 0.0, "tx_bps": 0.0},
                               {"name": "wlan0", "kind": "Wi-Fi", "state": "up", "default": True, "rx_bps": 2048.0, "tx_bps": 1024.0},
                               {"name": "tailscale0", "kind": "VPN (Tailscale)", "state": "unknown", "default": False,
                                "rx_bps": 9e6, "tx_bps": 9e6}],
                    "wifi": {"ssid": "ASUS_B", "signal": 100}},
        "services": [{"unit": "fleet-portal.service", "label": "Fleet portal", "state": "active", "restarts": 0}],
        "tunnels": [{"name": "config.yml", "port": 20241, "ready": 4, "requests": 1000.0, "req_per_min": 12.0}],
        "websites": [{"hostname": "fleet.echothermcontrols.com", "ok": True, "code": 302, "ms": 9, "error": None}],
        "backups": [{"label": "Fleet portal database", "folder": "~/fleet-portal-backups", "result": "success",
                     "age_s": 6 * 3600, "offsite": {"at": 1, "age_s": 6 * 3600}},
                    {"label": "Violations portal database", "folder": "~/beverly-backups", "result": "success",
                     "age_s": 6 * 3600, "offsite": None}],
        "database": {"connections": 12, "max_connections": 100, "standbys": 0},
    }
    for path, v in over.items():
        node = s
        keys = path.split("__")
        for k in keys[:-1]:
            node = node[k]
        node[keys[-1]] = v
    return copy.deepcopy(s)


def texts(issues, level=None):
    return " | ".join(i["text"] for i in issues if level is None or i["level"] == level)


def test_a_healthy_server_has_nothing_to_report():
    assert sh.checks(snap()) == []
    assert sh.level(sh.checks(snap())) == "ok"


def test_each_fault_is_named_at_the_right_level():
    assert "90% full" in texts(sh.checks(snap(storage__root={"used_pct": 90.0, "free": 3 * GB})), "crit")
    assert "82% full" in texts(sh.checks(snap(storage__root={"used_pct": 82.0, "free": 5 * GB})), "warn")
    assert "programs will start being killed" in texts(sh.checks(snap(memory__available=GB // 10)), "crit")
    assert "81 °C" in texts(sh.checks(snap(heat__temp_c=81.0)), "crit")
    uv = sh.checks(snap(heat__now=["under-voltage (power supply too weak)"],
                        heat__since_boot=["under-voltage (power supply too weak)"]))
    assert "Right now: under-voltage" in texts(uv, "crit") and "Since the last restart" not in texts(uv)
    past = sh.checks(snap(heat__since_boot=["under-voltage (power supply too weak)"]))
    assert "power supply dipped" in texts(past, "warn")
    down = sh.checks(snap(services=[{"unit": "beverly.service", "label": "Violations portal", "state": "failed", "restarts": 0}]))
    assert "Violations portal is failed" in texts(down, "crit")
    site = sh.checks(snap(websites=[{"hostname": "beverly.echothermcontrols.com", "ok": False, "code": None,
                                     "ms": 4000, "error": "timed out"}]))
    assert "beverly.echothermcontrols.com did not answer (timed out)" in texts(site, "crit")
    assert "answered 502" in texts(sh.checks(snap(websites=[{"hostname": "x", "ok": False, "code": 502, "ms": 5, "error": None}])), "crit")
    assert "no connection to Cloudflare" in texts(sh.checks(snap(tunnels=[{"name": "t", "port": 1, "ready": 0}])), "crit")


def test_backups_that_are_late_failed_or_missing():
    late = snap()
    late["backups"][0]["age_s"] = 40 * 3600
    assert "the newest backup is 40 h old" in texts(sh.checks(late), "crit")
    failed = snap()
    failed["backups"][1]["result"] = "exit-code"
    assert "Violations portal database: the last nightly backup FAILED" in texts(sh.checks(failed), "crit")
    gone = snap()
    gone["backups"][0]["age_s"] = None
    assert "no backup file found" in texts(sh.checks(gone), "crit")
    stale_off = snap()
    stale_off["backups"][0]["offsite"]["age_s"] = 50 * 3600
    assert "off-site copy is 2 d 2 h old" in texts(sh.checks(stale_off), "warn")


def test_problems_come_worst_first():
    issues = sh.checks(snap(heat__temp_c=72.0, storage__root={"used_pct": 95.0, "free": GB},
                            system__reboot_required=True))
    assert [i["level"] for i in issues] == ["crit", "warn", "info"]
    assert sh.level(issues) == "crit"


def test_what_would_take_everything_down():
    r = {x["what"]: x for x in sh.resilience(snap(), 1)}
    assert not r["Servers"]["ok"] and "every one of them goes down together" in r["Servers"]["detail"]
    assert not r["Main disk"]["ok"] and "4.4 GB written a day" in r["Main disk"]["detail"]
    assert not r["Network"]["ok"] and "Wi-Fi only (ASUS_B)" in r["Network"]["detail"]
    assert not r["Database copy"]["ok"]
    assert r["Backup: Fleet portal database"]["ok"]
    assert not r["Backup: Violations portal database"]["ok"]
    assert "ONLY on this same disk" in r["Backup: Violations portal database"]["detail"]
    assert r["Power"]["ok"]

    better = snap(storage__boot_kind="NVMe SSD", database__standbys=1)
    better["network"]["ifaces"][0]["state"] = "up"
    r = {x["what"]: x for x in sh.resilience(better, 2)}
    assert r["Servers"]["ok"] and r["Main disk"]["ok"] and r["Network"]["ok"] and r["Database copy"]["ok"]


def test_the_chart_row_leaves_tailscale_out_of_the_network_total():
    tot = sh.totals(snap())
    assert tot["net_rx_kbps"] == 2.0 and tot["net_tx_kbps"] == 1.0      # wlan0 only, not the VPN on top of it
    assert tot["req_per_min"] == 12.0 and tot["disk_used_pct"] == 35.0 and tot["problems"] == 0
    assert tot["mem_used_mb"] == 2048


def test_collect_never_crashes_on_a_machine_without_proc():
    """The laptop (Windows) has no /proc, /sys, systemctl or vcgencmd: every
    section comes back empty instead of the page failing."""
    s = sh.collect(None, with_db=False)
    assert s["host"] == sh.HOST and "services" in s and "websites" in s
    sh.checks(s)
    sh.resilience(s)
    sh.totals(s)


# ── network path: router + internet delay ───────────────────────────────────
PING_OK = """PING 192.168.1.1 (192.168.1.1) 56(84) bytes of data.

--- 192.168.1.1 ping statistics ---
10 packets transmitted, 9 received, 10% packet loss, time 1811ms
rtt min/avg/max/mdev = 11.592/138.141/304.967/80.968 ms, pipe 2
"""
PING_DEAD = """--- 192.168.1.1 ping statistics ---
10 packets transmitted, 0 received, +10 errors, 100% packet loss, time 2034ms
"""


def test_ping_summary_is_read():
    p = sh.parse_ping(PING_OK)
    assert p == {"sent": 10, "received": 9, "loss_pct": 10.0, "min_ms": 11.592, "avg_ms": 138.141, "max_ms": 304.967}
    d = sh.parse_ping(PING_DEAD)
    assert d["received"] == 0 and d["loss_pct"] == 100.0 and d["avg_ms"] is None
    assert sh.parse_ping("") is None and sh.parse_ping("ping: unknown host") is None


def test_the_router_address_comes_from_the_default_route(monkeypatch):
    route = ("Iface\tDestination\tGateway \tFlags\tRefCnt\tUse\tMetric\tMask\n"
             "wlan0\t0001A8C0\t00000000\t0001\t0\t0\t600\t00FFFFFF\n"
             "wlan0\t00000000\t0101A8C0\t0003\t0\t0\t600\t00000000\n")
    monkeypatch.setattr(sh, "_read", lambda path: route if path == "/proc/net/route" else "")
    assert sh.gateway() == "192.168.1.1"
    monkeypatch.setattr(sh, "_read", lambda path: "")
    assert sh.gateway() is None


def _link(r_avg, r_loss=0, i_avg=20, i_loss=0, wifi=True):
    mk = lambda avg, loss: {"sent": 10, "received": round(10 * (100 - loss) / 100), "loss_pct": loss,
                            "min_ms": avg and avg / 2, "avg_ms": avg, "max_ms": avg and avg * 2}
    return {"t": 0, "gateway": "192.168.1.1", "router": mk(r_avg, r_loss), "internet": mk(i_avg, i_loss),
            "wifi_retries": 5 if wifi else None}


def test_a_slow_home_network_is_called_out():
    assert sh.link_checks(_link(3)) == []                                   # healthy: 1-5 ms
    lv = [c[0] for c in sh.link_checks(_link(45))]
    assert lv == ["warn"]
    crit = sh.link_checks(_link(138))
    assert [c[0] for c in crit] == ["crit"] and "138 ms to the router" in crit[0][2] and "over Wi-Fi" in crit[0][2]
    assert [c[0] for c in sh.link_checks(_link(3, r_loss=40))] == ["crit"]   # mostly lost
    dead = sh.link_checks(_link(None, r_loss=100, i_avg=None, i_loss=100))
    assert len(dead) == 1 and "did not answer at all" in dead[0][2]        # one cause, not two alarms
    no_inet = sh.link_checks(_link(3, i_avg=None, i_loss=100))
    assert [c[0] for c in no_inet] == ["crit"] and "internet connection is down" in no_inet[0][2]
    lossy = sh.link_checks(_link(3, i_loss=20))
    assert [c[0] for c in lossy] == ["warn"] and "internet connection itself" in lossy[0][2]
    assert sh.link_checks(None) == []


def test_the_network_findings_reach_the_page_and_the_charts():
    snap = {"link": _link(138, i_avg=160)}
    assert any(i["area"] == "Network" and i["level"] == "crit" for i in sh.checks(snap))
    tot = sh.totals(snap)
    assert tot["router_ms"] == 138 and tot["router_loss_pct"] == 0 and tot["internet_ms"] == 160
    assert sh.totals({})["router_ms"] is None
    assert {"router_ms", "router_loss_pct", "internet_ms", "internet_loss_pct"} <= set(sh.SERIES)
