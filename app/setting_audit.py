"""
app/setting_audit.py — who changed which setting of which building, and when

ONE ledger (models.SettingChange) for every kind of box the portal watches.
A row is ONE setting of ONE building going from one value to another. Two
things feed it:

  * a change a person sends THROUGH the portal — recorded at once with their
    name and the reason they gave (record_portal);
  * a change made ANYWHERE ELSE — the vendor's own site, a phone app, the
    keypad on the wall. Nobody tells the portal about those, so each poll the
    settings just read are compared with the ones read before, and a
    difference is recorded as changed "outside the portal" (observe). When the
    difference is simply a portal change arriving at the controller, the
    portal's own row is marked as seen there instead of a second row written.

What keeps it small: only SETTINGS are compared (a short fixed list per kind
of box — never a reading, never a value the controller computes for itself),
and a row is written only when one actually changes. A building whose
settings nobody touches adds nothing, ever.

Kinds of box:
  bloom     Bloom / Empowered heating buildings — bloom_snapshot() below; the
            previous poll's building object is the "before", so nothing extra
            is stored
  legacy    the 2010 oBIX boxes — their settings live in eight oBIX objects
            the status poll does not read, so each box has them read in its
            turn about once an hour (due(), legacy.SETTINGS_PATHS) and
            compared with its stored snapshot (track())
  echotherm EchoTherm controllers — GET /api/fleet/settings with the fleet
            token returns every setting as it stands (credentials left out)
            and the controller's own note of who saved what lately; read
            every few minutes (fleet_poller.audit_settings) and compared
            with the stored snapshot, the controller's "who" filling in the
            name a change made at the controller would otherwise lack

ADDING A KIND OF BOX (an EchoTherm controller, the next vendor): give it
  1. a snapshot — a flat {setting key: value} dict of what it holds now,
     settings only;
  2. a describe(key, value, snapshot) -> (label, text) for people;
  3. one call in its poller: track(kind, its row id, building_id, name,
     snapshot, describe, now).
Nothing else changes: the ledger, the Changes page and the building cards
take every kind. A kind the portal can also WRITE to calls record_portal()
when a person sends a change, so the row carries their name.

The snapshot/diff half is pure; observe()/record_portal()/recent() touch the
database and assume an app context.
"""
import datetime as _dt
import re

from app import bloom_settings

MATCH_MIN = 20          # a difference seen within this long of a portal change to the same value is that change

# Bloom settings that are watched but not (yet) changeable from the portal.
BLOOM_WATCH_ONLY = {
    "dhw-supply-t-lo-sp": "Hot water too-cold alarm limit",
    "dhw-supply-t-hi-sp": "Hot water too-hot alarm limit",
}


def _num(v):
    if v is None or isinstance(v, (bool, dict, list)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def same(a, b) -> bool:
    """Two stored values that mean the same thing (72 and 72.0 do)."""
    na, nb = _num(a), _num(b)
    if na is not None and nb is not None:
        return na == nb
    return a == b


# ── Bloom ────────────────────────────────────────────────────────────────────

def bloom_snapshot(building: dict) -> dict:
    """{setting key: value} for one Bloom building object: the settings of its
    Schedule screen plus the watch-only ones. A setting Bloom lists more than
    once, or has no readable value for, is left out — it cannot be compared."""
    seen = {}
    for s in (building or {}).get("sensors") or []:
        t = s.get("type")
        if t in bloom_settings.BY_KEY or t in BLOOM_WATCH_ONLY:
            seen.setdefault(t, []).append(s.get("pv"))
    return {t: vals[0] for t, vals in seen.items() if len(vals) == 1 and _num(vals[0]) is not None}


def bloom_label(key) -> str:
    return bloom_settings.BY_KEY[key]["label"] if key in bloom_settings.BY_KEY else BLOOM_WATCH_ONLY.get(key, key)


def bloom_text(key, value, snapshot=None) -> str:
    """A Bloom setting's value as a person reads it."""
    if key in bloom_settings.BY_KEY:
        return bloom_settings.text(key, value, (snapshot or {}).get("space-min-threshold-type"))
    n = _num(value)
    return f"{n:g}°" if n is not None else "—"


def diff(before: dict, after: dict) -> list:
    """[(key, old, new)] for every setting present in BOTH snapshots whose value
    differs. A setting missing from either side is not a change: a point that
    dropped out of one poll must not read as "set to nothing"."""
    return [(k, before[k], after[k]) for k in sorted(after) if k in before and not same(before[k], after[k])]


def bloom_changes(old_building: dict, new_building: dict) -> list:
    """What changed between two polls of one Bloom building, ready for
    observe(): [{key, label, old, new, old_text, new_text}]."""
    before, after = bloom_snapshot(old_building), bloom_snapshot(new_building)
    return [{"key": k, "label": bloom_label(k), "old": o, "new": n,
             "old_text": bloom_text(k, o, before), "new_text": bloom_text(k, n, after)}
            for k, o, n in diff(before, after)]


def bloom_portal_changes(log: list) -> list:
    """A portal settings write's logged points (bloom_settings.plan()['log']) in
    the same shape."""
    tt = next((c.get("new") for c in log or [] if c.get("type") == "space-min-threshold-type"), None)
    snap = {"space-min-threshold-type": tt} if tt is not None else {}
    return [{"key": c["type"], "label": bloom_label(c["type"]), "old": c.get("old"), "new": c.get("new"),
             "old_text": bloom_text(c["type"], c.get("old"), snap), "new_text": bloom_text(c["type"], c.get("new"), snap)}
            for c in log or [] if c.get("type")]


# ── the ledger ───────────────────────────────────────────────────────────────

def record_portal(source, source_id, building_id, device, changes, by, note, landed, now=None) -> int:
    """A change a person sent through the portal and the vendor accepted: one
    row per setting, with their name and reason. `landed` = a read-back already
    found the values in place. Adds to the session; the caller commits."""
    from app.extensions import db
    from app.models import SettingChange
    now = now or _dt.datetime.utcnow()
    # A poll can beat this call by a moment and file the change as made
    # "outside": such a row, minutes old at most, is this very change — it
    # takes the person's name instead of a second row being written.
    early = (SettingChange.query
             .filter(SettingChange.source == source, SettingChange.source_id == source_id,
                     SettingChange.origin == "outside", SettingChange.at >= now - _dt.timedelta(minutes=2))
             .all())
    for c in changes:
        beat = next((r for r in early if r.key == c["key"] and same(r.new, c["new"])), None)
        if beat is not None:
            beat.origin, beat.by, beat.note = "portal", by, note
            continue
        db.session.add(SettingChange(
            at=now, building_id=building_id, source=source, source_id=source_id, device=device,
            key=c["key"], label=c["label"], old=c["old"], new=c["new"],
            old_text=c["old_text"], new_text=c["new_text"],
            origin="portal", by=by, note=note, seen_at=now if landed else None))
    return len(changes)


def mark_seen(source, source_id, keys, now=None) -> None:
    """A read-back found a portal change in place: stamp its rows. Adds to the
    session; the caller commits."""
    from app.models import SettingChange
    now = now or _dt.datetime.utcnow()
    rows = (SettingChange.query
            .filter(SettingChange.source == source, SettingChange.source_id == source_id,
                    SettingChange.origin == "portal", SettingChange.seen_at.is_(None),
                    SettingChange.key.in_(list(keys) or [""]),
                    SettingChange.at >= now - _dt.timedelta(minutes=MATCH_MIN))
            .all())
    for r in rows:
        r.seen_at = now


def observe(source, source_id, building_id, device, changes, now=None) -> int:
    """Differences the poller just saw on one box. Each is either a recent
    portal change to that very value arriving (its row is marked seen) or a
    change made outside the portal (a new row). Returns how many NEW rows were
    added. Adds to the session; the caller commits."""
    from app.extensions import db
    from app.models import SettingChange
    if not changes:
        return 0
    now = now or _dt.datetime.utcnow()
    since = now - _dt.timedelta(minutes=MATCH_MIN)
    recent_portal = (SettingChange.query
                     .filter(SettingChange.source == source, SettingChange.source_id == source_id,
                             SettingChange.origin == "portal", SettingChange.at >= since)
                     .order_by(SettingChange.at.desc()).all())
    added = 0
    for c in changes:
        mine = next((r for r in recent_portal if r.key == c["key"] and same(r.new, c["new"])), None)
        if mine is not None:
            if mine.seen_at is None:
                mine.seen_at = now
            continue
        db.session.add(SettingChange(
            at=now, building_id=building_id, source=source, source_id=source_id, device=device,
            key=c["key"], label=c["label"], old=c["old"], new=c["new"],
            old_text=c["old_text"], new_text=c["new_text"],
            origin="outside", by=(c.get("by") or None), note=None, seen_at=now))
        added += 1
    return added


SOURCES = {"bloom": "Bloom", "legacy": "2010 box", "echotherm": "EchoTherm"}
OUTSIDE = {"bloom": "outside the portal (in Bloom, or at the controller)",
           "legacy": "outside the portal (the vendor's app, or at the box)",
           "echotherm": "outside the portal (at the controller)"}


def who(row) -> str:
    """'Pat Viewer' / 'outside the portal (in Bloom, or at the controller)'."""
    if row.origin == "portal":
        return row.by or "someone, through the portal"
    if row.by:                                         # the box itself said who
        return f"{row.by}, at the controller"
    return OUTSIDE.get(row.source, "outside the portal")


def recent(building_ids=None, source=None, source_id=None, origin=None, by=None, since=None, limit=200):
    """Ledger rows, newest first. `building_ids` None = every building."""
    from app.models import SettingChange
    q = SettingChange.query
    if building_ids is not None:
        q = q.filter(SettingChange.building_id.in_(list(building_ids) or [-1]))
    if source:
        q = q.filter(SettingChange.source == source)
    if source_id is not None:
        q = q.filter(SettingChange.source_id == source_id)
    if origin:
        q = q.filter(SettingChange.origin == origin)
    if by:
        q = q.filter(SettingChange.by == by)
    if since is not None:
        q = q.filter(SettingChange.at >= since)
    return q.order_by(SettingChange.at.desc(), SettingChange.id.desc()).limit(limit).all()


# ── any kind of box: compare with the stored snapshot ────────────────────────

def due(source, ids, every_min, now=None, limit=None) -> set:
    """Which of these boxes should have their settings read now: the ones
    never read, or last read `every_min` minutes ago or more — the longest
    waiting first, at most `limit` of them, so that no poll cycle carries
    every box. Decided from the stored snapshots' times, which every worker
    process and every restart share. every_min 0 switches the reading off.
    App context assumed."""
    if not every_min or every_min <= 0:
        return set()
    from app.extensions import db
    from app.models import SettingSnapshot
    now = now or _dt.datetime.utcnow()
    read_at = dict(db.session.query(SettingSnapshot.source_id, SettingSnapshot.at)
                   .filter(SettingSnapshot.source == source).all())
    cut = now - _dt.timedelta(minutes=every_min)
    waiting = sorted((i for i in ids if read_at.get(i) is None or read_at[i] <= cut),
                     key=lambda i: (read_at.get(i) or _dt.datetime.min, i))
    return set(waiting[:limit] if limit else waiting)


def share(n_boxes, every_min, interval_s) -> int:
    """How many boxes' settings one poll cycle should read so that all of
    them come round once every `every_min` minutes (a little over, so the
    queue never grows)."""
    if not every_min or every_min <= 0 or n_boxes <= 0:
        return 0
    return int(n_boxes * max(1, interval_s) // (every_min * 60)) + 2


def touch(source, source_id, now=None) -> None:
    """A settings read that was due and brought nothing back: note the
    attempt, so the box goes to the back of the queue instead of holding a
    place in every cycle. The stored settings are kept as they are. The
    caller commits."""
    from app.extensions import db
    from app.models import SettingSnapshot
    now = now or _dt.datetime.utcnow()
    row = db.session.get(SettingSnapshot, (source, source_id))
    if row is None:
        db.session.add(SettingSnapshot(source=source, source_id=source_id, at=now, data={}))
    else:
        row.at = now


def track(source, source_id, building_id, device, snapshot, describe, now=None, who=None) -> int:
    """One box's settings as just read: compare them with the snapshot stored
    for it, record what changed (observe), and store the new snapshot. The
    first sight of a box is only a baseline. A setting missing from this read
    keeps its stored value, so a partial read never looks like a change — now
    or next time. `who(key)` may name who made a change AT the box, where the
    box itself knows. Returns how many new ledger rows. The caller commits."""
    from app.extensions import db
    from app.models import SettingSnapshot
    now = now or _dt.datetime.utcnow()
    if not snapshot:
        return 0
    row = db.session.get(SettingSnapshot, (source, source_id))
    if row is None:
        db.session.add(SettingSnapshot(source=source, source_id=source_id, at=now, data=dict(snapshot)))
        return 0
    before = row.data or {}                            # {} = only a failed attempt so far: this read is the baseline
    changes = []
    for k, o, n in diff(before, snapshot):
        label, old_text = describe(k, o, before)
        changes.append({"key": k, "label": label, "old": o, "new": n,
                        "old_text": old_text, "new_text": describe(k, n, snapshot)[1],
                        "by": who(k) if who else None})
    added = observe(source, source_id, building_id, device, changes, now)
    row.data, row.at = {**before, **snapshot}, now
    return added


# ── the 2010 oBIX boxes ──────────────────────────────────────────────────────

# Fields of the settings objects that are not settings: clock stamps, and
# values the box (or a feed into it) moves by itself.
LEGACY_SKIP = {"serverTime", "extOutsideTemp", "leadBoiler"}

_LEGACY_LABELS = {
    "emsSettings.insideDaySetpoint": "Indoor setpoint, day",
    "emsSettings.insideNightSetpoint": "Indoor setpoint, night",
    "emsSettings.outsideDaySetpoint": "Outdoor cutoff, day",
    "emsSettings.outsideNightSetpoint": "Outdoor cutoff, night",
    "emsSettings.dayStartHour": "Day period starts",
    "emsSettings.nightStartHour": "Night period starts",
    "emsSettings.winterStartMonth": "Heating season starts, month",
    "emsSettings.winterStartDay": "Heating season starts, day",
    "emsSettings.summerStartMonth": "Heating season ends, month",
    "emsSettings.summerStartDay": "Heating season ends, day",
    "emsSettings.ditMode": "Indoor-target mode",
    "emsSettings.mixHighTempSetpoint": "Mixing valve high limit",
    "emsSettings.mixLowTempSetpoint": "Mixing valve low limit",
    "emsSettings.hotWaterAlarmSet": "Hot water alarm limit",
    "emsSettings.stackAlarmSet": "Stack temperature alarm limit",
    "emsSettings.lowOilAlarmSet": "Low oil alarm limit",
    "emsSettings.freezeProtectionTemp": "Freeze protection temperature",
    "emsSettings.defaultOutsideTemp": "Outside temperature used when the sensor fails",
    "emsSettings.heatPGain": "Heat control gain (P)",
    "emsSettings.heatIGain": "Heat control gain (I)",
    "emsConfig.heatControlType": "Heating system type",
    "emsConfig.fuelType": "Fuel type",
    "emsConfig.numBoilers": "Number of boilers",
    "aquastat.aquastatRange": "Aquastat range",
    "DTs.amBoostMult": "Morning boost multiplier",
    "DTs.offTime": "Heat cycle off time",
}
# families of numbered fields: (object, name before the number) -> (label, what the number is)
_LEGACY_FAMILIES = {
    ("DITs", "weekdayDITHour"): ("Indoor target, weekdays", "hour"),
    ("DITs", "saturdayDITHour"): ("Indoor target, Saturday", "hour"),
    ("DITs", "sundayDITHour"): ("Indoor target, Sunday", "hour"),
    ("DITs", "weekdayCoolDITHour"): ("Cooling target, weekdays", "hour"),
    ("DITs", "saturdayCoolDITHour"): ("Cooling target, Saturday", "hour"),
    ("DITs", "sundayCoolDITHour"): ("Cooling target, Sunday", "hour"),
    ("aquastat", "daqHour"): ("Aquastat target", "hour"),
    ("DTs", "dayDTforT5x"): ("Heat cycle table, day", "step"),
    ("DTs", "nightDTforT5x"): ("Heat cycle table, night", "step"),
    ("hotWaterHeat", "daySetpointforT5x"): ("Supply water table, day", "step"),
    ("hotWaterHeat", "nightSetpointforT5x"): ("Supply water table, night", "step"),
    ("vacuumSteam", "dayValveStepsforT5x"): ("Steam valve table, day", "step"),
    ("vacuumSteam", "nightValveStepsforT5x"): ("Steam valve table, night", "step"),
    ("cfhtable", "numSensReqForApts"): ("Sensors needed to call for heat, table", "entry"),
    ("emsConfig", "relayAssignment"): ("Relay assignment", "relay"),
    ("emsConfig", "digitalInputAssign"): ("Digital input assignment", "input"),
    ("emsConfig", "analogInputAssign"): ("Analog input assignment", "input"),
}
_LEGACY_OBJECTS = {"emsSettings": "settings", "emsConfig": "configuration", "DITs": "indoor targets",
                   "DTs": "heat cycle", "hotWaterHeat": "hot-water heat", "vacuumSteam": "vacuum steam",
                   "aquastat": "aquastat", "cfhtable": "call-for-heat table"}
_DEGREES = ("setpoint", "temp", "dithour", "daqhour", "alarmset")
_HOURS = ("emsSettings.dayStartHour", "emsSettings.nightStartHour")


def legacy_snapshot(objects: dict) -> dict:
    """{'emsSettings.insideDaySetpoint': '72', ...} from the settings objects
    one read brought back ({object name: {field: value}})."""
    return {f"{obj}.{k}": v for obj, fields in (objects or {}).items() for k, v in (fields or {}).items()
            if k not in LEGACY_SKIP and v is not None}


def _hour_words(h) -> str:
    return f"{(h % 12) or 12} {'AM' if h % 24 < 12 else 'PM'}"


def _split_camel(name) -> str:
    out = []
    for ch in name:
        if out and (ch.isupper() or ch.isdigit()) and not (out[-1].isupper() or out[-1].isdigit() or out[-1] == " "):
            out.append(" ")
        out.append(ch)
    words = "".join(out)
    return words[:1].upper() + words[1:]


def legacy_describe(key, value, snapshot=None) -> tuple:
    """(label, value as a person reads it) for one 2010 box setting. The
    settings people look for have their own words; a numbered family reads
    as its table and place ('Indoor target, weekdays, 5 AM'); anything else
    is the box's own field name spelled out."""
    obj, _, field = key.partition(".")
    label = _LEGACY_LABELS.get(key)
    stem, idx = field.rstrip("0123456789"), field[len(field.rstrip("0123456789")):]
    if label is None and idx and (obj, stem) in _LEGACY_FAMILIES:
        fam, what = _LEGACY_FAMILIES[(obj, stem)]
        label = f"{fam}, {_hour_words(int(idx))}" if what == "hour" else f"{fam}, {what} {int(idx) + 1}"
    if label is None:
        label = f"{_split_camel(field)} ({_LEGACY_OBJECTS.get(obj, obj)})"
    n = _num(value)
    if n is None:
        text = {"true": "on", "false": "off"}.get(str(value).strip().lower(), str(value))
    elif key in _HOURS and n == int(n) and 0 <= n <= 24:
        text = _hour_words(int(n))
    elif any(w in (stem or field).lower() for w in _DEGREES):
        text = f"{n:g}°"
    else:
        text = f"{n:g}"
    return label[:120], text[:120]


# ── EchoTherm controllers ────────────────────────────────────────────────────

# The controller already reduces credentials to "set" / "not set" before they
# leave it. The portal applies the same rule to whatever arrives, by NAME, so
# a credential a controller's software failed to recognise is still never
# stored here (2026-11-21: a LoRaWAN `app_key` got past a rule that knew
# api_key and auth_key but not "key" as a word).
_SECRET_PARTS = ("password", "passwd", "secret", "token", "hash", "digest", "psk", "passphrase")
_SECRET_WORDS = {"key", "keys"}


def secret_name(key) -> bool:
    """Does this setting's name say it holds a credential?"""
    last = re.sub(r"\[[^\]]*\]", "", str(key)).rsplit(".", 1)[-1].lower()
    return any(p in last for p in _SECRET_PARTS) or bool(_SECRET_WORDS & set(re.split(r"[^a-z0-9]+", last)))


def _no_secret(key, value):
    if secret_name(key) and value not in ("set", "not set"):
        return "set" if value not in (None, "", 0, False) else "not set"
    return value


def echotherm_snapshot(settings: dict) -> dict:
    """The flat {key: value} the ledger compares, from a controller's
    GET /api/fleet/settings 'settings' (already flat, but a list is still a
    list there). A list is spread out so that ONE changed hour of a schedule
    is one row, not "the list changed": 'weekly_schedule.weekday[5]', and for
    a list of records 'sensor_registry[slot 3].name'. Its length is a setting
    of its own, so an entry added or removed shows too."""
    out = {}
    for key, value in (settings or {}).items():
        if not isinstance(value, list):
            if not isinstance(value, dict):
                out[key] = _no_secret(key, value)
            continue
        out[f"{key}.count"] = len(value)
        for i, item in enumerate(value):
            if isinstance(item, dict):
                tag = f"slot {item['slot']}" if "slot" in item else str(item.get("name") or item.get("user") or i)
                for f, v in item.items():
                    if not isinstance(v, (dict, list)):
                        out[f"{key}[{tag}].{f}"] = _no_secret(f, v)
            elif not isinstance(item, list):
                out[f"{key}[{i}]"] = _no_secret(key, item)
    return out


def echotherm_describe(key, value, snapshot=None) -> tuple:
    """(label, value in words) for one controller setting: the controller's
    own name for it, spelled out ('Weekly schedule › weekday › 6th')."""
    parts = []
    for part in key.split("."):
        name, _, idx = part.partition("[")
        words = name.replace("_", " ").strip()
        if idx:
            idx = idx.rstrip("]")
            words += f" › {int(idx) + 1}" if idx.isdigit() else f" › {idx}"
        parts.append(words)
    if parts and parts[-1] == "count":
        parts[-1] = "number of entries"
    label = " › ".join(p for p in parts if p)
    label = label[:1].upper() + label[1:]
    if value is None:
        text = "—"
    elif isinstance(value, bool):
        text = "on" if value else "off"
    elif isinstance(value, (int, float)):
        text = f"{value:g}"
    else:
        text = str(value) or "(empty)"
    return label[:120], text[:120]


def echotherm_who(changes: list):
    """who(key) for track(), from the controller's own list of recent saves
    ([{ts, by, changes: [{key, ...}]}], oldest first): the latest save that
    touched the setting names who made it. The controller lists a whole list
    under one key; the ledger's 'key[5]' / 'key[slot 3].name' / 'key.count'
    all belong to it."""
    latest = {}
    for save in changes or []:
        for c in (save.get("changes") or []) if isinstance(save, dict) else []:
            if isinstance(c, dict) and c.get("key"):
                latest[c["key"]] = save.get("by")

    def who(key):
        base = key.split("[", 1)[0]
        if base.endswith(".count") and base[:-6] in latest:
            base = base[:-6]
        return latest.get(key) or latest.get(base)
    return who
