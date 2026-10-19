"""
app/fleet_health.py — what the Fleet Overview colours mean
==============================================================================
One place decides whether a controller is fine, needs a look, or needs
someone now, so every screen agrees. Pure functions over a Controller's
latest ControllerStatus row and the `fleetHealth` block EMS publishes in
GET /api/status (EMS use_controller/fleet/health.py). No Flask, no
database: tested directly (tests/test_fleet_health.py).

Levels, worst wins:
  crit     someone should act now: offline, power failing, backup failing,
           disk nearly full, a heating alarm active
  warn     look soon: a backup getting old, running hot, under-voltage
           earlier, status gone quiet
  ok       nothing to do
  unknown  not configured or never polled

Thresholds are deliberately here, as constants, not spread through
templates. A controller that predates fleetHealth (older EMS) shows its
heating data and online state with "health not reported" -- never a false
red.
"""
import datetime as _dt
import time as _time

LEVELS = ("ok", "unknown", "info", "warn", "crit")   # ascending severity

# Controller alarm types (EMS events/event_logger.py ALM_*) that are worth
# paging someone for. The rest (restart, bypass change, gas/oil switch,
# thresholds, valve feedback, scald trend / warning / sensor fault, CO sensor
# fault, stack sensor fault, a LoRaWAN device offline) are shown in yellow on
# the controller's page and never page: the user, 2026-10-18 — nuisance
# alerts make people switch alerts off (a LoRaWAN sensor offline kept the
# bench in alarm for 16 hours).
URGENT_ALARMS = {1: "ignition failure", 3: "safety", 11: "pump failure", 12: "vacuum pump failure",
                 13: "hot water scald trip", 17: "gas leak", 18: "flood", 19: "CO trip", 21: "boiler not heating",
                 23: "CO shutdown", 24: "burner lockout (Fireye)", 25: "boiler fault (Aerco C-More)",
                 26: "burner lockout (Honeywell)", 27: "boiler fault (Aerco Modulex)"}
IGNITION_ALARM = 1


def _temp(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def supply_vs_target(d) -> dict:
    """Hot water: the controller's outdoor-reset target supply (dSupTemp),
    the real supply and return, and - while calling for heat - which of
    supply / return is under legacy.LOOP_COLD_F (the same rule as the 2010
    boxes). Empty for steam."""
    from app import legacy
    if heat_system(d) != "hot water":
        return {}
    target, supply, ret = _temp(d.get("dSupTemp")), _temp(d.get("supTemp")), _temp(d.get("hwrTemp"))
    cold = legacy.loop_cold(supply, ret) if d.get("heatDemand") else []
    return {"supplyTarget": target, "supply": supply, "ret": ret,
            "cold": f"Hot water cold: {legacy.cold_words(cold)}, under {legacy.LOOP_COLD_F} F while calling for heat" if cold else None,
            "supplyCold": ("supply", supply) in cold, "retCold": ("return", ret) in cold}


def heat_system(d):
    """'steam' / 'hot water' / None from a controller's /api/status heatCtrlType
    (EMS control/system_types.py: 1 steam, 5 vacuum steam, 20 hydronic)."""
    try:
        hct = int(d.get("heatCtrlType"))
    except (TypeError, ValueError, AttributeError):
        return None
    return "steam" if hct in (1, 5) else "hot water" if hct == 20 else None
_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}

# --- thresholds -----------------------------------------------------------
STATUS_STALE_SECS = 5 * 60          # no poll result for this long -> the poller itself is in trouble
CPU_TEMP_WARN_C, CPU_TEMP_CRIT_C = 70.0, 80.0
DISK_WARN_PCT, DISK_CRIT_PCT = 80.0, 90.0
LOAD_WARN = 3.0                     # 4 cores; a tuned controller idles near 0.1
HISTORY_WARN_SECS = 36 * 3600       # daily snapshot at 03:00, so >1.5 days is a missed one
HISTORY_CRIT_SECS = 72 * 3600
BUNDLE_WARN_SECS = 8 * 86400        # weekly bundle (Sundays)
BUNDLE_CRIT_SECS = 15 * 86400


def worst(levels) -> str:
    levels = list(levels) or ["ok"]
    return max(levels, key=lambda l: _RANK.get(l, 0))


def ago(seconds) -> str:
    """'just now', '4 min ago', '3 h ago', '2 d ago'."""
    if seconds is None:
        return "never"
    s = max(0, int(seconds))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def duration(seconds) -> str:
    if seconds is None:
        return "—"
    s = int(seconds)
    d, h, m = s // 86400, (s % 86400) // 3600, (s % 3600) // 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def plain_error(http_status, error) -> str:
    """What a poll failure means, in words an operator can act on. The raw
    exception stays on the controller page; the card gets this."""
    e = (error or "").lower()
    if http_status in (401, 403):
        return "token rejected: re-enter the Fleet API token from the controller's Settings"
    if http_status and http_status >= 500:
        return f"the controller answered with an error ({http_status})"
    if http_status == 404:
        return "the controller's software is too old for fleet polling"
    if "timed out" in e or "timeout" in e:
        return "no answer (timed out): powered off, offline, or the address is wrong"
    if "refused" in e:
        return "the box answered but the controller software is not running"
    if "name or service not known" in e or "getaddrinfo" in e or "nodename" in e:
        return "the address does not resolve"
    if "no route" in e or "unreachable" in e:
        return "no network route to it"
    if "json" in e or "expecting value" in e:
        return "it answered with something that is not a status"
    return "no usable answer"


def _age(ts, now):
    return None if not ts else max(0.0, now - float(ts))


def _age_dt(dt, now_dt):
    return None if dt is None else max(0.0, (now_dt - dt).total_seconds())


def evaluate(ctrl, status, now=None, now_dt=None) -> dict:
    """Everything a card or detail page shows for one controller.

    ctrl: has .tailscale_ip, .fleet_api_token
    status: ControllerStatus or None (.ok .polled_at .error .raw_json .version
            .building_name .last_ok_at)
    Returns {level, issues:[(level, text)], online, seen, heating, health,
             backup, software, alarms, restored}.
    """
    now = _time.time() if now is None else now
    now_dt = _dt.datetime.utcnow() if now_dt is None else now_dt
    issues = []
    out = {"issues": issues, "online": None, "seen": None, "heating": None,
           "health": None, "backup": None, "software": None, "alarms": [], "restored": None}

    enr = getattr(ctrl, "enrollment", None)
    enr_state = getattr(enr, "state", None)
    enr_mode = getattr(enr, "mode", "new")
    if enr_state == "assigned" and enr_mode != "restore":
        issues.append(("unknown", "Adopted: waiting for the module to confirm (it checks in every 15 s)"))
        out["level"] = "unknown"
        return out
    restoring = enr_state == "restoring" or (enr_state == "assigned" and enr_mode == "restore")
    if not getattr(ctrl, "tailscale_ip", None) or not getattr(ctrl, "fleet_api_token", None):
        issues.append(("unknown", "Not configured: no address or token"))
        out["level"] = "unknown"
        return out
    if status is None:
        issues.append(("unknown", "Never polled"))
        out["level"] = "unknown"
        return out

    polled_age = _age_dt(status.polled_at, now_dt)
    last_ok_age = _age_dt(getattr(status, "last_ok_at", None), now_dt)
    out["online"] = bool(status.ok)
    out["seen"] = ago(last_ok_age if not status.ok else polled_age)
    if not status.ok:
        out["seen"] = "never answered" if last_ok_age is None else f"last seen {ago(last_ok_age)}"
        out["errorPlain"] = plain_error(getattr(status, "http_status", None), status.error)
        since = f", last seen {ago(last_ok_age)}" if last_ok_age is not None else ""
        if restoring:
            # expected to be dark while the new module reboots into this site
            issues.append(("info", f"Being restored onto a new module{since}"))
        else:
            issues.append(("crit", f"Offline{since}: {out['errorPlain']}"))
    elif polled_age is not None and polled_age > STATUS_STALE_SECS:
        issues.append(("warn", f"Status not refreshed for {ago(polled_age)[:-4]}: is the poller running?"))

    d = status.raw_json or {}
    if d:
        out["heating"] = {
            "mode": d.get("heatMode"), "reason": d.get("heatModeReason"),
            "outdoor": d.get("outdoor"), "avgApt": d.get("avgAptTemp"), "target": d.get("tit"),
            "demand": d.get("heatDemand"), "bypass": d.get("bypass"),
            "blr1Hours": d.get("blr1LifetimeHours"), "blr2Hours": d.get("blr2LifetimeHours"),
            "hasBlr2": d.get("hasBlr2"), "system": heat_system(d),
            **supply_vs_target(d),
        }
        if d.get("bypass"):
            issues.append(("warn", "Bypass is ON: the controller is not in charge of the boilers"))
        for a in d.get("recentAlarms") or []:
            if a.get("active"):
                out["alarms"].append(a)
        if out["alarms"]:
            hot = heat_system(d) == "hot water"
            urgent, minor, ign_ignored = [], [], False
            for a in out["alarms"]:
                t = a.get("type")
                if t == IGNITION_ALARM and hot:
                    ign_ignored = True              # several boilers, fuel valves not all wired back
                elif t in URGENT_ALARMS:
                    urgent.append(a)
                else:
                    minor.append(a)
            label = lambda a: a.get("label") or URGENT_ALARMS.get(a.get("type")) or f"alarm {a.get('type')}"
            if urgent:
                more = f" (+{len(urgent) - 1} more)" if len(urgent) > 1 else ""
                issues.append(("crit", f"Alarm active: {label(urgent[0])}{more}"))
            if minor:
                issues.append(("warn", "Alarm active (not urgent, not paged): " + ", ".join(label(a) for a in minor[:3])))
            if ign_ignored:
                issues.append(("info", "Ignition alarm: not alerted on hot-water systems (no fuel-valve feedback)"))

    fh = d.get("fleetHealth") if d else None
    if not fh:
        if d:
            issues.append(("info", "Health not reported (controller software predates it)"))
        out["software"] = {"version": status.version}
        out["level"] = worst(l for l, _ in issues)
        return out

    # --- the box ---
    h = {k: fh.get(k) for k in ("hostname", "uptimeSecs", "load1", "cpuTempC", "cpuPct", "memUsedPct",
                                "diskUsedPct", "diskFreeGb", "undervoltageNow", "undervoltageSinceBoot",
                                "throttledNow")}
    h["uptime"] = duration(h["uptimeSecs"])
    out["health"] = h
    if h["undervoltageNow"]:
        issues.append(("crit", "Power supply under-voltage NOW: check the PSU"))
    elif h["undervoltageSinceBoot"]:
        issues.append(("warn", "Under-voltage happened since boot: the PSU is marginal"))
    t = h["cpuTempC"]
    if t is not None and t >= CPU_TEMP_CRIT_C:
        issues.append(("crit", f"CPU {t:.0f} °C: overheating"))
    elif t is not None and t >= CPU_TEMP_WARN_C:
        issues.append(("warn", f"CPU {t:.0f} °C: running hot"))
    if h["throttledNow"]:
        issues.append(("warn", "CPU throttled now"))
    du = h["diskUsedPct"]
    if du is not None and du >= DISK_CRIT_PCT:
        issues.append(("crit", f"Disk {du:.0f}% full"))
    elif du is not None and du >= DISK_WARN_PCT:
        issues.append(("warn", f"Disk {du:.0f}% full"))
    if h["load1"] is not None and h["load1"] >= LOAD_WARN:
        issues.append(("warn", f"Load {h['load1']}: something is busy"))

    # --- backup ---
    b = fh.get("backup") or {}
    bk = {"enabled": b.get("enabled"), "lastOk": b.get("lastOk"), "error": b.get("lastError"),
          "configAge": _age(b.get("lastTs"), now), "historyAge": _age(b.get("dbTs"), now),
          "bundleOn": b.get("bundleConfigured"), "bundleAge": _age(b.get("bundleTs"), now),
          "bundleError": b.get("bundleError")}
    bk["configAgo"], bk["historyAgo"], bk["bundleAgo"] = ago(bk["configAge"]), ago(bk["historyAge"]), ago(bk["bundleAge"])
    bk["historyLevel"] = _age_level(bk["historyAge"], HISTORY_WARN_SECS, HISTORY_CRIT_SECS)
    bk["bundleLevel"] = _age_level(bk["bundleAge"], BUNDLE_WARN_SECS, BUNDLE_CRIT_SECS)
    out["backup"] = bk
    if not b:
        issues.append(("warn", "Backup state not reported"))
    elif not bk["enabled"]:
        issues.append(("warn", "Backup is OFF: this site cannot be restored"))
    else:
        if bk["lastOk"] is False:
            issues.append(("crit", f"Last backup FAILED: {(bk['error'] or '')[:100]}"))
        elif b.get("lastTs") is None:
            issues.append(("warn", "No backup has run yet"))
        if bk["historyLevel"] in ("warn", "crit"):
            issues.append((bk["historyLevel"], f"History snapshot {bk['historyAgo']}"))
        if not bk["bundleOn"]:
            issues.append(("warn", "Site bundle OFF: keys, Wi-Fi and tunnel are not backed up"))
        elif bk["bundleError"]:
            issues.append(("crit", f"Site bundle FAILED: {bk['bundleError'][:100]}"))
        elif bk["bundleLevel"] in ("warn", "crit"):
            issues.append((bk["bundleLevel"], f"Site bundle {bk['bundleAgo']}"))

    # --- software ---
    o = fh.get("ota") or {}
    sw = {"version": status.version, "currentSha": (o.get("currentSha") or "")[:7] or None,
          "updateAvailable": o.get("updateAvailable"), "availableSha": (o.get("availableSha") or "")[:7] or None,
          "pending": o.get("pendingConfirm"), "lastUpdateOk": o.get("lastUpdateOk")}
    out["software"] = sw
    if sw["pending"]:
        issues.append(("info", "Software update confirming (10-minute window)"))
    elif sw["lastUpdateOk"] is False:
        issues.append(("warn", "Last software update failed"))
    elif sw["updateAvailable"]:
        issues.append(("info", f"Update available: {sw['availableSha']}"))

    out["restored"] = fh.get("provisioned")
    out["level"] = worst(l for l, _ in issues)
    issues.sort(key=lambda it: -_RANK.get(it[0], 0))
    return out


def _age_level(age, warn, crit) -> str:
    if age is None:
        return "unknown"
    if age >= crit:
        return "crit"
    if age >= warn:
        return "warn"
    return "ok"


def summarize(cards) -> dict:
    """The tiles at the top of the overview."""
    total = len(cards)
    online = sum(1 for c in cards if c["eval"]["online"])
    offline = sum(1 for c in cards if c["eval"]["online"] is False)
    attention = sum(1 for c in cards if c["eval"]["level"] in ("warn", "crit"))
    alarms = sum(1 for c in cards if c["eval"]["alarms"])
    def _bad_backup(e):
        b = e.get("backup")
        return bool(b) and (not b.get("enabled") or b.get("lastOk") is False
                            or b.get("historyLevel") in ("warn", "crit")
                            or b.get("bundleLevel") in ("warn", "crit") or b.get("bundleError"))
    backups = sum(1 for c in cards if _bad_backup(c["eval"]))
    updates = sum(1 for c in cards if (c["eval"].get("software") or {}).get("updateAvailable"))
    unknown = sum(1 for c in cards if c["eval"]["level"] == "unknown")
    return {"total": total, "online": online, "offline": offline, "attention": attention,
            "alarms": alarms, "backups": backups, "updates": updates, "unknown": unknown}
