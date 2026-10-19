"""
app/legacy.py — the installed 2010-generation gateways, read over oBIX

These are the boxes the Windows "USE Controller" client and Anagha read: an
oBIX server 2.6.3 on a Rabbit board (firmware like 3.2.2), one per building,
mostly behind a shared public address with one port per box. The portal reads
them VIEW-ONLY for the Anagha-style live view (/live): nothing here writes.

How they must be treated, learned on the first real box (2026-10-14):
  * Digest login only (realm "USEnergy"). Before a login is ever sent to an
    address, it must have answered as one of these gateways (401 with that
    realm, or an oBIX reply): an old address can belong to someone else now.
  * One requests.Session per box, kept between polls, so the Digest
    handshake is reused: a fresh handshake per request took ~14 s each.
  * A pause between requests to the same box: back-to-back requests make it
    reset the connection.
  * Sensor names differ per box (Aqu1/Aquast, Rsr1/Riser1...): each box's own
    obix/systemnames labels its system temperatures.

Poll: obix/status every cycle, obix/alarm every cycle, obix/systemnames on
the first poll and daily. Pure HTTP happens in worker threads; the database
is written afterwards, in the poller's thread.
"""
import datetime as _dt
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from requests.auth import HTTPDigestAuth
from sqlalchemy import text

from app import history

log = logging.getLogger(__name__)

_LOCK_KEY = 875_219_002
PAUSE = 2.0
TIMEOUT = 30
NAMES_EVERY = _dt.timedelta(hours=24)
STALE = _dt.timedelta(minutes=15)
_PAIR = re.compile(r'<(\w+)\s+name\s*=\s*"([^"]*)"\s*(?:val\s*=\s*"([^"]*)")?')

# The gateway's heat-mode codes (EMS FC_Mem.c HeatMd[]) and how they read,
# with the colour the card's status pill gets.
STATUS = {
    "Summer": ("Summer Mode", "idle"),
    "Outsd Sat": ("Outside Temp. Satisfied", "ok"),
    "Insd Sat": ("Inside Temp. Satisfied", "ok"),
    "DTime Sat": ("Desired Time Satisfied", "ok"),
    "DTime Rem": ("Desired Time Remaining", "heat"),
    "Aqua Call": ("Aquastat Call", "heat"),
    "Pressure": ("Waiting for Pressure", "heat"),
    "Circ Pump": ("Pump Time Remaining", "heat"),
    "Auto": ("Auto", "ok"),
    "Ign Fail": ("Ignition Failure", "crit"),
    "Safety": ("Safety", "crit"),
    "FpByp On": ("Front Panel Bypass On", "warn"),
    "FpByp Off": ("Front Panel Bypass Off", "ok"),
    "RmByp On": ("Remote Bypass On", "warn"),
    "RmByp Off": ("Remote Bypass Off", "ok"),
    "RyByp On": ("Relay Bypass On", "warn"),
    "RyByp Off": ("Relay Bypass Off", "ok"),
    "RlyBypass": ("Relay Bypass", "warn"),
    "Cooling": ("Cooling", "ok"),
    "Cool OffT": ("Cooling Off Time", "ok"),
}

# Which system temperature plays which part on the card, by the box's own
# sensor name (case-insensitive prefix; the first match wins).
ROLES = [("out", ("out",)), ("aqua", ("aqu",)), ("coil", ("coi",)), ("mix", ("mix",)),
         ("supp", ("hwts", "sup")), ("retn", ("hwtr", "ret", "cond")), ("risr", ("rsr", "ris"))]


# From the box's obix/emsConfig (read with the sensor names, daily): how many
# boilers it really has and its fuel. Boxes report run minutes for boilers
# they do not have (217 Howard Ave, one boiler, showed 830 heat minutes on
# "boiler 2"): Anagha sums only numBoilers, and so do we (2026-10-15).
CONFIG_KEYS = ("numBoilers", "fuelType", "heatControlType", "installationType", "numWaterMeters", "numFuelMeters",
               "numGasMeters", "emsControlsBoiler1", "emsControlsHotWater1", "emsControlsBoiler2", "emsControlsHotWater2")


def boilers(data) -> int:
    """How many boilers to count: the box's numBoilers, or 1 until it is known."""
    n = _int(((data or {}).get("config") or {}).get("numBoilers"))
    return min(4, n) if n and n > 0 else 1


def fuel(data):
    """'Gas' or 'Oil' (emsConfig fuelType: 0 = gas, anything else oil, as
    Anagha labels them), or None until known."""
    f = _int(((data or {}).get("config") or {}).get("fuelType"))
    return None if f is None else ("Gas" if f == 0 else "Oil")


# The box's heatControlType (emsConfig, also in its status), as the EMS reads
# the 2010 codes (EMS config.migrate_legacy_hct / control/system_types.py):
# 1 steam, 5 vacuum steam, 7 the old DB-Interrupt (vacuum steam); 9 / 11 / 13
# / 15 hot water (pump / mixing valve / boiler / condensing). Checked against
# the boxes 2026-10-18: every box that ever said "Waiting for Pressure" (a
# steam-only state) is coded 1, and no hot-water box ever did.
STEAM_HCT = {1: "Steam", 5: "Vacuum steam", 7: "Vacuum steam"}
HOT_WATER_HCT = {9: "Hot water (pump)", 11: "Hot water (mixing valve)", 13: "Hot water (boiler)",
                 15: "Hot water (condensing)"}


def heat_code(data):
    d = data or {}
    return _int((d.get("config") or {}).get("heatControlType")) or _int((d.get("status") or {}).get("heatControlType"))


def heat_system(data):
    """'steam', 'hot water', or None when the box has not said."""
    hct = heat_code(data)
    return "steam" if hct in STEAM_HCT else "hot water" if hct in HOT_WATER_HCT else None


def heat_label(data):
    hct = heat_code(data)
    return STEAM_HCT.get(hct) or HOT_WATER_HCT.get(hct)


# Ignition failures are not alerted on hot-water buildings: several boilers,
# and not all have their fuel valve wired back, so the box cannot tell a
# boiler that failed to light from one that was never asked (the user,
# 2026-10-18). On steam they matter most, and the alert carries the evidence.
def ignition_ignored(data) -> bool:
    return heat_system(data) == "hot water"


def status_code(raw) -> str:
    """The box's heat-mode code. Now and then a box drops the first letters
    of it ("afety", "tsd Sat", "utsd Sat": once each in a night, 2026-10-15),
    which read as no status, a missed Safety, and false status changes. A
    code that is the unique ending of a known one is that code."""
    code = (raw or "").strip()
    if code in STATUS or len(code) < 4:
        return code
    hits = [k for k in STATUS if k.endswith(code)]
    return hits[0] if len(hits) == 1 else code


def values(xml: str) -> dict:
    """{name: val} from an oBIX reply (the gateway's XML is line-oriented and
    not always well-formed, so this is a tolerant scan, not an XML parser)."""
    out = {}
    for tag, name, val in _PAIR.findall(xml or ""):
        if tag not in ("obj", "list", "op", "ref"):
            out[name.strip()] = (val or "").strip()
    return out


def parse_alarm(xml: str):
    """The latest alarm the box reports: {name, at, active} or None. A box
    with no alarms answers an object with no display name, stamped with the
    current time: that is not an alarm (seen on 13 boxes, 2026-10-14)."""
    if not xml or "<obj" not in xml:
        return None
    m = re.search(r'display\s*=\s*"([^"]*)"', xml)
    if not m or not m.group(1).strip():
        return None
    v = values(xml)
    return {"name": m.group(1).strip(), "at": v.get("timestamp", ""), "active": not v.get("normalTimestamp")}


# The alarm names the gateways send, how they read, and how much they matter
# (only 'crit' and 'warn' make a box a "problem"; a restart is information).
ALARMS = {
    "safetyActive": ("Safety", "crit"),
    "ignitionFail": ("Ignition failure", "crit"),
    "noAptSensorsReporting": ("No apartment sensors reporting", "warn"),
    "emsRestart": ("Controller restarted", "info"),
    "bypassChange": ("Bypass changed", "info"),
}
try:
    from zoneinfo import ZoneInfo
    LOCAL = ZoneInfo("America/New_York")
except Exception:                      # no tz database: show UTC
    LOCAL = _dt.timezone.utc


def clock_drift(data) -> _dt.timedelta:
    """How far the box's clock is off: its serverTime against when the
    portal read it. Several boxes run an hour off (a wrong DST or time-zone
    setting); drifts under 10 minutes are ignored."""
    try:
        box = _dt.datetime.fromisoformat(((data or {}).get("status") or {}).get("serverTime", "").replace("Z", "+00:00"))
        read = _dt.datetime.fromisoformat((data or {}).get("read_at", ""))
        d = box.astimezone(_dt.timezone.utc).replace(tzinfo=None) - read
        d = _dt.timedelta(minutes=round(d.total_seconds() / 60))       # whole minutes
        return d if abs(d) >= _dt.timedelta(minutes=10) else _dt.timedelta(0)
    except (ValueError, TypeError, AttributeError):
        return _dt.timedelta(0)


def alarm_info(alarm, now=None, drift=_dt.timedelta(0)):
    """Display form of a box alarm: {words, level, when, recent, active} or
    None. Times arrive in each box's own offset, and some clocks are an hour
    off, so they are corrected by the box's drift, put through UTC and shown
    in New York time."""
    if not alarm:
        return None
    now = now or _dt.datetime.utcnow()
    words, level = ALARMS.get(alarm.get("name"), (alarm.get("name") or "Alarm", "warn"))
    when, recent, utc = alarm.get("at") or "", False, None
    try:
        at = _dt.datetime.fromisoformat(when.replace("Z", "+00:00"))
        if at.tzinfo is None:
            at = at.replace(tzinfo=LOCAL)
        at = at - drift
        utc = at.astimezone(_dt.timezone.utc).replace(tzinfo=None)
        recent = _dt.timedelta(0) <= now - utc < _dt.timedelta(hours=24) or _dt.timedelta(0) < utc - now < _dt.timedelta(minutes=15)
        when = at.astimezone(LOCAL).strftime("%b %d %I:%M %p").replace(" 0", " ")
    except ValueError:
        pass
    return {"words": words, "level": level, "when": when, "recent": recent, "utc": utc,
            "active": bool(alarm.get("active")), "name": alarm.get("name")}


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def hhmm(minutes):
    """Minutes as hh:mm; a dash for anything that is not a number (a box
    never read has no readings at all)."""
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        return "—"
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def card(data: dict) -> dict:
    """The Anagha-style card from one box's stored reading (LegacyStatus.data)."""
    st = (data or {}).get("status") or {}
    names = (data or {}).get("names") or {}
    temps = []
    for k, v in st.items():
        if k.startswith("systemTemperature") and k[17:].isdigit():
            i = int(k[17:])
            temps.append((i, (names.get(f"systemSensorName{i}") or f"#{i}").strip(), _int(v)))
    temps.sort()
    role = {}
    for key, prefixes in ROLES:
        for _i, nm, val in temps:
            if nm.lower().startswith(prefixes) and key not in role:
                role[key] = val
    code = status_code(st.get("callForHeat"))
    phrase, tone = STATUS.get(code, (code or "No status", "idle"))
    # Run times are the sum over the box's boilers, as Anagha shows them
    # (checked 2026-10-15: 101-109 Lowell's hot water 121 + 184 = Anagha's 305).
    runs = [(i, _int(st.get(f"boiler{i}HeatRunMinutes")), _int(st.get(f"boiler{i}HWtrRunMinutes")))
            for i in range(1, boilers(data) + 1)]
    total = lambda k: (sum(r[k] or 0 for r in runs) if any(r[k] is not None for r in runs) else None)
    heat, hw = total(1), total(2)
    gallons, level = _int(st.get("gallons")), _int(st.get("fuelLevel"))
    apts = [_int(st[k]) for k in sorted(st, key=lambda k: int(k[20:] or 0) if k[20:].isdigit() else 0)
            if k.startswith("apartmentTemperature")]
    return {
        "code": code, "status": phrase, "tone": tone,
        "out": role.get("out"), "inside": _int(st.get("averageTemperature")), "dit": _int(st.get("targetInsideTemperature")),
        "heat_rt": heat, "hw_rt": hw, "total_rt": (heat or 0) + (hw or 0) if heat is not None or hw is not None else None,
        "aqua": role.get("aqua"), "coil": role.get("coil"), "mix": role.get("mix"),
        "stack": _int(st.get("stackTemperature")), "hi_stack": _int(st.get("highStackTemp")),
        "water": _int(st.get("waterMeterCount")),
        "supp": role.get("supp"), "retn": role.get("retn"), "risr": role.get("risr"),
        "oil_pct": level if (gallons or level) else None,
        "boilers": [(i, h, w) for i, h, w in runs if h or w],     # per boiler, those that ran today
        "n_boilers": boilers(data), "fuel": fuel(data),
        "system": heat_system(data), "system_label": heat_label(data),
        "temps": temps, "apartments": [a for a in apts if a],
        # (sensor number from 0, reading) of the apartment sensors that read, for the charts
        "apt_list": sorted((int(k[20:]), _int(v)) for k, v in st.items()
                           if k.startswith("apartmentTemperature") and k[20:].isdigit() and _int(v)),
        "alarm": (data or {}).get("alarm"),
        "box_time": st.get("serverTime"),
    } | {"tint": tint(role, _int(st.get("averageTemperature")), _int(st.get("targetInsideTemperature")),
                      _int(st.get("stackTemperature")), temps)}


# Cell colours, decoded from Anagha's Live page (2026-10-15: every one of its
# 188 rows checked against these rules) so both read the same at a glance:
#   blue   = cold / not up to temperature      red    = too hot, or a fault
#   teal   = warm, but explained (the weather; lukewarm hot water)
#   orange = scald risk
# What Anagha leaves out, and these add: each colour says why, with numbers.
TINT_LEGEND = [("cold", "cold / not up to temperature"), ("hot", "too hot, or a fault"),
               ("wx", "warm because of the weather · lukewarm hot water"), ("scald", "hot water scald risk")]


def tint(role, avg, dit, stack, temps) -> dict:
    """{field: (colour, why)} for the cells that deserve a colour."""
    t = {}
    out = role.get("out")
    if avg and dit:
        if out is not None and out >= dit + 2:          # beats red: a hot day is not a fault
            t["inside"] = ("wx", f"{out} F outside, above the desired {dit} F: the warmth is the weather's")
        elif avg >= dit + 11:
            t["inside"] = ("hot", f"{avg - dit} F above the desired {dit} F: overheating, fuel wasted")
        elif avg <= dit - 3:
            t["inside"] = ("cold", f"{dit - avg} F below the desired {dit} F")
    for key, name in (("supp", "Supply"), ("retn", "Return")):
        v = role.get(key)
        if v and v < 90:
            t[key] = ("cold", f"{name} {v} F, under 90 F: the loop is cold")
        elif v and v > 140:
            t[key] = ("hot", f"{name} {v} F, over 140 F")
    v = role.get("aqua")
    if v and v < 150:
        t["aqua"] = ("cold", f"Boiler water {v} F, under 150 F: not up to temperature")
    if stack == 0 and any(val for _i, _n, val in temps):
        t["stack"] = ("hot", "The stack sensor reads 0 while the others read: a sensor or wiring fault")
    elif stack and stack >= 600:
        t["stack"] = ("hot", f"Stack {stack} F: check combustion, or the boiler needs cleaning")
    v = role.get("coil")
    if v and v < 110:
        t["coil"] = ("hot", f"Coil {v} F, under 110 F: it cannot make hot water")
    v = role.get("mix")
    if v and v < 100:
        t["mix"] = ("cold", f"Hot water {v} F: under 100 F")
    elif v and v <= 120:
        t["mix"] = ("wx", f"Hot water {v} F: lukewarm, at or under 120 F (New York's minimum at the tap)")
    elif v and v >= 140:
        t["mix"] = ("scald", f"Hot water {v} F: 140 F or more is a scald risk")
    return t


# ── polling ──────────────────────────────────────────────────────────────────

_sessions = {}
_sessions_lock = threading.Lock()


def _session(box_id, user, password):
    with _sessions_lock:
        s = _sessions.get(box_id)
        # A new session whenever the login changes at all: two logins can
        # share a user name and differ only in the password (2026-10-14).
        if s is None or (s.auth.username, s.auth.password) != (user, password):
            s = requests.Session()
            s.auth = HTTPDigestAuth(user, password)
            _sessions[box_id] = s
        return s


def identify(url) -> str:
    """Ask without any login. 'gateway', 'open' (oBIX without a login),
    'other' (something else is there) or 'unreachable'."""
    try:
        r = requests.get(f"{url}/obix/about", timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException:
        return "unreachable"
    if r.status_code == 401 and "USEnergy" in r.headers.get("WWW-Authenticate", ""):
        return "gateway"
    if r.status_code == 200 and ("useg:" in r.text or "obix" in r.text.lower()):
        return "open"
    return "other"


def poll_one(box: dict, now=None) -> dict:
    """One box, plain dict in and out (runs in a worker thread, no database).
    box: {id, url, user, password, verified, names_due}."""
    now = now or _dt.datetime.utcnow()
    out = {"id": box["id"], "polled_at": now, "ok": False}
    t0 = time.monotonic()
    if not box.get("verified"):
        kind = identify(box["url"])
        out["identity"] = kind
        if kind == "other":
            out["error"] = "something else answers at this address: no login sent"
            return out
        if kind == "unreachable":
            out["error"] = "no answer"
            return out
    s = _session(box["id"], box["user"], box["password"])
    data = {}
    try:
        extra = (["obix/systemnames"] if box.get("names_due") else []) + \
                (["obix/emsConfig"] if box.get("names_due") or box.get("config_due") else [])
        for i, path in enumerate(["obix/status", "obix/alarm"] + extra):
            if i:
                time.sleep(PAUSE)
            r = s.get(f"{box['url']}/{path}", timeout=TIMEOUT)
            if r.status_code == 401:
                with _sessions_lock:
                    _sessions.pop(box["id"], None)       # never retry a refused session
                out["error"], out["auth_failed"] = "the box refused the login", True
                return out
            if r.status_code != 200:
                raise requests.HTTPError(f"HTTP {r.status_code} on {path}")
            if path == "obix/status":
                data["status"] = values(r.text)
            elif path == "obix/alarm":
                data["alarm"] = parse_alarm(r.text)
            elif path == "obix/emsConfig":
                data["config"] = {k: v for k, v in values(r.text).items() if k in CONFIG_KEYS}
            else:
                data["names"] = {k: v for k, v in values(r.text).items() if k.startswith(("systemSensorName", "userAlarm"))}
    except requests.RequestException as e:
        with _sessions_lock:
            _sessions.pop(box["id"], None)           # start clean next time
        out["error"] = f"{type(e).__name__}: {str(e)[:150]}"
        if data.get("status"):                       # status arrived before it failed: keep it
            out.update(ok=True, data=data, partial=True)
        return out
    out.update(ok=True, data=data, response_ms=int((time.monotonic() - t0) * 1000))
    return out


def poll_all(app, now=None, poll=poll_one, workers=None) -> int:
    """One cycle over every active box; returns how many answered."""
    from app.extensions import db
    from app.models import LegacyBox, LegacyStatus
    now = now or _dt.datetime.utcnow()
    boxes = LegacyBox.query.filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None),
                                   LegacyBox.identity != "other").all()
    jobs = []
    for b in boxes:
        if b.login is None:
            continue
        st = b.status
        jobs.append({"id": b.id, "url": b.url, "user": b.login.username, "password": b.login.password,
                     "verified": b.identity in ("gateway", "open"),
                     "names_due": not (st and st.names_at and now - st.names_at < NAMES_EVERY),
                     "config_due": not (st and st.data and st.data.get("config"))})
    # Boxes read side by side (LEGACY_POLL_WORKERS, default 16: a cycle of
    # ~186 boxes took ~2 min at 8, 2026-10-15). Pacing stays per box.
    with ThreadPoolExecutor(max_workers=workers or app.config.get("LEGACY_POLL_WORKERS", 16)) as pool:
        results = list(pool.map(lambda j: poll(j, now), jobs))
    answered = 0
    readings, events = [], []
    for res in results:
        b = db.session.get(LegacyBox, res["id"])
        if "identity" in res:
            b.identity = res["identity"]
        row = b.status or LegacyStatus(box_id=b.id)
        prev_data = dict(row.data or {})
        row.polled_at, row.ok = res["polled_at"], res["ok"]
        row.error = res.get("error")
        row.auth_failed = bool(res.get("auth_failed"))
        row.response_ms = res.get("response_ms")
        if res.get("ok"):
            answered += 1
            row.last_ok_at = res["polled_at"]
            merged = dict(row.data or {})
            merged.update({k: v for k, v in res["data"].items() if v is not None or k == "alarm"})
            merged["read_at"] = res["polled_at"].isoformat()      # for the box's clock drift
            if "names" in res["data"]:
                row.names_at = res["polled_at"]
            readings.append(history.reading(b.id, res["polled_at"], merged))
        else:
            merged = None
        row.data = history.remember(prev_data, res.get("ok"), merged)
        events += history.changes(b.id, res["polled_at"], prev_data, res.get("ok"), row.data, res.get("error"))
        db.session.add(row)
    db.session.commit()
    if app.config.get("LEGACY_HISTORY_ENABLED", True):
        try:
            history.save(readings, events, now)
        except Exception as e:                       # the Live page's readings are already saved
            db.session.rollback()
            log.warning("History save failed: %s", e)
    return answered


class LegacyPoller:
    def __init__(self):
        self._thread = None

    def start(self, app):
        if not app.config.get("LEGACY_POLLER_ENABLED", True):
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="legacy-poller", daemon=True)
        self._thread.start()

    def _run(self, app):
        interval = app.config.get("LEGACY_POLL_INTERVAL_SECONDS", 300)
        time.sleep(20)                        # let the app finish starting
        while True:
            started = time.monotonic()
            try:
                with app.app_context():
                    from app.extensions import db
                    conn = db.engine.connect()
                    try:
                        if conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar():
                            try:
                                # Every gunicorn worker runs this loop; the lock stops two
                                # cycles at once, this stops them running back to back
                                # (the boxes are fragile: one read per interval is plenty).
                                last = conn.execute(text("SELECT max(polled_at) FROM legacy_statuses")).scalar()
                                if last is None or _dt.datetime.utcnow() - last > _dt.timedelta(seconds=interval * 0.8):
                                    n = poll_all(app)
                                    log.info("Legacy boxes: %d answered", n)
                                    # Alerts right after the cycle, under the same lock
                                    # (app/alerts.py check_legacy).
                                    try:
                                        from app import alerts
                                        alerts.check_legacy(app)
                                        alerts.daily_digest(app)
                                    except Exception as e:
                                        db.session.rollback()
                                        log.warning("Legacy alert check failed: %s", e)
                                    # Then the history: hourly / daily sums, old months dropped.
                                    if app.config.get("LEGACY_HISTORY_ENABLED", True):
                                        try:
                                            history.maintain(app)
                                        except Exception as e:
                                            db.session.rollback()
                                            log.warning("History upkeep failed: %s", e)
                            finally:
                                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                    finally:
                        conn.close()
            except Exception as e:
                log.warning("Legacy poll cycle failed: %s", e)
            time.sleep(max(30, interval - (time.monotonic() - started)))


legacy_poller = LegacyPoller()
