"""
app/server_health.py — the Server page: how the machine the portal runs on is doing

The fleet portal, the violations portal, TurboGen, the public website and the
Pathsco dashboard all live on one Raspberry Pi (beverly-pi). This module reads
that machine the way `top`, `df`, `free`, `ss` and `systemctl` would —
straight from /proc, /sys and a few commands, no extra package — and says in
plain words what is wrong with it and what would take every site down at once.

  collect(prev)     one snapshot of everything, a JSON-safe dict. Speeds (CPU %,
                    network, disk writes, requests a minute, each service's CPU)
                    are worked out against `prev`, the snapshot before it; with
                    none recent enough it reads twice, one second apart.
  checks(snap)      [{level, area, text}] — the problems, worst first
  resilience(snap)  [{ok, what, detail}] — single points of failure
  sample_once()     one snapshot into server_samples (the charts, one row a
                    minute) and server_hosts (the newest whole snapshot)
  ServerSampler     a daemon thread calling sample_once() every minute

SEVERAL SERVERS: every row carries the host name, and the page has a picker as
soon as a second host appears in server_hosts. Only the machine the portal is
running on is read live; another server reports by writing its own snapshot
(the same collect(), run there). Nothing here assumes there is only one.

Two gunicorn workers each run the sampler: a transaction-level advisory lock
plus "has this host been sampled in the last 48 s?" keeps it to one snapshot
a minute, and the unique (host, minute) row is the last line of defence.

Read-only: nothing here changes the machine. Website checks are a plain GET of
"/" on the local port behind each tunnel hostname, with that Host header, and
never follow a redirect (an Access or login redirect still proves it answers).
"""
import datetime as _dt
import glob
import http.client
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from app import shutdown

log = logging.getLogger(__name__)

HOST = socket.gethostname()
HOME = os.path.expanduser("~")
LOCK_KEY = 875_219_006          # 875_219_00x are the portal's advisory locks (billing = 005)
RAW_KEEP_S = 900                # a snapshot older than this is too old to measure speeds against

# The services this server is expected to run, in plain words. A unit that is
# not installed on a machine is left out, so the same list works on a new Pi.
WATCH = [
    ("fleet-portal.service", "system", "Fleet portal"),
    ("beverly.service", "system", "Violations portal"),
    ("turbogen-dashboard.service", "system", "TurboGen dashboard"),
    ("postgresql@*-main.service", "system", "Database (PostgreSQL)"),
    ("cloudflared.service", "system", "Cloudflare tunnel: portals + TurboGen"),
    ("echotherm-website.service", "user", "Public website"),
    ("echotherm-website-tunnel.service", "user", "Cloudflare tunnel: public website"),
    ("pathsco-dashboard.service", "user", "Pathsco finance dashboard"),
    ("pathsco-finance-tunnel.service", "user", "Cloudflare tunnel: Pathsco dashboard"),
    ("tailscaled.service", "system", "Tailscale (remote access)"),
    ("ssh.service", "system", "SSH (remote login)"),
]
# Nightly backups: what, where, the file pattern, the unit that writes them,
# and the folder of the off-site copy (a git clone that is pushed), if any.
BACKUPS = [
    ("Fleet portal database", "~/fleet-portal-backups", "fleetportal_*.dump",
     "fleet-portal-backup.service", "~/fleet-portal-offsite"),
    ("Violations portal database", "~/beverly-backups", "beverly_*.dump",
     "beverly-backup.service", None),
]
BACKUP_MAX_AGE_H = 30           # nightly, so a day and a bit is late

THROTTLE_BITS = {0: "under-voltage (power supply too weak)", 1: "CPU speed capped",
                 2: "CPU throttled", 3: "soft temperature limit"}


# ── small helpers ────────────────────────────────────────────────────────────
def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def _run(args, timeout=6, env=None) -> str:
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=env)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _user_env() -> dict:
    """systemctl --user from a system service needs the user's runtime dir."""
    env = dict(os.environ)
    rd = env.get("XDG_RUNTIME_DIR") or (f"/run/user/{os.getuid()}" if hasattr(os, "getuid") else "")
    if rd:
        env["XDG_RUNTIME_DIR"] = rd
        env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={rd}/bus")
    return env


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def size(n, unit="B") -> str:
    """1536 -> '1.5 KB'. None (or anything not a number) -> '—'."""
    if not _num(n):
        return "—"
    n = float(n)
    for u in ("", "K", "M", "G", "T"):
        if abs(n) < 1024 or u == "T":
            return (f"{n:.0f} {u}{unit}" if u == "" or n >= 100 else f"{n:.1f} {u}{unit}")
        n /= 1024
    return f"{n:.1f} T{unit}"


def rate(bps) -> str:
    """Bytes a second -> '1.2 MB/s'."""
    return size(bps) + "/s" if _num(bps) else "—"


def ago(secs) -> str:
    if not _num(secs):
        return "—"
    secs = max(0, int(secs))
    if secs < 90:
        return f"{secs} s"
    if secs < 5400:
        return f"{secs // 60} min"
    if secs < 2 * 86400:
        h, m = divmod(secs // 60, 60)
        return f"{h} h {m} min" if m and h < 10 else f"{h} h"
    d, h = divmod(secs // 3600, 24)
    return f"{d} d {h} h" if h else f"{d} d"


def _per_s(cur, old, dt):
    if cur is None or old is None or not dt or dt <= 0 or cur < old:   # a counter reset (reboot)
        return None
    return (cur - old) / dt


# ── pure parsers (tests drive these with real text from the Pi) ──────────────
def parse_cpu(text: str):
    """/proc/stat first line -> {busy, total, iowait} in jiffies."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            v = [int(x) for x in line.split()[1:]]
            v += [0] * (8 - len(v))
            idle = v[3] + v[4]
            total = sum(v[:8])          # guest time is already inside user
            return {"busy": total - idle, "total": total, "iowait": v[4]}
    return None


def cpu_pct(cur, old):
    if not cur or not old:
        return None, None
    dt = cur["total"] - old["total"]
    if dt <= 0:
        return None, None
    return (round(100 * (cur["busy"] - old["busy"]) / dt, 1),
            round(100 * (cur["iowait"] - old["iowait"]) / dt, 1))


def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {Key: bytes}."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"(\w+):\s+(\d+)(?:\s+kB)?", line)
        if m:
            out[m.group(1)] = int(m.group(2)) * (1024 if "kB" in line else 1)
    return out


def parse_net_dev(text: str) -> dict:
    """/proc/net/dev -> {iface: {rx, tx, rx_err, rx_drop, tx_err, tx_drop}} (bytes / packets)."""
    out = {}
    for line in text.splitlines()[2:]:
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        f = [int(x) for x in rest.split()]
        if len(f) >= 12:
            out[name.strip()] = {"rx": f[0], "rx_err": f[2], "rx_drop": f[3],
                                 "tx": f[8], "tx_err": f[10], "tx_drop": f[11]}
    return out


def parse_blockstat(text: str):
    """/sys/block/<dev>/stat -> {read, written} bytes (sectors are always 512 bytes here)."""
    f = text.split()
    if len(f) < 7:
        return None
    return {"read": int(f[2]) * 512, "written": int(f[6]) * 512}


def decode_throttled(value: int) -> dict:
    """vcgencmd get_throttled bits: 0-3 happening now, 16-19 happened since boot."""
    return {"now": [t for b, t in THROTTLE_BITS.items() if value & (1 << b)],
            "since_boot": [t for b, t in THROTTLE_BITS.items() if value & (1 << (b + 16))]}


def parse_tunnel_config(text: str) -> dict:
    """A cloudflared config.yml -> {tunnel, routes: [(hostname, service)]}. Line-based on
    purpose (no YAML library): ingress entries are '- hostname: x' then 'service: y'."""
    tunnel, routes, host = None, [], None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        m = re.match(r"tunnel:\s*(\S+)", line)
        if m:
            tunnel = m.group(1)
        m = re.match(r"-?\s*hostname:\s*(\S+)", line)
        if m:
            host = m.group(1).strip("'\"")
            continue
        m = re.match(r"-?\s*service:\s*(\S+)", line)
        if m and host:
            routes.append((host, m.group(1).strip("'\"")))
            host = None
    return {"tunnel": tunnel, "routes": routes}


def parse_tunnel_metrics(text: str) -> dict:
    """cloudflared /metrics (Prometheus text) -> {requests, errors, connections}."""
    req = err = conns = None
    for line in text.splitlines():
        if line.startswith("cloudflared_tunnel_response_by_code{"):
            req = (req or 0) + float(line.rsplit(" ", 1)[1])
        elif line.startswith("cloudflared_tunnel_request_errors "):
            err = float(line.rsplit(" ", 1)[1])
        elif line.startswith("cloudflared_tunnel_ha_connections "):
            conns = int(float(line.rsplit(" ", 1)[1]))
    if req is not None or err is not None:
        req = (req or 0) + (err or 0)
    return {"requests": req, "errors": err, "connections": conns}


def parse_systemctl_show(text: str) -> list:
    """`systemctl show -p A,B unit1 unit2` -> one dict per unit (blank-line separated)."""
    units, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                units.append(cur)
                cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    if cur:
        units.append(cur)
    return units


def parse_ss_listen(text: str) -> list:
    """`ss -ltnpH` -> [(addr, port, pid or None, process name or None)]."""
    out = []
    for line in text.splitlines():
        f = line.split()
        if len(f) < 4:
            continue
        addr, _, port = f[3].rpartition(":")
        if not port.isdigit():
            continue
        m = re.search(r'\(\("([^"]+)",pid=(\d+)', line)
        out.append((addr, int(port), int(m.group(2)) if m else None, m.group(1) if m else None))
    return out


def _systemd_time(v: str):
    """'Tue 2026-11-24 17:53:00 EST' -> epoch seconds (None if unset)."""
    if not v or v in ("n/a", "0"):
        return None
    m = re.search(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)", v)
    if not m:
        return None
    # systemctl prints local time; the Pi's clock zone is the box's own zone.
    try:
        return time.mktime(time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, OverflowError):
        return None


# ── the sections of a snapshot ───────────────────────────────────────────────
def _system() -> dict:
    up = _read("/proc/uptime").split()
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?$', _read("/etc/os-release"), re.M))
    load = _read("/proc/loadavg").split()
    # systemd-timesyncd leaves this file once the clock is synchronised. Asking
    # timedatectl instead starts a systemd service (and logs it) every minute.
    sync = ("yes" if os.path.exists("/run/systemd/timesync/synchronized")
            else "no" if os.path.isdir("/run/systemd/timesync") else "")
    mhz = _read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").strip()
    return {
        "host": HOST,
        "model": _read("/proc/device-tree/model").strip("\x00\n ") or None,
        "os": osr.get("PRETTY_NAME"),
        "kernel": os.uname().release if hasattr(os, "uname") else None,
        "uptime_s": float(up[0]) if up else None,
        "cores": os.cpu_count(),
        "load": [float(x) for x in load[:3]] if len(load) >= 3 else None,
        "cpu_mhz": int(mhz) // 1000 if mhz.isdigit() else None,
        "clock_synced": {"yes": True, "no": False}.get(sync),
        "reboot_required": os.path.exists("/var/run/reboot-required"),
    }


def _temp_throttle() -> dict:
    t = _read("/sys/class/thermal/thermal_zone0/temp").strip()
    raw = _run(["vcgencmd", "get_throttled"]).strip()     # 'throttled=0x50000'
    m = re.search(r"0x([0-9a-fA-F]+)", raw)
    val = int(m.group(1), 16) if m else None
    return {"temp_c": round(int(t) / 1000, 1) if t.lstrip("-").isdigit() else None,
            "throttled": val, **(decode_throttled(val) if val is not None else {"now": [], "since_boot": []})}


def _memory() -> dict:
    mi = parse_meminfo(_read("/proc/meminfo"))
    if not mi.get("MemTotal"):
        return {}
    total, avail = mi["MemTotal"], mi.get("MemAvailable", mi.get("MemFree", 0))
    st, sf = mi.get("SwapTotal", 0), mi.get("SwapFree", 0)
    return {"total": total, "available": avail, "used": total - avail,
            "used_pct": round(100 * (total - avail) / total, 1),
            "cached": mi.get("Cached", 0) + mi.get("Buffers", 0),
            "swap_total": st, "swap_used": st - sf,
            "swap_pct": round(100 * (st - sf) / st, 1) if st else None}


REAL_FS = {"ext4", "ext3", "ext2", "xfs", "btrfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3"}


def _disk_kind(dev: str) -> str:
    base = os.path.basename(dev)
    if base.startswith("mmcblk"):
        return "SD card / eMMC"
    if base.startswith("nvme"):
        return "NVMe SSD"
    if base.startswith("sd"):
        return "USB drive"
    return "disk"


def _parent_disk(dev: str) -> str:
    base = os.path.basename(dev)
    m = re.match(r"(mmcblk\d+|nvme\d+n\d+)(p\d+)?$", base) or re.match(r"(sd[a-z]+)\d*$", base)
    return m.group(1) if m else base


def _storage() -> dict:
    mounts, seen = [], set()
    for line in _read("/proc/mounts").splitlines():
        f = line.split()
        if len(f) < 3 or f[2] not in REAL_FS or f[0] in seen or not f[0].startswith("/dev/"):
            continue
        seen.add(f[0])
        try:
            st = os.statvfs(f[1])
        except (OSError, AttributeError):
            continue
        total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
        used = total - st.f_bfree * st.f_frsize
        mounts.append({"mount": f[1], "device": f[0], "fs": f[2], "kind": _disk_kind(f[0]),
                       "total": total, "used": used, "free": free,
                       "used_pct": round(100 * used / (used + free), 1) if used + free else None})
    root = next((m for m in mounts if m["mount"] == "/"), None)
    disks = {}
    for m in mounts:
        d = _parent_disk(m["device"])
        if d not in disks:
            io = parse_blockstat(_read(f"/sys/block/{d}/stat"))
            card = None
            if d.startswith("mmcblk"):
                dd = f"/sys/block/{d}/device/"
                card = {"name": _read(dd + "name").strip() or None, "made": _read(dd + "date").strip() or None,
                        "maker_id": _read(dd + "manfid").strip() or None}
            disks[d] = {"dev": d, "kind": _disk_kind(d), "io": io, "card": card}
    return {"mounts": mounts, "root": root, "boot_kind": root["kind"] if root else None,
            "boot_disk": _parent_disk(root["device"]) if root else None, "disks": disks}


def _network() -> dict:
    counters = parse_net_dev(_read("/proc/net/dev"))
    addrs = {}
    try:
        for i in json.loads(_run(["ip", "-j", "addr"]) or "[]"):
            addrs[i.get("ifname")] = [a.get("local") for a in i.get("addr_info", [])
                                      if a.get("local") and not a["local"].startswith("fe80")]
    except ValueError:
        pass
    default = None
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000":
            default = f[0]
            break
    ifaces = []
    for name, c in counters.items():
        if name == "lo":
            continue
        kind = ("Wi-Fi" if os.path.isdir(f"/sys/class/net/{name}/wireless") or name.startswith("wl")
                else "VPN (Tailscale)" if name.startswith("tailscale")
                else "Ethernet" if name.startswith(("eth", "en")) else "other")
        state = _read(f"/sys/class/net/{name}/operstate").strip() or None
        ifaces.append({"name": name, "kind": kind, "state": state, "addrs": addrs.get(name, []),
                       "default": name == default, **c})
    wifi = None
    for line in _run(["nmcli", "-t", "-f", "IN-USE,SIGNAL,SSID,CHAN,RATE", "dev", "wifi", "list",
                      "--rescan", "no"]).splitlines():
        if line.startswith("*:"):
            p = line.split(":")
            wifi = {"signal": int(p[1]) if p[1].isdigit() else None, "ssid": p[2],
                    "channel": p[3], "rate": p[4] if len(p) > 4 else None}
            break
    est = 0
    for f in ("/proc/net/tcp", "/proc/net/tcp6"):
        est += sum(1 for line in _read(f).splitlines()[1:] if len(line.split()) > 3 and line.split()[3] == "01")
    return {"ifaces": ifaces, "default": default, "wifi": wifi, "connections": est}


# ── how good the network path is (router + internet delay) ──────────────────
# A slow or lossy home network looks exactly like a dead server from outside:
# every request crosses it, so a 150 ms Wi-Fi hop to the router makes every
# site slow for everyone. Measured by the sampler once a minute (pinging takes
# ~2 s, too slow for a page load) and shown from the newest stored snapshot.
INTERNET_PING = "1.1.1.1"           # Cloudflare: the tunnels' own provider
PING_COUNT = 10
ROUTER_WARN_MS, ROUTER_CRIT_MS = 30, 100     # a healthy home network answers in 1-5 ms
LOSS_WARN_PCT, LOSS_CRIT_PCT = 10, 30
LINK_FRESH_S = 300                  # an older measurement isn't shown as "now"
_PING_RE = re.compile(r"(\d+) packets transmitted, (\d+) (?:packets )?received")
_RTT_RE = re.compile(r"= ([\d.]+)/([\d.]+)/([\d.]+)")


def parse_ping(text: str):
    """iputils `ping -q` summary -> {sent, received, loss_pct, min_ms, avg_ms, max_ms}
    (the times None when nothing came back), or None when there is no summary."""
    m = _PING_RE.search(text or "")
    if not m:
        return None
    sent, got = int(m.group(1)), int(m.group(2))
    r = _RTT_RE.search(text)
    return {"sent": sent, "received": got, "loss_pct": round(100 * (sent - got) / sent, 1) if sent else None,
            "min_ms": float(r.group(1)) if r else None, "avg_ms": float(r.group(2)) if r else None,
            "max_ms": float(r.group(3)) if r else None}


def gateway() -> str | None:
    """The router's address: the default route's gateway in /proc/net/route
    (little-endian hex)."""
    for line in _read("/proc/net/route").splitlines()[1:]:
        f = line.split()
        if len(f) > 2 and f[1] == "00000000" and f[2] != "00000000":
            try:
                return socket.inet_ntoa(int(f[2], 16).to_bytes(4, "little"))
            except (ValueError, OverflowError):
                return None
    return None


def _ping(addr: str) -> dict | None:
    return parse_ping(_run(["ping", "-n", "-q", "-c", str(PING_COUNT), "-i", "0.2", "-W", "1", addr],
                           timeout=PING_COUNT * 0.2 + 4))


def _link() -> dict:
    """Delay and loss to the router and to the internet, measured side by side
    (about 2 s), plus the Wi-Fi retry counter when the card reports one."""
    gw = gateway()
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_gw = ex.submit(_ping, gw) if gw else None
        f_in = ex.submit(_ping, INTERNET_PING)
        router = f_gw.result() if f_gw else None
        internet = f_in.result()
    retries = None
    for line in _read("/proc/net/wireless").splitlines()[2:]:
        f = line.split()
        if len(f) >= 10 and f[0].rstrip(":").startswith("wl"):
            try:
                retries = int(f[8])
            except ValueError:
                pass
            break
    return {"t": time.time(), "gateway": gw, "router": router, "internet": internet, "wifi_retries": retries}


def _procs() -> list:
    """Every process: pid, name, rss bytes, the systemd unit it belongs to."""
    out = []
    for p in glob.glob("/proc/[0-9]*"):
        st = _read(p + "/status")
        if not st:
            continue
        name = re.search(r"^Name:\s*(.+)$", st, re.M)

        def kb(key):
            m = re.search(rf"^{key}:\s*(\d+)", st, re.M)
            return int(m.group(1)) * 1024 if m else 0
        cg = _read(p + "/cgroup")
        units = re.findall(r"/([^/\n]+\.service)", cg)
        out.append({"pid": int(p.rsplit("/", 1)[1]), "name": name.group(1) if name else "?",
                    "rss": kb("VmRSS"), "own": kb("RssAnon"), "shared": kb("RssShmem"),
                    "unit": units[-1] if units else None})
    return out


def unit_memory(procs) -> dict:
    """Memory per service: each process's own (anonymous) memory, plus the
    shared-memory segment ONCE (PostgreSQL's shared buffers appear in every
    one of its processes; adding them up would show it using several GB)."""
    own, shared = {}, {}
    for p in procs:
        u = p.get("unit")
        if u:
            own[u] = own.get(u, 0) + p.get("own", 0)
            shared[u] = max(shared.get(u, 0), p.get("shared", 0))
    return {u: own[u] + shared.get(u, 0) for u in own}


def _services(procs) -> list:
    props = "Id,ActiveState,SubState,LoadState,NRestarts,ActiveEnterTimestamp,CPUUsageNSec,MainPID"
    want = {"system": [], "user": []}
    labels, order = {}, {}
    for i, (unit, scope, label) in enumerate(WATCH):
        if "*" in unit:          # e.g. postgresql@17-main: find the installed version
            found = re.findall(r"^\s*(postgresql@\S+-main\.service)", _run(
                ["systemctl", "list-units", "--plain", "--no-legend", "--all", unit]), re.M)
            unit = found[0] if found else None
        if unit:
            want[scope].append(unit)
            labels[(scope, unit)] = label
            order[(scope, unit)] = i
    rss_by_unit = unit_memory(procs)
    out = []
    for scope, units in want.items():
        if not units:
            continue
        args = ["systemctl"] + (["--user"] if scope == "user" else []) + ["show", "-p", props] + units
        for u in parse_systemctl_show(_run(args, env=_user_env() if scope == "user" else None)):
            if u.get("LoadState") in (None, "not-found"):
                continue
            cpu = u.get("CPUUsageNSec", "")
            out.append({"unit": u.get("Id"), "scope": scope, "label": labels.get((scope, u.get("Id")), u.get("Id")),
                        "state": u.get("ActiveState"), "sub": u.get("SubState"),
                        "restarts": int(u["NRestarts"]) if u.get("NRestarts", "").isdigit() else None,
                        "since": _systemd_time(u.get("ActiveEnterTimestamp", "")),
                        "cpu_ns": int(cpu) if cpu.isdigit() else None,
                        "rss": rss_by_unit.get(u.get("Id"))})
    out.sort(key=lambda s: order.get((s["scope"], s["unit"]), 99))
    return out


def _tunnels() -> list:
    """Every running cloudflared: its config, the hostnames it serves, and its
    own health page (/ready: connections to Cloudflare) and request counters."""
    procs = []
    for p in glob.glob("/proc/[0-9]*"):
        if _read(p + "/comm").strip() != "cloudflared":
            continue
        args = _read(p + "/cmdline").split("\x00")
        cfg = args[args.index("--config") + 1] if "--config" in args[:-1] else None
        metrics = args[args.index("--metrics") + 1] if "--metrics" in args[:-1] else None
        if not cfg:
            cfg = next((c for c in (os.path.join(HOME, ".cloudflared/config.yml"), "/etc/cloudflared/config.yml")
                        if os.path.exists(c)), None)
        procs.append({"pid": int(p.rsplit("/", 1)[1]), "config": cfg,
                      "port": int(metrics.rsplit(":", 1)[1]) if metrics and metrics.rsplit(":", 1)[-1].isdigit() else None})
    # Metrics ports: cloudflared takes the first free of 20241-20245 unless told.
    # ss names the owner only for our own processes; a root-owned tunnel's port
    # is the one left over.
    listen = [x for x in parse_ss_listen(_run(["ss", "-ltnpH"])) if 20241 <= x[1] <= 20260 and x[0] in ("127.0.0.1", "localhost")]
    by_pid = {pid: port for _, port, pid, name in listen if pid}
    for t in procs:
        t["port"] = t["port"] or by_pid.get(t["pid"])
    free_ports = [port for _, port, pid, _ in listen if not pid and port not in {t["port"] for t in procs}]
    unplaced = [t for t in procs if not t["port"]]
    if len(unplaced) == 1 and len(free_ports) == 1:
        unplaced[0]["port"] = free_ports[0]
    out = []
    for t in procs:
        cfg = parse_tunnel_config(_read(t["config"])) if t["config"] else {"tunnel": None, "routes": []}
        ready = met = None
        if t["port"]:
            ready = _http_json("127.0.0.1", t["port"], "/ready")
            met = parse_tunnel_metrics(_http_text("127.0.0.1", t["port"], "/metrics") or "")
        out.append({"pid": t["pid"], "config": t["config"], "name": os.path.basename(t["config"] or "?"),
                    "tunnel": cfg["tunnel"], "routes": cfg["routes"], "port": t["port"],
                    "ready": (ready or {}).get("readyConnections"),
                    "requests": (met or {}).get("requests"), "errors": (met or {}).get("errors")})
    out.sort(key=lambda t: t["name"])
    return out


def _http(host, port, path, hostname=None, timeout=4):
    t0 = time.time()
    try:
        c = http.client.HTTPConnection(host, port, timeout=timeout)
        c.request("GET", path, headers={"Host": hostname or f"{host}:{port}",
                                        "User-Agent": "fleet-portal-server-check"})
        r = c.getresponse()
        body = r.read(200_000)
        c.close()
        return r.status, round((time.time() - t0) * 1000), body, None
    except (OSError, http.client.HTTPException) as e:
        return None, round((time.time() - t0) * 1000), b"", str(e) or e.__class__.__name__


def _http_json(host, port, path):
    st, _, body, _ = _http(host, port, path, timeout=3)
    try:
        return json.loads(body) if st else None
    except ValueError:
        return None


def _http_text(host, port, path):
    st, _, body, _ = _http(host, port, path, timeout=3)
    return body.decode("utf-8", "replace") if st else None


def _websites(tunnels) -> list:
    jobs = []
    for t in tunnels:
        for hostname, service in t["routes"]:
            m = re.match(r"https?://([^:/]+)(?::(\d+))?", service)
            if m:
                jobs.append((hostname, service, t["name"], m.group(1), int(m.group(2) or 80)))

    def probe(j):
        hostname, service, tunnel, h, port = j
        st, ms, _, err = _http(h, port, "/", hostname=hostname)
        return {"hostname": hostname, "service": service, "tunnel": tunnel, "port": port,
                "code": st, "ms": ms, "error": err, "ok": st is not None and st < 500}
    if not jobs:
        return []
    with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
        return list(ex.map(probe, jobs))


def _backups(now: float) -> list:
    out = []
    for label, folder, pattern, unit, offsite in BACKUPS:
        d = os.path.expanduser(folder)
        files = sorted(glob.glob(os.path.join(d, pattern)), key=lambda f: os.path.getmtime(f))
        newest = files[-1] if files else None
        st = parse_systemctl_show(_run(["systemctl", "show", "-p", "Id,LoadState,Result,ExecMainStatus,ExecMainExitTimestamp", unit]))
        st = st[0] if st else {}
        if not files and st.get("LoadState") in (None, "not-found"):
            continue                  # not on this machine
        off = None
        if offsite and os.path.isdir(os.path.join(os.path.expanduser(offsite), ".git")):
            line = _run(["git", "-C", os.path.expanduser(offsite), "log", "-1", "--format=%ct"]).strip()
            off = {"at": int(line) if line.isdigit() else None}
            if off["at"]:
                off["age_s"] = now - off["at"]
        out.append({"label": label, "folder": folder, "count": len(files),
                    "total": sum(os.path.getsize(f) for f in files),
                    "newest": os.path.basename(newest) if newest else None,
                    "size": os.path.getsize(newest) if newest else None,
                    "at": os.path.getmtime(newest) if newest else None,
                    "age_s": now - os.path.getmtime(newest) if newest else None,
                    "result": st.get("Result"), "exit": st.get("ExecMainStatus"),
                    "ran_at": _systemd_time(st.get("ExecMainExitTimestamp", "")),
                    "offsite": off, "offsite_configured": bool(offsite)})
    return out


def _database() -> dict:
    """Sizes, connections and standby copies, through the portal's own connection."""
    try:
        from sqlalchemy import text
        from app.extensions import db
        q = lambda s: db.session.execute(text(s)).all()      # noqa: E731
        out = {"databases": [{"name": n, "size": int(s)} for n, s in q(
                   "SELECT datname, pg_database_size(datname) FROM pg_database "
                   "WHERE NOT datistemplate AND datname <> 'postgres' ORDER BY 2 DESC")],
               "connections": q("SELECT count(*) FROM pg_stat_activity WHERE backend_type = 'client backend'")[0][0],
               "max_connections": int(q("SHOW max_connections")[0][0]),
               "started": q("SELECT extract(epoch FROM pg_postmaster_start_time())")[0][0],
               "version": q("SHOW server_version")[0][0],
               "standbys": q("SELECT count(*) FROM pg_stat_replication")[0][0],
               "is_standby": q("SELECT pg_is_in_recovery()")[0][0]}
        out["started"] = float(out["started"]) if out["started"] is not None else None
        db.session.rollback()          # read-only: end the transaction
        return out
    except Exception as e:            # the page must render even with the database in trouble
        try:
            from app.extensions import db
            db.session.rollback()
        except Exception:
            pass
        return {"error": str(e).splitlines()[0][:200]}


# ── one snapshot ─────────────────────────────────────────────────────────────
def _raw():
    st = _storage_io_only()
    return {"t": time.time(), "cpu": parse_cpu(_read("/proc/stat")),
            "net": {k: [v["rx"], v["tx"]] for k, v in parse_net_dev(_read("/proc/net/dev")).items()},
            "disk": st}


def _storage_io_only():
    out = {}
    for d in os.listdir("/sys/block") if os.path.isdir("/sys/block") else []:
        if d.startswith(("loop", "ram", "zram")):
            continue
        io = parse_blockstat(_read(f"/sys/block/{d}/stat"))
        if io:
            out[d] = [io["read"], io["written"]]
    return out


def collect(prev=None, with_db=True, measure_link=False) -> dict:
    """Everything about this machine right now (see the module docstring).
    measure_link pings the router and the internet (~2 s, the sampler does it);
    otherwise the newest measurement is carried over from `prev` while fresh."""
    raw = _raw()
    old = (prev or {}).get("raw")
    if not old or not (0.5 <= raw["t"] - old.get("t", 0) <= RAW_KEEP_S) or not raw["cpu"]:
        old = raw
        time.sleep(1)
        raw = _raw()
    dt = raw["t"] - old["t"]
    snap = {"v": 1, "host": HOST, "t": raw["t"], "raw": raw, "interval_s": round(dt, 1)}
    snap["system"] = _system()
    snap["cpu"] = dict(zip(("pct", "iowait_pct"), cpu_pct(raw["cpu"], old.get("cpu"))))
    snap["heat"] = _temp_throttle()
    snap["memory"] = _memory()
    snap["storage"] = _storage()
    for d, io in snap["storage"]["disks"].items():
        o, c = old.get("disk", {}).get(d), raw["disk"].get(d)
        io["read_bps"] = _per_s(c[0], o[0], dt) if c and o else None
        io["write_bps"] = _per_s(c[1], o[1], dt) if c and o else None
        up = snap["system"]["uptime_s"]
        io["written_per_day"] = (c[1] / (up / 86400)) if c and up and up > 3600 else None
    snap["network"] = _network()
    for i in snap["network"]["ifaces"]:
        o = old.get("net", {}).get(i["name"])
        i["rx_bps"] = _per_s(i["rx"], o[0], dt) if o else None
        i["tx_bps"] = _per_s(i["tx"], o[1], dt) if o else None
    procs = _procs()
    snap["top"] = [{k: p[k] for k in ("pid", "name", "rss", "unit")}
                   for p in sorted(procs, key=lambda p: -p["rss"])[:10]]
    snap["services"] = _services(procs)
    pdt = raw["t"] - (prev or {}).get("t", 0) if prev else None
    prev_svc = {s["unit"]: s for s in (prev or {}).get("services", [])}
    cores = snap["system"]["cores"] or 1
    for s in snap["services"]:
        o = prev_svc.get(s["unit"])
        d = _per_s(s["cpu_ns"], o.get("cpu_ns") if o else None, pdt) if pdt and pdt <= RAW_KEEP_S else None
        s["cpu_pct"] = round(d / 1e9 / cores * 100, 1) if d is not None else None
    snap["tunnels"] = _tunnels()
    prev_tun = {t["name"]: t for t in (prev or {}).get("tunnels", [])}
    for t in snap["tunnels"]:
        o = prev_tun.get(t["name"])
        r = _per_s(t["requests"], o.get("requests") if o else None, pdt) if pdt and pdt <= RAW_KEEP_S else None
        t["req_per_min"] = round(r * 60, 1) if r is not None else None
    snap["websites"] = _websites(snap["tunnels"])
    old_link = (prev or {}).get("link")
    if measure_link:
        link = _link()
        o = old_link or {}
        gap = link["t"] - o.get("t", 0)
        link["wifi_retries_per_min"] = (round((link["wifi_retries"] - o["wifi_retries"]) / gap * 60)
                                        if _num(link.get("wifi_retries")) and _num(o.get("wifi_retries"))
                                        and 0 < gap <= RAW_KEEP_S and link["wifi_retries"] >= o["wifi_retries"] else None)
        snap["link"] = link
    elif old_link and raw["t"] - old_link.get("t", 0) <= LINK_FRESH_S:
        snap["link"] = old_link
    else:
        snap["link"] = None
    snap["backups"] = _backups(raw["t"])
    snap["database"] = _database() if with_db else None
    return snap


def totals(snap: dict) -> dict:
    """The few numbers a chart row keeps (server_samples)."""
    net = [i for i in snap.get("network", {}).get("ifaces", []) if not i["kind"].startswith("VPN")]
    rx = [i["rx_bps"] for i in net if i.get("rx_bps") is not None]
    tx = [i["tx_bps"] for i in net if i.get("tx_bps") is not None]
    st = snap.get("storage", {})
    boot = st.get("disks", {}).get(st.get("boot_disk") or "", {})
    link = snap.get("link") or {}
    router, inet = link.get("router") or {}, link.get("internet") or {}
    reqs = [t["req_per_min"] for t in snap.get("tunnels", []) if t.get("req_per_min") is not None]
    mem = snap.get("memory", {})
    issues = checks(snap)
    return {"cpu_pct": snap.get("cpu", {}).get("pct"),
            "load1": (snap.get("system", {}).get("load") or [None])[0],
            "mem_used_pct": mem.get("used_pct"),
            "mem_used_mb": round(mem["used"] / 2**20) if mem.get("used") is not None else None,
            "swap_used_mb": round(mem["swap_used"] / 2**20) if mem.get("swap_used") is not None else None,
            "disk_used_pct": (st.get("root") or {}).get("used_pct"),
            "temp_c": snap.get("heat", {}).get("temp_c"),
            "net_rx_kbps": round(sum(rx) / 1024, 1) if rx else None,
            "net_tx_kbps": round(sum(tx) / 1024, 1) if tx else None,
            "disk_write_kbps": round(boot["write_bps"] / 1024, 1) if boot.get("write_bps") is not None else None,
            "req_per_min": round(sum(reqs), 1) if reqs else None,
            "router_ms": router.get("avg_ms"), "router_loss_pct": router.get("loss_pct"),
            "internet_ms": inet.get("avg_ms"), "internet_loss_pct": inet.get("loss_pct"),
            "throttled": snap.get("heat", {}).get("throttled"),
            "problems": sum(1 for i in issues if i["level"] in ("crit", "warn"))}


# ── what is wrong ────────────────────────────────────────────────────────────
def checks(snap: dict) -> list:
    out = []

    def add(level, area, text):
        out.append({"level": level, "area": area, "text": text})

    root = (snap.get("storage") or {}).get("root")
    if root and root.get("used_pct") is not None:
        p = root["used_pct"]
        if p >= 90:
            add("crit", "Storage", f"The main disk is {p:.0f}% full ({size(root['free'])} left). Databases stop writing when it fills.")
        elif p >= 80:
            add("warn", "Storage", f"The main disk is {p:.0f}% full ({size(root['free'])} left).")
    for m in (snap.get("storage") or {}).get("mounts", []):
        if m is not root and m.get("used_pct") is not None and m["used_pct"] >= 90:
            add("warn", "Storage", f"{m['mount']} is {m['used_pct']:.0f}% full.")
    mem = snap.get("memory") or {}
    if mem.get("total"):
        free_pct = 100 * mem["available"] / mem["total"]
        if free_pct < 5:
            add("crit", "Memory", f"Only {size(mem['available'])} of memory left — programs will start being killed.")
        elif free_pct < 15:
            add("warn", "Memory", f"Memory is getting tight: {size(mem['available'])} free of {size(mem['total'])}.")
        if mem.get("swap_pct") is not None and mem["swap_pct"] >= 50:
            add("warn", "Memory", f"Swap is {mem['swap_pct']:.0f}% used — the machine is short of memory and slows down.")
    heat = snap.get("heat") or {}
    t = heat.get("temp_c")
    if t is not None:
        if t >= 80:
            add("crit", "Temperature", f"The processor is at {t:.0f} °C — it slows itself down above 80 °C. Check the fan / airflow.")
        elif t >= 70:
            add("warn", "Temperature", f"The processor is running hot: {t:.0f} °C.")
    for what in heat.get("now", []):
        add("crit" if what.startswith("under-voltage") else "warn", "Power", f"Right now: {what}.")
    for what in heat.get("since_boot", []):
        if what not in heat.get("now", []):
            add("warn" if what.startswith("under-voltage") else "info", "Power",
                f"Since the last restart: {what} happened at least once."
                + (" The power supply dipped — a weak supply corrupts SD cards." if what.startswith("under-voltage") else ""))
    sysi = snap.get("system") or {}
    load, cores = sysi.get("load"), sysi.get("cores") or 1
    if load and load[1] > cores * 2:
        add("warn", "CPU", f"The processor is overloaded: load {load[1]:.1f} over 5 minutes on {cores} cores.")
    cpu = (snap.get("cpu") or {}).get("pct")
    if cpu is not None and cpu >= 90:
        add("warn", "CPU", f"The processor is {cpu:.0f}% busy.")
    if sysi.get("clock_synced") is False:
        add("warn", "System", "The clock is not synchronised to internet time — logins and certificates can fail.")
    if sysi.get("reboot_required"):
        add("info", "System", "Updates are installed that need a restart.")
    for s in snap.get("services", []):
        if s["state"] != "active":
            add("crit", "Services", f"{s['label']} is {s['state']} ({s['unit']}).")
        elif s.get("restarts"):
            add("info", "Services", f"{s['label']} restarted itself {s['restarts']} time{'s' if s['restarts'] != 1 else ''} since it was started.")
    for w in snap.get("websites", []):
        if not w["ok"]:
            why = f"answered {w['code']}" if w["code"] else f"did not answer ({w['error']})"
            add("crit", "Websites", f"{w['hostname']} {why}.")
        elif w["ms"] and w["ms"] > 3000:
            add("warn", "Websites", f"{w['hostname']} is slow: {w['ms'] / 1000:.1f} s to answer.")
    for tn in snap.get("tunnels", []):
        if tn["ready"] is None and tn["port"]:
            add("warn", "Websites", f"Tunnel {tn['name']}: its health page did not answer.")
        elif tn["ready"] == 0:
            add("crit", "Websites", f"Tunnel {tn['name']} has no connection to Cloudflare — its sites are offline.")
        elif tn["ready"] is not None and tn["ready"] < 2:
            add("warn", "Websites", f"Tunnel {tn['name']} has only {tn['ready']} of its usual 4 connections to Cloudflare.")
    for b in snap.get("backups", []):
        if b["result"] and b["result"] != "success":
            add("crit", "Backups", f"{b['label']}: the last nightly backup FAILED ({b['result']}).")
        if b["age_s"] is None:
            add("crit", "Backups", f"{b['label']}: no backup file found in {b['folder']}.")
        elif b["age_s"] > BACKUP_MAX_AGE_H * 3600:
            add("crit", "Backups", f"{b['label']}: the newest backup is {ago(b['age_s'])} old.")
        off = b.get("offsite")
        if off and off.get("age_s") is not None and off["age_s"] > BACKUP_MAX_AGE_H * 3600:
            add("warn", "Backups", f"{b['label']}: the off-site copy is {ago(off['age_s'])} old — the nightly push is failing.")
    for i in link_checks(snap.get("link")):
        add(*i)
    dbi = snap.get("database") or {}
    if dbi.get("error"):
        add("crit", "Database", f"Could not read the database: {dbi['error']}")
    elif dbi.get("max_connections") and dbi.get("connections", 0) > 0.8 * dbi["max_connections"]:
        add("warn", "Database", f"{dbi['connections']} of {dbi['max_connections']} database connections in use.")
    rank = {"crit": 0, "warn": 1, "info": 2}
    out.sort(key=lambda i: rank[i["level"]])
    return out


def link_checks(link) -> list:
    """(level, area, text) for a slow or lossy network path. The router hop is
    the home network itself (Wi-Fi or cable); the internet hop adds the
    provider. Either one slow makes every website slow for every visitor."""
    out = []
    if not link:
        return out
    r, i = link.get("router"), link.get("internet")
    wired = "" if (link.get("wifi_retries") is None) else " over Wi-Fi"
    if link.get("gateway") and r and r.get("received") == 0:
        out.append(("crit", "Network", f"The router ({link['gateway']}) did not answer at all{wired} — "
                                       "the server is cut off from the local network."))
    elif r:
        avg, loss = r.get("avg_ms"), r.get("loss_pct") or 0
        said = ((f"{avg:.0f} ms to the router on average" + (f" (up to {r['max_ms']:.0f} ms)" if _num(r.get("max_ms")) else "")
                 if _num(avg) else "the router barely answers") + f", {loss:.0f}% lost. Normal is 1-5 ms")
        why = (" Everything going to or from the websites crosses this hop, so every site is slow for every visitor; "
               "other devices on the same Wi-Fi feel it too. A network cable to the router fixes it for the server.")
        if (avg is not None and avg >= ROUTER_CRIT_MS) or loss >= LOSS_CRIT_PCT:
            out.append(("crit", "Network", f"The local network is very slow{wired}: {said}." + why))
        elif (avg is not None and avg >= ROUTER_WARN_MS) or loss >= LOSS_WARN_PCT:
            out.append(("warn", "Network", f"The local network is slow{wired}: {said}." + why))
    if i and i.get("received") == 0 and not (r and r.get("received") == 0):
        out.append(("crit", "Network", f"The internet ({INTERNET_PING}) did not answer although the router did — "
                                       "the internet connection is down; every site is offline from outside."))
    elif i and (i.get("loss_pct") or 0) >= LOSS_WARN_PCT and not (r and (r.get("loss_pct") or 0) >= LOSS_WARN_PCT):
        out.append(("warn", "Network", f"{i['loss_pct']:.0f}% of pings to the internet were lost while the router answered "
                                       "fine — the internet connection itself is dropping packets."))
    return out


def level(issues) -> str:
    lv = {i["level"] for i in issues}
    return "crit" if "crit" in lv else "warn" if "warn" in lv else "ok"


def resilience(snap: dict, n_servers: int = 1) -> list:
    """What would take every site down at once — the single points of failure,
    each {ok, what, detail}. The plan for more servers fixes these one by one."""
    out = []
    sites = sorted({w["hostname"] for w in snap.get("websites", [])})
    out.append({"ok": n_servers > 1, "what": "Servers",
                "detail": (f"{n_servers} servers report here." if n_servers > 1 else
                           f"One server. All {len(sites)} websites run only on {snap.get('host')}: "
                           "if it stops, every one of them goes down together.")})
    st = snap.get("storage") or {}
    kind = st.get("boot_kind")
    boot = (st.get("disks") or {}).get(st.get("boot_disk") or "", {})
    per_day = boot.get("written_per_day")
    out.append({"ok": kind not in ("SD card / eMMC",), "what": "Main disk",
                "detail": (f"Runs from an {kind.split(' /')[0]}" if kind else "Unknown")
                + (f", about {size(per_day)} written a day" if per_day else "")
                + (". SD cards wear out from database writes and fail without warning; an NVMe or USB SSD is far sturdier."
                   if kind == "SD card / eMMC" else ".")})
    net = snap.get("network") or {}
    wired = any(i["kind"] == "Ethernet" and i["state"] == "up" for i in net.get("ifaces", []))
    dflt = next((i for i in net.get("ifaces", []) if i.get("default")), None)
    out.append({"ok": wired, "what": "Network",
                "detail": ("Wired Ethernet." if wired else
                           f"Wi-Fi only ({(net.get('wifi') or {}).get('ssid') or (dflt or {}).get('name') or '?'}); "
                           "the Ethernet port is unplugged. Wi-Fi drops take every site offline — a cable is more reliable.")})
    dbi = snap.get("database") or {}
    out.append({"ok": bool(dbi.get("standbys")), "what": "Database copy",
                "detail": (f"{dbi['standbys']} live standby cop{'ies' if dbi['standbys'] != 1 else 'y'} following every change."
                           if dbi.get("standbys") else
                           "No live standby. If the disk dies, the fleet and violations portals restore from last night's backup "
                           "(up to a day of changes lost) onto a rebuilt machine.")})
    for b in snap.get("backups", []):
        off = b.get("offsite")
        ok = bool(off and off.get("at"))
        out.append({"ok": ok, "what": f"Backup: {b['label']}",
                    "detail": (f"Nightly on this disk, and an encrypted copy off-site ({ago(off.get('age_s'))} ago)." if ok else
                               "Nightly, but ONLY on this same disk — if the disk fails, the backups go with it.")})
    heat = snap.get("heat") or {}
    uv = any(w.startswith("under-voltage") for w in heat.get("now", []) + heat.get("since_boot", []))
    out.append({"ok": not uv, "what": "Power",
                "detail": ("The power supply dipped since the last restart — replace it; no battery backup either."
                           if uv else "No power dips since the last restart. No battery backup (UPS): a power cut stops every site.")})
    return out


# ── storing snapshots ────────────────────────────────────────────────────────
def _utcnow():
    return _dt.datetime.utcnow()


def live(with_db=True) -> dict:
    """A fresh snapshot of this machine, measured against the stored one."""
    from app.extensions import db
    from app.models import ServerHost
    row = db.session.get(ServerHost, HOST)
    prev = row.snapshot if row is not None else None
    db.session.rollback()
    return collect(prev, with_db=with_db)


def sample_once(interval_s=60, keep_days=30, collect_fn=None):
    """One snapshot a minute for this host, whichever process gets there first.
    Returns the stored totals, or None when another process had it."""
    from sqlalchemy import text
    from app.extensions import db
    from app.models import ServerHost, ServerSample
    collect_fn = collect_fn or collect
    got = db.session.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY}).scalar()
    if not got:
        db.session.rollback()
        return None
    now = _utcnow()
    row = db.session.get(ServerHost, HOST)
    if row is not None and row.seen_at and (now - row.seen_at).total_seconds() < interval_s * 0.8:
        db.session.rollback()
        return None
    snap = collect_fn(row.snapshot if row is not None else None, measure_link=True)
    tot = totals(snap)
    minute = now.replace(second=0, microsecond=0)
    db.session.execute(
        text("INSERT INTO server_samples (host, at, cpu_pct, load1, mem_used_pct, mem_used_mb, swap_used_mb, "
             "disk_used_pct, temp_c, net_rx_kbps, net_tx_kbps, disk_write_kbps, req_per_min, "
             "router_ms, router_loss_pct, internet_ms, internet_loss_pct, throttled, problems) "
             "VALUES (:host, :at, :cpu_pct, :load1, :mem_used_pct, :mem_used_mb, :swap_used_mb, :disk_used_pct, "
             ":temp_c, :net_rx_kbps, :net_tx_kbps, :disk_write_kbps, :req_per_min, "
             ":router_ms, :router_loss_pct, :internet_ms, :internet_loss_pct, :throttled, :problems) "
             "ON CONFLICT (host, at) DO NOTHING"),
        {"host": HOST, "at": minute, **tot})
    if row is None:
        row = ServerHost(host=HOST)
        db.session.add(row)
    row.seen_at, row.snapshot = now, json.loads(json.dumps(snap, default=str))
    if minute.minute == 0:            # once an hour: drop what is older than the keep window
        db.session.execute(text("DELETE FROM server_samples WHERE at < :cut"),
                           {"cut": now - _dt.timedelta(days=keep_days)})
    db.session.commit()
    return tot


SERIES = ("cpu_pct", "load1", "mem_used_pct", "swap_used_mb", "disk_used_pct", "temp_c",
          "net_rx_kbps", "net_tx_kbps", "disk_write_kbps", "req_per_min",
          "router_ms", "router_loss_pct", "internet_ms", "internet_loss_pct")


def history(host: str, hours: int) -> dict:
    """Chart data: {t: [epoch ms], <series>: [...]}, averaged into buckets so a
    month is ~1,400 points, not 43,000."""
    from sqlalchemy import text
    from app.extensions import db
    hours = max(1, min(int(hours), 24 * 31))
    bucket = 60 if hours <= 24 else 600 if hours <= 24 * 7 else 1800
    cols = ", ".join(f"avg({c})" for c in SERIES)
    rows = db.session.execute(text(
        f"SELECT floor(extract(epoch FROM at) / :b) * :b AS t, {cols} FROM server_samples "
        f"WHERE host = :h AND at >= :since GROUP BY 1 ORDER BY 1"),
        {"b": bucket, "h": host, "since": _utcnow() - _dt.timedelta(hours=hours)}).all()
    db.session.rollback()
    out = {"t": [int(float(r[0]) * 1000) for r in rows], "bucket_s": bucket, "hours": hours}
    for i, c in enumerate(SERIES, start=1):
        out[c] = [round(float(r[i]), 2) if r[i] is not None else None for r in rows]
    return out


class ServerSampler:
    """Daemon thread, same shape as the other pollers: start(app), one cycle a
    minute, log-and-swallow. SERVER_SAMPLER_ENABLED=0 turns it off."""

    def __init__(self):
        self._thread = None

    def start(self, app) -> None:
        if not app.config.get("SERVER_SAMPLER_ENABLED", True):
            log.info("Server sampler disabled (SERVER_SAMPLER_ENABLED=0)")
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="server-sampler", daemon=True)
        self._thread.start()

    def _run(self, app) -> None:
        interval = app.config.get("SERVER_SAMPLE_SECONDS", 60)
        keep = app.config.get("SERVER_KEEP_DAYS", 30)
        time.sleep(10)                # let the app finish starting
        while not shutdown.stopping():         # app/shutdown.py: no new round while the worker exits
            try:
                with app.app_context():
                    sample_once(interval, keep)
            except Exception as e:
                log.warning("Server sample failed: %s", e)
                try:
                    with app.app_context():
                        from app.extensions import db
                        db.session.rollback()
                except Exception:
                    pass
            time.sleep(interval)


server_sampler = ServerSampler()
