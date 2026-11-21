"""
app/bloom_settings.py — a Bloom heating building's settings, changed from the portal

Bloom's own "Schedule" screen (/property/<locid>/schedule) lets an admin change
a heating building's day and night setpoints, the outdoor temperature above
which heating stays off, when the day period starts and ends, the heating
season's dates, and the "secondary call for heat" rule. It saves them with the
same call the fan pages already use — PUT /api/points/<locid> with the changed
point objects (app/bloom.put_points) — but WITHOUT the `bacnet` flag the fan
screen adds. This module works out, from the LIVE building object, exactly
which points a requested change would touch. It is pure: nothing here talks to
Bloom or the database (routes.skyline_settings does both).

How Bloom stores the values (read from its page code, 2026-11-20):
  * temperatures and counts: whole numbers
  * start-tm-day / stop-tm-day: the hour times 100 plus HUNDREDTHS of an hour
    (500 = 5:00, 550 = 5:30, 2200 = 10:00 PM) — not HHMM
  * wint-start-day / wint-end-day: the day of a leap year (275 = October 1,
    152 = May 31), so a date keeps its number every year
  * space-min-threshold-type: 1 = a percentage of the spaces, 0 = a number of
    spaces; space-min-sp-enabled: 1 = the rule is on

The limits are Bloom's own (the min/max of its input boxes). Two guards are
added that Bloom's screen does not have: the day period must start before it
ends, and a setting Bloom lists more than once on a building is not offered
here at all — there is no telling which copy the controller obeys.
"""
import datetime as _dt
import hashlib
import json

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December"]
_LEAP = 2020                       # the leap year Bloom counts a day-of-year in
STALE_MIN = 120                    # a building whose freshest point is older than this is not reporting

# key = Bloom's point type. kind: deg / clock / date / flag / choice / count.
SETTINGS = [
    {"key": "start-tm-day", "label": "Day period starts", "group": "Day and night", "kind": "clock",
     "help": "From this time the day setpoints apply."},
    {"key": "stop-tm-day", "label": "Night period starts", "group": "Day and night", "kind": "clock",
     "help": "From this time until the next day start the night setpoints apply."},
    {"key": "ind-sp-day", "label": "Indoor setpoint, day", "group": "Day and night", "kind": "deg", "lo": 50, "hi": 80,
     "help": "The building calls for heat while its average apartment temperature is below this."},
    {"key": "ind-sp-night", "label": "Indoor setpoint, night", "group": "Day and night", "kind": "deg", "lo": 50, "hi": 80,
     "help": "The same, during the night period."},
    {"key": "oat-sp-day", "label": "Outdoor cutoff, day", "group": "Day and night", "kind": "deg", "lo": 0, "hi": 70,
     "help": "Heating stays off while it is warmer than this outside."},
    {"key": "oat-sp-night", "label": "Outdoor cutoff, night", "group": "Day and night", "kind": "deg", "lo": 0, "hi": 70,
     "help": "The same, during the night period."},
    {"key": "wint-start-day", "label": "Heating season starts", "group": "Heating season", "kind": "date", "help": ""},
    {"key": "wint-end-day", "label": "Heating season ends", "group": "Heating season", "kind": "date", "help": ""},
    {"key": "space-min-sp-enabled", "label": "Secondary call for heat", "group": "Secondary call for heat", "kind": "flag",
     "help": "Calls for heat when enough apartments are below a minimum, even if the building average is satisfied."},
    {"key": "space-min-threshold-type", "label": "Triggered by", "group": "Secondary call for heat", "kind": "choice",
     "choices": [(1, "a percentage of the apartments"), (0, "a number of apartments")], "help": ""},
    {"key": "space-min-threshold", "label": "Threshold", "group": "Secondary call for heat", "kind": "count",
     "lo": 1, "hi": 100, "help": "How many apartments (or what percentage) must be below the minimum."},
    {"key": "space-min-sp-day", "label": "Apartment minimum, day", "group": "Secondary call for heat", "kind": "deg",
     "lo": 50, "hi": 90, "help": ""},
    {"key": "space-min-sp-night", "label": "Apartment minimum, night", "group": "Secondary call for heat", "kind": "deg",
     "lo": 50, "hi": 90, "help": ""},
]
BY_KEY = {s["key"]: s for s in SETTINGS}
GROUPS = list(dict.fromkeys(s["group"] for s in SETTINGS))


def _num(v):
    if v is None or isinstance(v, (bool, dict, list)):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _whole(v):
    """A stored value as a whole number, or None when it is not one."""
    n = _num(v)
    return int(n) if n is not None and n == int(n) else None


# ── Bloom's encodings ────────────────────────────────────────────────────────

def clock_decode(raw):
    """Bloom's time number -> (hour, minute): 550 -> (5, 30)."""
    n = _num(raw)
    if n is None or n < 0:
        return None
    h, m = int(n // 100), int(round((n % 100) * 0.6))
    if m == 60:
        h, m = h + 1, 0
    return (h, m) if h <= 24 else None


def clock_encode(hour, minute):
    """(5, 30) -> 550."""
    return hour * 100 + int(round(minute / 0.6))


def clock_text(raw):
    t = clock_decode(raw)
    if t is None:
        return "—"
    h, m = t
    return f"{(h % 12) or 12}:{m:02d} {'AM' if h % 24 < 12 else 'PM'}"


def clock_field(raw):
    """The value an <option> carries for a time: 'HH:MM'."""
    t = clock_decode(raw)
    return f"{t[0]:02d}:{t[1]:02d}" if t else ""


def date_decode(raw):
    """Bloom's day of a leap year -> (month, day): 275 -> (10, 1)."""
    n = _whole(raw)
    if n is None or not 1 <= n <= 366:
        return None
    d = _dt.date(_LEAP, 1, 1) + _dt.timedelta(days=n - 1)
    return d.month, d.day


def date_encode(month, day):
    """(10, 1) -> 275. Raises ValueError for a date that does not exist."""
    return _dt.date(_LEAP, month, day).timetuple().tm_yday


def date_text(raw):
    d = date_decode(raw)
    return f"{MONTHS[d[0] - 1]} {d[1]}" if d else "—"


def text(key, raw, threshold_type=None):
    """A stored value as a person reads it."""
    s = BY_KEY[key]
    kind = s["kind"]
    if raw is None or _num(raw) is None:
        return "—"
    if kind == "clock":
        return clock_text(raw)
    if kind == "date":
        return date_text(raw)
    n = _num(raw)
    if kind == "deg":
        return f"{n:g}°"
    if kind == "flag":
        return "on" if n == 1 else "off"
    if kind == "choice":
        return dict(s["choices"]).get(int(n), f"{n:g}")
    if kind == "count":
        tt = _num(threshold_type)
        return f"{n:g}%" if tt == 1 else (f"{n:g} apartment{'s' if n != 1 else ''}" if tt == 0 else f"{n:g}")
    return f"{n:g}"


def clock_options(current_raw=None):
    """Every half hour of the day as (field value, label), plus the stored time
    when it is not on a half hour — so the form can always show what is there."""
    opts = [(f"{h:02d}:{m:02d}", clock_text(clock_encode(h, m))) for h in range(24) for m in (0, 30)]
    cur = clock_field(current_raw)
    if cur and cur not in {o[0] for o in opts}:
        opts.append((cur, clock_text(current_raw)))
        opts.sort()
    return opts


# ── the building's setting points ────────────────────────────────────────────

def points(building: dict) -> dict:
    """{setting key: (point or None, why it is not offered or None)} from one
    live building object. A setting is offered only when Bloom lists it exactly
    once, with the id a write needs."""
    found = {}
    for s in (building or {}).get("sensors") or []:
        if s.get("type") in BY_KEY:
            found.setdefault(s["type"], []).append(s)
    out = {}
    for d in SETTINGS:
        got = found.get(d["key"], [])
        if not got:
            out[d["key"]] = (None, "this building has no such setting in Bloom")
        elif len(got) > 1:
            out[d["key"]] = (None, f"Bloom lists it {len(got)} times on this building, so it has to be changed in Bloom")
        elif not got[0].get("_id"):
            out[d["key"]] = (None, "Bloom gave no id for it")
        else:
            out[d["key"]] = (got[0], None)
    return out


def offered(building: dict) -> int:
    """How many settings of this building can be changed from the portal."""
    return sum(1 for pt, _why in points(building).values() if pt is not None)


def _ts(v):
    try:
        return _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00")).astimezone(_dt.timezone.utc).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def heard_age(building: dict, now=None):
    """Minutes since Bloom last read ANY point of this building, or None."""
    now = now or _dt.datetime.utcnow()
    stamps = [t for t in (_ts(s.get("pollTs")) for s in (building or {}).get("sensors") or []) if t is not None]
    return max(0.0, (now - max(stamps)).total_seconds() / 60) if stamps else None


# ── what a person typed -> values ────────────────────────────────────────────

def parse(form) -> tuple:
    """(wanted, errors) from a submitted form. `wanted` holds Bloom's raw value
    for every setting the form carried something readable for; a field left
    out or blank is simply not in it (= leave that setting alone). Limits are
    checked later, against the building, only for values that change."""
    wanted, errors = {}, {}
    for s in SETTINGS:
        key, kind = s["key"], s["kind"]
        if kind == "date":
            m, d = (form.get(f"{key}.m") or "").strip(), (form.get(f"{key}.d") or "").strip()
            if not m and not d:
                continue
            try:
                wanted[key] = date_encode(int(m), int(d))
            except (TypeError, ValueError):
                errors[key] = "not a date"
            continue
        v = (form.get(key) or "").strip()
        if kind == "flag":
            if form.get(f"{key}.sent"):                 # a checkbox: absent means off, if the form had it at all
                wanted[key] = 1 if v in ("1", "on") else 0
            continue
        if not v:
            continue
        if kind == "clock":
            try:
                h, m = (int(x) for x in v.split(":"))
                if not (0 <= h <= 23 and 0 <= m <= 59):
                    raise ValueError
                wanted[key] = clock_encode(h, m)
            except ValueError:
                errors[key] = "not a time"
            continue
        try:
            n = float(v)
            wanted[key] = int(n) if n == int(n) else n      # a fraction is refused later, unless it is what Bloom holds
        except (ValueError, OverflowError):
            errors[key] = "not a number"
    return wanted, errors


def fields(wanted: dict) -> list:
    """`wanted` back as (field name, value) pairs — the hidden inputs that carry
    a previewed change to its confirmation, readable by parse() again."""
    out = []
    for s in SETTINGS:
        key = s["key"]
        if key not in wanted:
            continue
        v = wanted[key]
        if s["kind"] == "date":
            md = date_decode(v)
            if md:
                out += [(f"{key}.m", md[0]), (f"{key}.d", md[1])]
        elif s["kind"] == "clock":
            out.append((key, clock_field(v)))
        elif s["kind"] == "flag":
            out += [(f"{key}.sent", 1), (key, 1 if v else "")]
        else:
            out.append((key, v))
    return out


# ── the change ───────────────────────────────────────────────────────────────

def plan(building: dict, wanted=None, form_errors=None) -> dict:
    """What setting this building to `wanted` would change, from the LIVE
    building object (its points carry the ids Bloom needs). Pure. Returns:

      rows     one per setting, in display order: the value there now, the one
               asked for, whether it changes, and `error` / `why` (not offered)
      points   the exact point objects to send — Bloom's own object with `pv`
               new and `originalPv` old; EMPTY when any row has an error
               (all or nothing: never send half of what was asked)
      digest   a fingerprint of `points`, so the confirmed change can be checked
               against the previewed one
    """
    wanted = dict(wanted or {})
    form_errors = form_errors or {}
    pts = points(building)

    def now_of(key):
        pt = pts[key][0]
        return pt.get("pv") if pt is not None else None

    def after(key):                                    # the value in force once this change is made
        return wanted[key] if key in wanted and pts[key][0] is not None else now_of(key)

    tt_now, tt_new = now_of("space-min-threshold-type"), after("space-min-threshold-type")
    rows, send = [], []
    for s in SETTINGS:
        key = s["key"]
        pt, why = pts[key]
        old = pt.get("pv") if pt is not None else None
        row = {**s, "found": pt is not None, "why": why, "old": old,
               "old_text": text(key, old, tt_now) if pt is not None else "—",
               "new": None, "new_text": None, "change": False, "error": form_errors.get(key), "note": None}
        if key in wanted and not row["error"]:
            new = wanted[key]
            if pt is None:
                row["error"] = f"cannot be changed here: {why}"
            elif _num(old) is not None and _num(old) == float(new):
                pass                                   # already that value: nothing to send
            elif s["kind"] == "clock" and clock_decode(old) is not None and clock_decode(old) == clock_decode(new):
                pass                                   # the same time of day, whatever its last digit
            elif new != int(new):
                row["new"], row["new_text"], row["change"] = new, f"{new:g}", True
                row["error"] = "whole numbers only"
            else:
                row["new"], row["new_text"], row["change"] = new, text(key, new, tt_new), True
                lo, hi = s.get("lo"), s.get("hi")
                if s["kind"] == "choice" and new not in dict(s["choices"]):
                    row["error"] = "not one of the choices"
                elif s["kind"] == "flag" and new not in (0, 1):
                    row["error"] = "on or off"
                elif lo is not None and not lo <= new <= hi:
                    unit = "°" if s["kind"] == "deg" else ""
                    row["error"] = f"Bloom allows {lo}{unit} to {hi}{unit}"
                elif s["kind"] == "clock" and clock_decode(new) is None:
                    row["error"] = "not a time"
                elif s["kind"] == "date" and date_decode(new) is None:
                    row["error"] = "not a date"
        rows.append(row)
    by = {r["key"]: r for r in rows}

    # the day period must start before it ends (a guard of ours, not Bloom's)
    start, stop = by["start-tm-day"], by["stop-tm-day"]
    if (start["change"] or stop["change"]) and not (start["error"] or stop["error"]):
        a, b = _num(after("start-tm-day")), _num(after("stop-tm-day"))
        if a is not None and b is not None and a >= b:
            for r in (start, stop):
                if r["change"]:
                    r["error"] = "the day period has to start before the night period"
    ws, we = by["wint-start-day"], by["wint-end-day"]
    if (ws["change"] or we["change"]) and not (ws["error"] or we["error"]):
        if _num(after("wint-start-day")) == _num(after("wint-end-day")):
            for r in (ws, we):
                if r["change"]:
                    r["error"] = "the season cannot start and end on the same day"
    # the threshold reads differently once its type changes: show it in the new terms
    th = by["space-min-threshold"]
    if by["space-min-threshold-type"]["change"] and th["found"] and not th["change"]:
        th["note"] = f"stays {th['old']:g}, now read as {text('space-min-threshold', th['old'], tt_new)}" \
            if _num(th["old"]) is not None else None

    errors = any(r["error"] for r in rows)
    if not errors:
        for r in rows:
            if r["change"]:
                pt = pts[r["key"]][0]
                send.append({**pt, "pv": r["new"], "originalPv": pt.get("pv")})
    digest = hashlib.sha256(json.dumps(
        sorted((str(p.get("_id")), p["pv"]) for p in send)).encode()).hexdigest()[:16] if send else ""
    return {"rows": rows, "points": send, "digest": digest, "n_changes": len(send), "errors": errors,
            "n_offered": sum(1 for r in rows if r["found"]),
            "asked": sum(1 for r in rows if r["change"]),
            "log": [{"type": p.get("type"), "name": p.get("name"), "old": p.get("originalPv"), "new": p["pv"],
                     "label": BY_KEY[p["type"]]["label"]} for p in send]}


def form_groups(p: dict) -> list:
    """A plan's rows laid out for the form, group by group. Each row gains what
    its input shows — the value asked for, else the one in Bloom — as `field`
    ('HH:MM' for a time), `md` ((month, day) for a date) and, for a time, the
    `options` of its drop-down."""
    out = []
    for name in GROUPS:
        rows = [r for r in p["rows"] if r["group"] == name]
        for r in rows:
            v = r["new"] if r["change"] else r["old"]
            r["field"] = clock_field(v) if r["kind"] == "clock" else v
            r["md"] = date_decode(v) if r["kind"] == "date" else None
            r["options"] = clock_options(r["old"]) if r["kind"] == "clock" else None
        out.append({"name": name, "rows": rows})
    return out


def landed(building: dict, wanted: dict) -> bool:
    """True when the building now holds every value that was asked for."""
    p = plan(building, wanted)
    return not p["errors"] and p["n_changes"] == 0


def write_summary(changes) -> str:
    """A logged change in a few words: 'Indoor setpoint, day 72° → 70°; …'."""
    ch = changes or []
    tt = next((c.get("new") for c in ch if c.get("type") == "space-min-threshold-type"), None)
    parts = []
    for c in ch:
        key = c.get("type")
        if key not in BY_KEY:
            parts.append(f"{key}: {c.get('old')} → {c.get('new')}")
            continue
        parts.append(f"{BY_KEY[key]['label']} {text(key, c.get('old'), tt)} → {text(key, c.get('new'), tt)}")
    return "; ".join(parts)
