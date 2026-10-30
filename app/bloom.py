"""
app/bloom.py — Bloom / Empowered Buildings buildings, read view-only

A third monitored source beside the EchoTherm controllers (app/fleet_poller.py)
and the 2010 oBIX boxes (app/legacy.py): buildings on a different vendor's
controller, read through Bloom's private JSON API at my.empoweredbuildings.com.
Added for Skyline Terrace (Canton OH). Nothing here writes to Bloom.

The API (confirmed 2026-10-29 against the live account):
  * POST {base}/login            {email, password}  -> a bearer token (JWT).
        NOT /api/...login (those 404 as URL_NOT_FOUND); the sign-in form posts
        to /login and the token is then sent as Authorization: Bearer on /api/*.
  * GET  {base}/api/assets        -> the ACCOUNT object:
        {key, name, buildings:[...], buildingIds, tags, users}
        Every building the account can see, with its current readings, in ONE
        payload (no per-building endpoint; the app takes live deltas over
        socket.io, which we don't need — we re-poll).
  * GET  {base}/api/alarms        -> a flat list of alarm records, each tagged
        with the building's `locId`; end==null means still open.

A building object carries: _id, name, locid, accounts (e.g. ["skyline-terrace"]
— the authoritative grouping), equipment, zones, and a `sensors` list. Each
sensor is {type, pv, unit, assoc, name, ...}: pv is the present value, `type`
the point (oat-act, ht-supply-t, avg-space-t, pmp-rs, ...), `assoc` which
equipment/zone it belongs to. Per-apartment space sensors carry a `name`;
building-level aggregates/system points have name==null. See POINTS below.

Poll model mirrors fleet_poller: a daemon thread per process, one Postgres
advisory lock per cycle, log-and-swallow throughout. The whole building object
plus its open alarms are stored as raw JSON (BloomStatus.data); the card is
derived at read time by card(), so a Bloom-side change rarely needs a migration.
"""
import datetime as _dt
import logging
import threading
import time

import requests
from sqlalchemy import text

from app.extensions import db

log = logging.getLogger(__name__)

_LOCK_KEY = 875_219_003            # 001 = fleet poller, 002 = legacy, 003 = this
DEFAULT_BASE = "https://my.empoweredbuildings.com"
LOGIN_PATH = "/login"             # {email, password} -> {token}
SKYLINE_ACCOUNT = "skyline-terrace"   # the `accounts` tag that marks a Skyline building

# Loop-heat thresholds, kept the same as the legacy wall's (app/legacy.py /
# app/alerts.py) so "is this building heating?" means the same thing portal-wide.
LOOP_HOT_F = 95.0
LOOP_DRIVE_F = 90.0
LOOP_DELTA_F = 10.0

# Bloom sensor `type` -> our normalised point. Confirmed against the live
# account 2026-10-29 (flask bloom-dump). Building-level single points only;
# pumps are handled per-equipment below.
POINTS = {
    "outside_f":   "oat-act",         # outside air, degF
    "space_avg_f": "avg-space-t",
    "space_min_f": "min-space-t",
    "space_max_f": "max-space-t",
    "setpoint_f":  "ind-sp-act",      # active indoor setpoint (the "SPT")
    "supply_f":    "ht-supply-t",     # heating supply temp
    "return_f":    "ht-ret-t",        # heating return temp
    "supply_sp_f": "hw-supply-sp",    # heating supply setpoint (reset target)
    "boiler_pct":  "blr-pct",         # boiler command %
    "dhw_supply_f":"dhw-supply-t",
    "dhw_lo_sp_f": "dhw-supply-t-lo-sp",
    "oat_cutoff_f":"oat-sp-act",      # outdoor heat cutoff setpoint
}
MODE = {1: "DAY", 2: "NIGHT", 0: "OFF"}
# Alarm types that mean a real fault (vs. an informational/manual flag), used
# only to colour the card red; every open alarm is still listed.
FAULT_ALARMS = {"pmp-run-fail", "blr-run-fail", "blr-run-fail-all", "blr-alm",
                "emp-pi-fail-alm", "hw-lot-alm", "dhw-supply-t-lo-alm", "emp-sec-cfh-alm"}


def _num(v):
    """A float if numeric (Bloom sends temps as numbers, sometimes strings),
    else None. Bloom uses an {'errorClass':..,'errorCode':..} dict for a point
    with no unit/reading — treated as no value."""
    if v is None or isinstance(v, bool) or isinstance(v, dict):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip().rstrip("°%F ").strip())
    except (ValueError, TypeError):
        return None


def _truthy(v):
    return _num(v) == 1.0 if _num(v) is not None else False


def _int(v):
    return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# ── Auth + fetch ─────────────────────────────────────────────────────────────

def _extract_token(payload):
    """Pull the bearer token out of a login response, however it is wrapped."""
    if isinstance(payload, str):
        return payload or None
    if not isinstance(payload, dict):
        return None
    for k in ("token", "accessToken", "access_token", "jwt", "idToken", "id_token"):
        v = payload.get(k)
        if isinstance(v, str) and v:
            return v
    for nest in ("data", "user", "result", "auth"):
        inner = payload.get(nest)
        if isinstance(inner, (dict, str)):
            t = _extract_token(inner)
            if t:
                return t
    return None


def login(account, session, timeout=15):
    """Log in and return a fresh bearer token. POST /login {email, password}."""
    base = (account.base_url or DEFAULT_BASE).rstrip("/")
    url = f"{base}{LOGIN_PATH}"
    user, pw = account.username, account.password
    last = None
    for body in ({"email": user, "password": pw}, {"username": user, "password": pw}):
        try:
            r = session.post(url, json=body, timeout=timeout)
        except requests.RequestException as e:
            last = str(e)
            continue
        if r.status_code in (200, 201):
            tok = _extract_token(r.json() if r.content else {})
            if tok:
                return tok
            last = "login ok but no token field found (check _extract_token)"
            break
        last = f"HTTP {r.status_code}"
        if r.status_code not in (400, 401, 422):
            break
    raise RuntimeError(f"Bloom login failed: {last}")


def _auth_get(session, base, path, token, timeout=20):
    return session.get(f"{base.rstrip('/')}{path}", timeout=timeout,
                       headers={"Authorization": f"Bearer {token}"})


def fetch_json(account, session, token, path, timeout=20):
    """GET an authenticated JSON endpoint; re-login once on a 401 and retry.
    Returns (json, token) — token may be refreshed. Raises on hard failure."""
    base = account.base_url or DEFAULT_BASE
    r = _auth_get(session, base, path, token, timeout)
    if r.status_code == 401:
        token = login(account, session)
        r = _auth_get(session, base, path, token, timeout)
    r.raise_for_status()
    return (r.json() if r.content else None), token


# ── The account payload: buildings, keys, alarms ─────────────────────────────

def buildings(assets_payload):
    """The list of building objects out of GET /api/assets (the account object
    carries them under `buildings`; tolerate a bare list too)."""
    if isinstance(assets_payload, dict):
        return assets_payload.get("buildings") or []
    return assets_payload if isinstance(assets_payload, list) else []


def asset_key(building):
    """A building's stable id for matching to a row: its locid (the slug the
    app and the alarms use), falling back to Mongo _id."""
    v = (building or {}).get("locid") or (building or {}).get("_id")
    return str(v) if v is not None else None


def norm_name(s):
    """Squash a name for forgiving matching: '631 Alan Page Dr' -> '631alanpagedr'."""
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def is_skyline(building):
    return SKYLINE_ACCOUNT in (building.get("accounts") or [])


def alarms_by_locid(alarms_payload):
    """Group the OPEN alarms (end is None) by the building locId they belong to."""
    out = {}
    lst = alarms_payload if isinstance(alarms_payload, list) else \
        (alarms_payload.get("data") or alarms_payload.get("alarms") or []) if isinstance(alarms_payload, dict) else []
    for a in lst:
        if not isinstance(a, dict) or a.get("end") is not None:
            continue
        k = a.get("locId") or a.get("locid")
        if k is not None:
            out.setdefault(str(k), []).append(a)
    return out


# ── Normalise one building -> card ───────────────────────────────────────────

def _sensor_index(building):
    """(building-level pv by type, {pump key: {type: pv}}). Per-apartment space
    sensors (they carry a `name`) are left out of the single-value map."""
    pv, pumps = {}, {}
    for s in building.get("sensors") or []:
        if s.get("name") is not None:          # a named per-apartment sensor
            continue
        t, val = s.get("type"), s.get("pv")
        assoc = s.get("assoc") or {}
        if assoc.get("type") == "equipment" and str(assoc.get("key", "")).startswith("pump"):
            pumps.setdefault(assoc["key"], {})[t] = val
        else:
            pv[t] = val
    return pv, pumps


def _space_count(building):
    """(reporting space sensors, below-setpoint count)."""
    spaces = [s for s in building.get("sensors") or []
              if s.get("type") == "space-t" and s.get("name")]
    below = sum(1 for s in spaces if ((s.get("stats") or {}).get("belowSetpoint")))
    return len(spaces), below


def _points(building):
    """One Bloom building object -> the normalised point dict the card/tests use."""
    pv, pumps = _sensor_index(building)
    pump_list = []
    for key in sorted(pumps):
        p = pumps[key]
        rt = _num(p.get("pmp-rt-day"))          # daily runtime; Bloom reports it in seconds
        pump_list.append({
            "key": key,
            "on": _truthy(p.get("pmp-rs")),        # actually RUNNING (not merely commanded)
            "commanded": _truthy(p.get("pmp-cmd")),
            "run_min": int(rt // 60) if rt is not None else None,
        })
    heat_rt = sum((p["run_min"] or 0) for p in pump_list) if pump_list else None
    n_spaces, n_below = _space_count(building)
    out = {"name": building.get("name"), "locid": building.get("locid"),
           "mode": MODE.get(_int(_num(pv.get("cur-mode")))),
           "cfh": _truthy(pv.get("cfh")),
           "system_status": _int(_num(pv.get("system-status"))),
           "pumps": pump_list, "heat_rt": heat_rt,
           "spaces": n_spaces, "spaces_below": n_below}
    for norm, bkey in POINTS.items():
        out[norm] = _num(pv.get(bkey))
    # Some buildings (e.g. heat-pump plants) have no local outdoor sensor
    # (oat-act); fall back to Bloom's weather-service outdoor (oat-web).
    if out["outside_f"] is None:
        out["outside_f"] = _num(pv.get("oat-web"))
    return out


def loop_hot(supply, retn) -> bool:
    """Portal-wide 'is the loop heating?' test (matches app/legacy.loop_hot)."""
    s = supply if (supply and supply > 0) else None
    r = retn if (retn and retn > 0) else None
    peak = max([v for v in (s, r) if v is not None], default=None)
    if peak is not None and peak >= LOOP_HOT_F:
        return True
    if s is not None and r is not None and s >= LOOP_DRIVE_F and (s - r) >= LOOP_DELTA_F:
        return True
    return False


def _alarm_text(a):
    if isinstance(a, str):
        return a
    if isinstance(a, dict):
        return (a.get("txt") or a.get("title") or a.get("type") or "alarm")
    return str(a)


def _clock(v):
    """Bloom schedule times are HHMM integers (500 -> 5:00, 2200 -> 22:00)."""
    n = _num(v)
    if n is None:
        return None
    n = int(n)
    return f"{n // 100}:{n % 100:02d}"


def _fmt_point(t, raw):
    """Display string for a raw plant-sensor value, formatted by point type
    (Bloom units aren't always numeric). Temperatures get °, humidity/valve/%
    points get %, pressures a plain number, binaries On/Off."""
    if t == "binary":
        n = _num(raw)
        return "On" if n == 1.0 else ("Off" if n == 0.0 else "—")
    n = _num(raw)
    if n is None:
        return "—"
    if t.endswith("-t"):                       # gen-t, ht-supply-t, …
        return f"{n:.1f}°"
    if t.endswith("-h"):                        # humidity
        return f"{n:.0f}%"
    if t in ("vlv-pos", "pmp-speed") or t.endswith("-pct"):
        return f"{n:.0f}%"
    if t.startswith("diff-pr"):                 # differential pressure / setpoint
        return f"{n:.1f}"
    return f"{n:g}"                             # modes, multistate, unknown


# Friendly labels for the raw point dump (every known Bloom sensor type).
POINT_LABELS = {
    "oat-act": "Outside air", "oat-web": "Outside (web)", "oat-sp-act": "Outdoor cutoff (active)",
    "oat-sp-day": "Outdoor cutoff — day", "oat-sp-night": "Outdoor cutoff — night",
    "avg-space-t": "Space avg", "min-space-t": "Space min", "max-space-t": "Space max",
    "ctrl-avg-space-t": "Control space avg", "ind-sp-act": "Indoor setpoint (active)",
    "ind-sp-day": "Indoor setpoint — day", "ind-sp-night": "Indoor setpoint — night",
    "cur-mode": "Current mode", "system-status": "System status", "cfh": "Call for heat",
    "ht-supply-t": "Heating supply", "ht-ret-t": "Heating return", "hw-supply-sp": "Heating supply setpoint",
    "blr-pct": "Boiler command %", "dhw-supply-t": "DHW supply", "dhw-supply-t-lo-sp": "DHW low setpoint",
    "dhw-supply-t-hi-sp": "DHW high setpoint", "dhw-mix-t": "DHW mix", "hw-hit-sp": "HW hi-temp setpoint",
    "hw-lot-sp": "HW lo-temp setpoint", "start-tm-day": "Schedule start", "stop-tm-day": "Schedule stop",
    "space-min-sp-act": "Min-space setpoint (active)",
}
# alarm/flag point types worth listing under "flags" (pv 1 = active)
_FLAG_SUFFIX = ("-alm", "-fail", "-fail-all", "-triggered")


def detail(data: dict) -> dict:
    """Everything pullable from Bloom for one building, from BloomStatus.data
    ({asset:{...}, alarms:[...]}). Used by the per-building page. Derived."""
    data = data or {}
    building = data.get("asset") or {}
    c = card(data)
    pv, pumps = _sensor_index(building)

    # Per-apartment humidity (space-h), joined to the space-t grid by sensor name.
    rh_by_name = {s.get("name"): _num(s.get("pv"))
                  for s in (building.get("sensors") or [])
                  if s.get("type") == "space-h" and s.get("name")}
    apartments = sorted(
        ({"name": s.get("name"), "pv": _num(s.get("pv")),
          "rh": rh_by_name.get(s.get("name")),
          "below": bool((s.get("stats") or {}).get("belowSetpoint"))}
         for s in (building.get("sensors") or []) if s.get("type") == "space-t" and s.get("name")),
        key=lambda a: a["name"] or "")
    n_active = sum(1 for a in apartments if a["pv"] is not None)

    # Named plant/equipment sensors — HX supply/return per tower, the pumps,
    # differential pressure, valve positions, modes, status points. These carry
    # a `name`, so _sensor_index() drops them from the single-value map; surface
    # them raw here so a heat-pump plant's detail page is not mostly blank.
    # A point may appear un-zoned and again per zone (different readings), so keep
    # each (name, zone) pair.
    plant = []
    for s in (building.get("sensors") or []):
        nm, t = s.get("name"), s.get("type")
        if not nm or t in ("space-t", "space-h"):
            continue
        assoc = s.get("assoc") or {}
        plant.append({
            "name": nm, "type": t, "label": POINT_LABELS.get(t, t),
            "disp": _fmt_point(t, s.get("pv")),
            "zone": assoc.get("key") if assoc.get("type") == "zone" else None,
        })
    plant.sort(key=lambda p: (p["name"] or "", p["zone"] or "", p["type"] or ""))

    schedule = {
        "mode": c["mode"], "current_setpoint": c["setpoint_f"],
        "day_setpoint": _num(pv.get("ind-sp-day")), "night_setpoint": _num(pv.get("ind-sp-night")),
        "start": _clock(pv.get("start-tm-day")), "stop": _clock(pv.get("stop-tm-day")),
        "oat_cutoff": _num(pv.get("oat-sp-act")),
        "space_min_sp": _num(pv.get("space-min-sp-act")),
    }

    # Every building-level point (type -> value), split into readings and flags.
    readings, flags = [], []
    for t in sorted(pv):
        v = pv[t]
        if isinstance(v, dict) or v is None:        # no reading / unit-error
            continue
        label = POINT_LABELS.get(t, t)
        if t.endswith(_FLAG_SUFFIX):
            if _num(v) == 1.0:                       # only list active flags
                flags.append({"type": t, "label": label})
        else:
            readings.append({"type": t, "label": label, "value": v})

    alarms = [{"text": _alarm_text(a), "severity": a.get("severity"), "start": a.get("start"),
               "type": a.get("type")} for a in (data.get("alarms") or []) if isinstance(a, dict)]

    return {"name": building.get("name"), "locid": building.get("locid"),
            "card": c, "apartments": apartments, "n_active": n_active,
            "schedule": schedule, "readings": readings, "flags": flags,
            "plant": plant, "alarms": alarms}


def _boilers_firing(building) -> bool:
    """True when a boiler is firing — any boiler run-status on, or a non-zero
    boiler command %. Used for the Live wall's Fire column on Skyline rows."""
    pct, running = None, False
    for s in building.get("sensors") or []:
        t = s.get("type")
        if t == "blr-pct":
            pct = _num(s.get("pv"))
        elif t == "blr-rs" and _num(s.get("pv")) == 1.0:
            running = True
    return running or bool(pct and pct > 0)


def card(data: dict) -> dict:
    """The wall card from one building's stored reading (BloomStatus.data =
    {asset:{...building...}, alarms:[...open alarms...]}). Derived, never stored."""
    data = data or {}
    building = data.get("asset") or {}
    alarms = data.get("alarms") or []
    p = _points(building)

    pump_on = any(x["on"] for x in p["pumps"])
    loop = loop_hot(p["supply_f"], p["return_f"])
    # "Heating" means heat is actually moving — a hot loop or a running pump —
    # NOT merely a call for heat (a building can call and fail to respond).
    heating = loop or pump_on
    # The thing to chase, in the owner's own terms: calling for heat while the
    # loop never got hot and no pump is running.
    cold_call = bool(p["cfh"]) and not heating
    fault = cold_call or any((a.get("type") in FAULT_ALARMS) for a in alarms)

    if fault:
        tone = "crit"
    elif heating:
        tone = "heat"
    elif alarms:
        tone = "warn"
    else:
        tone = "ok"

    if heating:
        status = "Heating" + (" (call)" if p["cfh"] else "")
    elif cold_call:
        status = "Call for heat — not responding"
    else:
        status = "Satisfied"

    return {
        **p,
        "heating": heating, "loop_hot": loop, "pump_on": pump_on,
        "firing": _boilers_firing(building),
        "cold_call": cold_call, "fault": fault, "tone": tone, "status": status,
        "alarms": [_alarm_text(a) for a in alarms],
        "n_alarms": len(alarms),
    }


# ── Poller ───────────────────────────────────────────────────────────────────

class BloomPoller:
    def __init__(self):
        self._thread = None

    def start(self, app) -> None:
        if not app.config.get("BLOOM_POLLER_ENABLED", True):
            log.info("Bloom poller disabled (BLOOM_POLLER_ENABLED=0)")
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="bloom-poller", daemon=True)
        self._thread.start()
        log.info("Bloom poller started")

    def _run(self, app) -> None:
        interval = app.config.get("BLOOM_POLL_INTERVAL_SECONDS", 60)
        while True:
            try:
                self._run_cycle(app)
            except Exception as e:
                log.warning("Bloom poller cycle failed: %s", e)
            time.sleep(interval)

    def _run_cycle(self, app) -> None:
        from app.models import BloomAccount
        with app.app_context():
            conn = db.engine.connect()
            try:
                if not conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _LOCK_KEY}).scalar():
                    return
                for acct in BloomAccount.query.filter(BloomAccount.is_active.is_(True)).all():
                    try:
                        poll_account(app, acct)
                    except Exception as e:
                        log.warning("Bloom poll failed for account %s (%s): %s", acct.id, acct.label, e)
                try:
                    from app import alerts
                    if hasattr(alerts, "check_bloom"):
                        alerts.check_bloom(app)
                except Exception as e:
                    db.session.rollback()
                    log.warning("Bloom alert check failed: %s", e)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
                conn.close()


def poll_account(app, account):
    """One login + one /api/assets + one /api/alarms for the whole account,
    then upsert a BloomStatus per active asset row. App context assumed."""
    from app.models import BloomAsset, BloomStatus
    timeout = app.config.get("BLOOM_POLL_TIMEOUT_SECONDS", 20)
    started = time.monotonic()
    session = requests.Session()

    token = account.token or login(account, session, timeout)
    assets_json, token = fetch_json(account, session, token, "/api/assets", timeout)
    try:
        alarms_json, token = fetch_json(account, session, token, "/api/alarms", timeout)
    except Exception as e:
        log.warning("Bloom /api/alarms failed for %s: %s", account.label, e)
        alarms_json = []

    if token and token != account.token:
        account.token = token
        account.token_at = _dt.datetime.utcnow()
        db.session.commit()

    by_key, by_name = {}, {}
    for b in buildings(assets_json):
        k = asset_key(b)
        if k is not None:
            by_key[k] = b
        nm = norm_name(b.get("name"))
        if nm:
            by_name[nm] = b
    alarms = alarms_by_locid(alarms_json)
    ms = int((time.monotonic() - started) * 1000)
    now = _dt.datetime.utcnow()

    rows = (BloomAsset.query
            .filter(BloomAsset.account_id == account.id,
                    BloomAsset.is_active.is_(True),
                    BloomAsset.deleted_at.is_(None))
            .all())
    for row in rows:
        b = by_key.get(str(row.asset_key)) or by_name.get(norm_name(row.name))
        st = db.session.get(BloomStatus, row.id) or BloomStatus(asset_id=row.id)
        st.polled_at = now
        st.response_ms = ms
        if b is None:
            st.ok = False
            st.error = "building not present in /api/assets"
        else:
            st.ok = True
            st.error = None
            st.last_ok_at = now
            st.data = {"asset": b, "alarms": alarms.get(str(asset_key(b)), [])}
        db.session.add(st)
    db.session.commit()
    log.info("Bloom poll %s: %d buildings seen, %d rows updated", account.label, len(by_key), len(rows))


bloom_poller = BloomPoller()
