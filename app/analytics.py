"""
app/analytics.py — what the 2010 boxes' history says (/analytics, box history)

Reads only the summaries app/history.py keeps (legacy_hourly, legacy_daily,
legacy_events; raw reads only for a box's last week), so any period is a
few aggregate queries.

Per building, for a period, compared with the fleet and with the building's
own previous period of the same length:
  * Heat-season minimums: apartment sensor-hours below them, lowest reading
  * Efficiency: burner minutes per heating degree-day (65 F base). Weather
    is taken out, so buildings compare fairly; needs a few degree-days
  * Waste: heating while well above the desired temperature, heating on
    warm days (60 F+ outside)
  * Hot water: hot-water burner time per day, water used at night (leaks)
  * Combustion: average peak stack temperature while firing
  * Reliability: time in Safety / Ignition Failure, trips, answering rate
  * Sensors: apartment sensors stuck on one value, or gone quiet

findings() turns those numbers into a short ranked list in plain English.
"""
import datetime as _dt
import statistics

from sqlalchemy import text

from app import history

PERIODS = {"1": 1, "7": 7, "30": 30, "90": 90, "365": 365}
MIN_HDD = 2.0                  # fewer degree-days than this: efficiency is not comparable yet
EFFICIENCY_FLAG = 1.4          # burner time per degree-day this many times the fleet median
TREND_FLAG = 0.25              # 25% worse than the building's previous period
HW_FLAG = 2.0
STACK_FLAG = 1.3
UPTIME_FLAG = 0.9


def period(key, now):
    """(start, end, label) in UTC for a period key: a day count, or 'season'
    (since October 1, New York)."""
    if key == "season":
        lt = history.local(now)
        year = lt.year if lt.month >= 10 else lt.year - 1
        start = history.day_bounds(_dt.date(year, 10, 1))[0]
        return start, now, f"Heating season {year}-{str(year + 1)[2:]}"
    days = PERIODS.get(str(key), 7)
    label = {1: "Last 24 hours", 7: "Last 7 days", 30: "Last 30 days", 90: "Last 90 days", 365: "Last 12 months"}[days]
    return now - _dt.timedelta(days=days), now, label


_AGG = """
SELECT h.box_id,
       count(*) AS hours, sum(h.n) AS n,
       sum(h.heat_min) AS heat, sum(h.hw_min) AS hw, sum(h.hdd) AS hdd,
       avg(h.in_avg) AS in_avg, min(h.in_min) AS in_min, avg(h.out_avg) AS out_avg, avg(h.dit) AS dit,
       sum(h.calling_min) AS calling, sum(h.safety_min) AS safety, sum(h.bypass_min) AS bypass,
       count(*) FILTER (WHERE h.law_low > 0) AS law_hours,
       coalesce(sum(h.law_low), 0) AS law_sensor_hours,
       count(*) FILTER (WHERE h.law_req IS NOT NULL AND h.apt_avg IS NOT NULL) AS law_checked,
       min(h.apt_low) AS apt_low,
       count(*) FILTER (WHERE h.in_avg >= h.dit + :overheat AND h.heat_min > 0) AS overheat_hours,
       coalesce(sum(h.heat_min) FILTER (WHERE h.out_avg >= :warm), 0) AS warm_heat,
       avg(h.stack_max) FILTER (WHERE h.heat_min >= 10) AS stack_fire,
       max(h.hi_stack) AS hi_stack,
       sum(h.water) AS water,
       sum(h.water) FILTER (WHERE extract(hour FROM (h.hour AT TIME ZONE 'UTC') AT TIME ZONE :tz) BETWEEN 1 AND 4)
           AS water_night,
       sum(h.fuel) AS fuel,
       bool_and(h.out_fleet) AS no_out_sensor,
       min(h.hour) AS first_hour
FROM legacy_hourly h
WHERE h.hour >= :s AND h.hour < :e
GROUP BY h.box_id
"""


def _f(v):
    return float(v) if v is not None else None


def _median(vals):
    vals = [v for v in vals if v is not None]
    return statistics.median(vals) if vals else None


def portfolio(key, now=None, poll_min=5):
    """Everything /analytics shows for one period."""
    from app import legacy
    from app.extensions import db
    from app.models import LegacyBox
    now = now or _dt.datetime.utcnow()
    start, end, label = period(key, now)
    tz = getattr(legacy.LOCAL, "key", "UTC")
    q = lambda s, e: {r.box_id: r._mapping for r in db.session.execute(
        text(_AGG), {"s": s, "e": e, "overheat": history.OVERHEAT, "warm": history.WARM_OUTSIDE, "tz": tz})}
    cur = q(start, end)
    prev = q(start - (end - start), start)
    since = db.session.execute(text("SELECT min(hour) FROM legacy_hourly")).scalar()
    boxes = {b.id: b for b in LegacyBox.query.filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None))}
    ev = {}
    for r in db.session.execute(text(
            "SELECT box_id, kind, code, count(*) AS c FROM legacy_events WHERE ts >= :s AND ts < :e "
            "GROUP BY box_id, kind, code"), {"s": start, "e": end}):
        ev.setdefault(r.box_id, []).append((r.kind, r.code, r.c))

    rows = []
    for box_id, a in cur.items():
        b = boxes.get(box_id)
        if b is None:
            continue
        eff_start = max(start, a["first_hour"] or start)
        span_h = max(1.0, (end - eff_start).total_seconds() / 3600)
        days = span_h / 24
        heat, hw, hdd = int(a["heat"] or 0), int(a["hw"] or 0), _f(a["hdd"]) or 0.0
        p = prev.get(box_id)
        p_mpd = (int(p["heat"] or 0) / _f(p["hdd"])) if p and (_f(p["hdd"]) or 0) >= MIN_HDD else None
        kinds = ev.get(box_id, [])
        entries = sum(c for k, code, c in kinds if k == "status" and code in ("Safety", "Ign Fail"))
        alarms = sum(c for k, code, c in kinds if k == "alarm" and code in ("safetyActive", "ignitionFail"))
        rows.append({
            "box": b, "hours": a["hours"], "n": a["n"], "days": days,
            "uptime": min(1.0, (a["n"] or 0) / (span_h * 60 / poll_min)),
            "heat": heat, "hw": hw, "hdd": hdd,
            "mpd": heat / hdd if hdd >= MIN_HDD else None, "prev_mpd": p_mpd,
            "heat_day": heat / days, "hw_day": hw / days,
            "in_avg": _f(a["in_avg"]), "in_min": a["in_min"], "out_avg": _f(a["out_avg"]), "dit": _f(a["dit"]),
            "calling": a["calling"] or 0, "safety": a["safety"] or 0, "bypass": a["bypass"] or 0,
            "trips": max(entries, alarms), "dark": sum(c for k, _c, c in kinds if k == "dark"),
            "law_hours": a["law_hours"], "law_sensor_hours": a["law_sensor_hours"], "law_checked": a["law_checked"],
            "apt_low": a["apt_low"], "overheat_hours": a["overheat_hours"], "warm_heat": a["warm_heat"],
            "stack_fire": _f(a["stack_fire"]), "hi_stack": a["hi_stack"],
            "water": a["water"], "water_night": a["water_night"], "fuel": a["fuel"],
            "no_out_sensor": bool(a["no_out_sensor"]),
        })
    fleet = {
        "mpd": _median(r["mpd"] for r in rows),
        "hw_day": _median(r["hw_day"] for r in rows if r["hw"]),
        "stack_fire": _median(r["stack_fire"] for r in rows),
        "hdd": _median(r["hdd"] for r in rows),
    }
    for r in rows:
        r["mpd_x"] = r["mpd"] / fleet["mpd"] if r["mpd"] and fleet["mpd"] else None
        r["trend"] = (r["mpd"] - r["prev_mpd"]) / r["prev_mpd"] if r["mpd"] and r["prev_mpd"] else None
        r["hw_x"] = r["hw_day"] / fleet["hw_day"] if r["hw"] and fleet["hw_day"] else None
        r["stack_x"] = r["stack_fire"] / fleet["stack_fire"] if r["stack_fire"] and fleet["stack_fire"] else None
    law = law_detail([r["box"].id for r in rows if r["law_sensor_hours"]], start, end)
    sensors = sensor_health(now, boxes)
    kpi = {
        "boxes": len(rows), "of": len(boxes),
        "burner_hours": sum(r["heat"] + r["hw"] for r in rows) / 60,
        "law_boxes": sum(1 for r in rows if r["law_sensor_hours"]),
        "law_sensor_hours": sum(r["law_sensor_hours"] for r in rows),
        "safety_hours": sum(r["safety"] for r in rows) / 60,
        "trips": sum(r["trips"] for r in rows),
        "uptime": _median(r["uptime"] for r in rows),
        "hdd": fleet["hdd"],
    }
    return {"start": start, "end": end, "label": label, "key": str(key), "since": since, "rows": rows,
            "fleet": fleet, "kpi": kpi, "law": law, "sensors": sensors,
            "findings": findings(rows, fleet, law, sensors, boxes)}


def law_detail(box_ids, start, end):
    """Per building with sensor-hours under the minimum: which sensors, how
    many hours, how cold, and when it last happened."""
    from app.models import LegacyHourly
    out = {}
    if not box_ids:
        return out
    for h in LegacyHourly.query.filter(LegacyHourly.box_id.in_(box_ids), LegacyHourly.hour >= start,
                                       LegacyHourly.hour < end, LegacyHourly.law_low > 0) \
            .order_by(LegacyHourly.hour).all():
        d = out.setdefault(h.box_id, {"sensors": {}, "last": None, "night": 0, "day": 0})
        d["last"] = h.hour
        d["night" if h.law_req == history.NIGHT_MIN else "day"] += 1
        for i, v in enumerate(h.apt_avg or []):
            if v is not None and v < h.law_req:
                s = d["sensors"].setdefault(i + 1, {"hours": 0, "low": v})
                s["hours"] += 1
                s["low"] = min(s["low"], v)
    for d in out.values():
        d["worst"] = sorted(d["sensors"].items(), key=lambda kv: (-kv[1]["hours"], kv[1]["low"]))[:4]
    return out


def sensor_health(now, boxes):
    """Apartment sensors over the last 48 hours: stuck on one value for a
    day or more, or reporting earlier but silent for the last 6 hours."""
    from app.extensions import db
    latest = db.session.execute(text("SELECT max(hour) FROM legacy_hourly")).scalar()
    if latest is None:
        return []
    out = []
    for r in db.session.execute(text("""
            SELECT h.box_id, u.i, count(*) AS hours, count(DISTINCT u.v) AS vals, max(h.hour) AS last,
                   min(u.v) AS lo, max(u.v) AS hi
            FROM legacy_hourly h, unnest(h.apt_avg) WITH ORDINALITY AS u(v, i)
            WHERE h.hour >= :s AND u.v IS NOT NULL
            GROUP BY h.box_id, u.i"""), {"s": now - _dt.timedelta(hours=48)}):
        b = boxes.get(r.box_id)
        if b is None:
            continue
        if r.hours >= 24 and r.vals == 1:
            out.append({"box": b, "sensor": r.i, "what": f"stuck at {r.lo} F for {r.hours} hours"})
        elif r.hours >= 6 and r.last < latest - _dt.timedelta(hours=6):
            out.append({"box": b, "sensor": r.i,
                        "what": f"silent since {history.local(r.last).strftime('%b %d %I %p').replace(' 0', ' ')}"})
    return sorted(out, key=lambda s: (s["box"].name.lower(), s["sensor"]))


def _hm(minutes):
    minutes = int(round(minutes or 0))
    return f"{minutes // 60} h {minutes % 60:02d} min" if minutes >= 60 else f"{minutes} min"


def findings(rows, fleet, law, sensors, boxes):
    """The ranked list at the top of /analytics: (severity, box, category, text)."""
    out = []
    add = lambda sev, r, cat, msg, score: out.append({"sev": sev, "box": r["box"], "cat": cat, "text": msg, "score": score})
    for r in rows:
        if r["law_sensor_hours"]:
            d = law.get(r["box"].id, {})
            worst = ", ".join(f"#{i} ({s['hours']} h, low {s['low']} F)" for i, s in d.get("worst", []))
            add("crit" if r["law_hours"] >= 6 else "warn", r, "Heat-season minimum",
                f"apartments below the minimum for {r['law_hours']} hour(s), {r['law_sensor_hours']} sensor-hour(s)"
                + (f": sensors {worst}" if worst else ""), 1000 + r["law_sensor_hours"])
        if r["safety"]:
            add("crit", r, "Safety", f"{_hm(r['safety'])} in Safety / Ignition Failure"
                + (f" ({r['trips']} trip{'s' if r['trips'] != 1 else ''})" if r["trips"] else ""), 900 + r["safety"])
        elif r["trips"]:
            add("warn", r, "Safety", f"{r['trips']} safety/ignition alarm(s), cleared between reads", 800 + r["trips"])
        if r["mpd_x"] and r["mpd_x"] >= EFFICIENCY_FLAG:
            add("warn", r, "Efficiency", f"{r['mpd_x']:.1f}x the fleet's burner time per degree-day "
                f"({r['mpd']:.0f} vs {fleet['mpd']:.0f} min)", 500 + r["mpd_x"] * 10)
        if r["trend"] and r["trend"] >= TREND_FLAG:
            add("warn", r, "Efficiency", f"burner time per degree-day up {r['trend'] * 100:.0f}% on the previous period",
                450 + r["trend"] * 100)
        if r["overheat_hours"] >= 6:
            add("warn", r, "Waste", f"heated for {r['overheat_hours']} hour(s) while {history.OVERHEAT}+ F above "
                f"its desired temperature", 400 + r["overheat_hours"])
        if r["warm_heat"] >= 60:
            add("warn", r, "Waste", f"{_hm(r['warm_heat'])} of heating with {history.WARM_OUTSIDE} F+ outside",
                350 + r["warm_heat"] / 60)
        if r["hw_x"] and r["hw_x"] >= HW_FLAG and r["hw_day"] >= 120:
            add("info", r, "Hot water", f"hot-water burner time {r['hw_x']:.1f}x typical "
                f"({_hm(r['hw_day'])} a day): heavy use or a leak", 300 + r["hw_x"])
        if r["water"] and r["water_night"] and r["water_night"] > 0.25 * r["water"]:
            add("warn", r, "Water", f"{r['water_night'] / r['water'] * 100:.0f}% of the water used between "
                "1 and 5 am: check for leaks", 320)
        if r["stack_x"] and r["stack_x"] >= STACK_FLAG and (r["stack_fire"] or 0) >= 400:
            add("info", r, "Combustion", f"stack {r['stack_fire']:.0f} F while firing, {r['stack_x']:.1f}x the "
                "fleet's: cleaning or burner tune-up", 250 + r["stack_x"])
        if r["uptime"] < UPTIME_FLAG and r["days"] >= 1:
            add("info", r, "Connection", f"answered {r['uptime'] * 100:.0f}% of reads"
                + (f", dark {r['dark']} time(s)" if r["dark"] else ""), 100 + (1 - r["uptime"]) * 100)
    for s in sensors:
        out.append({"sev": "info", "box": s["box"], "cat": "Sensor",
                    "text": f"apartment sensor #{s['sensor']} {s['what']}", "score": 50})
    out.sort(key=lambda f: -f["score"])
    return out


# ── one box's history (/live/<id>: charts, daily table, CSV) ────────────────

RANGES = {"1": 1, "2": 2, "7": 7, "30": 30, "90": 90, "365": 365}
LINES = ("out", "inside", "dit", "aqua", "supp", "retn", "stack")


def _ms(t):
    return int(t.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)


def box_series(box_id, days, now=None) -> dict:
    """Chart data for one box. Temperatures: every read up to 7 days, hourly
    averages up to 120 days, daily beyond. Burner minutes and the apartment
    heat map: hourly (daily beyond 120 days, without the heat map)."""
    from app.models import LegacyDaily, LegacyEvent, LegacyHourly, LegacyReading
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    out = {"days": days, "lines": {}, "bars": {}, "apt": None, "events": []}
    if days <= 7:
        rs = LegacyReading.query.filter(LegacyReading.box_id == box_id, LegacyReading.ts >= start) \
            .order_by(LegacyReading.ts).all()
        out["res"] = "every read"
        out["t"] = [_ms(r.ts) for r in rs]
        ok = lambda v, lo, hi: v if v is not None and lo <= v <= hi else None
        rng = {"out": (-40, 130), "inside": (35, 110), "dit": (40, 90), "stack": (0, 1500)}
        for k in LINES:
            lo, hi = rng.get(k, (0, 260))
            out["lines"][k] = [ok(getattr(r, k), lo, hi) for r in rs]
    hourly = days <= 120
    if hourly:
        hs = LegacyHourly.query.filter(LegacyHourly.box_id == box_id, LegacyHourly.hour >= start) \
            .order_by(LegacyHourly.hour).all()
        bt = [_ms(h.hour) for h in hs]
        out["bars"] = {"t": bt, "heat": [h.heat_min for h in hs], "hw": [h.hw_min for h in hs], "step": "hour"}
        if days > 7:
            out["res"] = "hourly averages"
            out["t"] = bt
            cols = {"out": "out_avg", "inside": "in_avg", "dit": "dit", "aqua": "aqua_avg", "supp": "supp_avg",
                    "retn": "retn_avg", "stack": "stack_avg"}
            for k, c in cols.items():
                out["lines"][k] = [getattr(h, c) for h in hs]
        width = max((len(h.apt_avg or []) for h in hs), default=0)
        if width:
            out["apt"] = {"t": bt, "sensors": list(range(1, width + 1)),
                          "v": [[(h.apt_avg[i] if h.apt_avg and len(h.apt_avg) > i else None) for h in hs]
                                for i in range(width)],
                          "req": [h.law_req for h in hs]}
    else:
        ds = LegacyDaily.query.filter(LegacyDaily.box_id == box_id, LegacyDaily.day >= history.local(start).date()) \
            .order_by(LegacyDaily.day).all()
        t = [_ms(history.day_bounds(d.day)[0]) for d in ds]
        out["res"] = "daily averages"
        out["t"] = t
        cols = {"out": "out_avg", "inside": "in_avg", "dit": "dit"}
        for k in LINES:
            out["lines"][k] = [getattr(d, cols[k]) for d in ds] if k in cols else [None] * len(ds)
        out["bars"] = {"t": t, "heat": [d.heat_min for d in ds], "hw": [d.hw_min for d in ds], "step": "day"}
    out["events"] = [{"t": _ms(e.ts), "kind": e.kind, "text": e.text or e.code or e.kind}
                     for e in LegacyEvent.query.filter(LegacyEvent.box_id == box_id, LegacyEvent.ts >= start)
                     .order_by(LegacyEvent.ts.desc()).limit(200)]
    return out


CSV_COLUMNS = {
    "raw": ["ts", "code", "out", "inside", "dit", "aqua", "coil", "mix", "supp", "retn", "risr", "stack", "hi_stack",
            "valve", "pressure", "heat1", "hw1", "heat2", "hw2", "heat3", "hw3", "heat4", "hw4", "fuel_level",
            "gallons", "fuel_count", "water_count", "ign_count", "apt"],
    "hourly": ["hour", "n", "out_avg", "out_fleet", "in_avg", "in_min", "in_max", "dit", "aqua_avg", "aqua_max",
               "supp_avg", "retn_avg", "coil_min", "coil_max", "mix_min", "mix_max", "stack_avg", "stack_max",
               "hi_stack", "heat_min", "hw_min", "calling_min", "safety_min", "bypass_min", "fuel", "water", "hdd",
               "law_req", "law_low", "apt_low", "apt_avg"],
    "daily": ["day", "n", "hours", "out_avg", "out_min", "out_max", "hdd", "in_avg", "in_min", "in_max", "dit",
              "heat_min", "hw_min", "calling_min", "safety_min", "fuel", "water", "water_night", "law_hours",
              "law_sensor_hours", "apt_low", "stack_fire", "overheat_hours", "warm_heat_min"],
}


def box_csv_rows(box_id, res, days, now=None):
    """(header, rows) for a box's CSV download. Times are New York time."""
    from app.models import LegacyDaily, LegacyHourly, LegacyReading
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    cols = CSV_COLUMNS[res]
    if res == "raw":
        q = LegacyReading.query.filter(LegacyReading.box_id == box_id, LegacyReading.ts >= start).order_by(LegacyReading.ts)
    elif res == "hourly":
        q = LegacyHourly.query.filter(LegacyHourly.box_id == box_id, LegacyHourly.hour >= start).order_by(LegacyHourly.hour)
    else:
        q = LegacyDaily.query.filter(LegacyDaily.box_id == box_id, LegacyDaily.day >= history.local(start).date()) \
            .order_by(LegacyDaily.day)

    def cell(c, v):
        if isinstance(v, _dt.datetime):
            return history.local(v).strftime("%Y-%m-%d %H:%M")
        if isinstance(v, list):
            return " ".join("" if x is None else str(x) for x in v)
        return "" if v is None else v
    header = [{"ts": "time (New York)", "hour": "hour (New York)"}.get(c, c) for c in cols]
    return header, ([cell(c, getattr(r, c)) for c in cols] for r in q.yield_per(2000))


# ── one sensor (or several) of one box, for the Live page's pop-up chart ─────

BASE_SENSORS = [("out", "Outside"), ("inside", "Inside average"), ("dit", "Desired inside"), ("aqua", "Aquastat"),
                ("supp", "Supply"), ("retn", "Return"), ("risr", "Riser"), ("coil", "Coil"), ("mix", "Mix"),
                ("stack", "Stack"), ("hi_stack", "High stack")]
_HOURLY_COL = {"out": "out_avg", "inside": "in_avg", "dit": "dit", "aqua": "aqua_avg", "supp": "supp_avg",
               "retn": "retn_avg", "stack": "stack_avg", "hi_stack": "hi_stack", "coil": "coil_max", "mix": "mix_max"}


def _ok_temp(key, v):
    """A reading worth plotting: 0 is how these boxes say 'no sensor'
    (except outside, which really can be 0 F); apartments only 35-110 F."""
    if v is None:
        return None
    if key == "out":
        return v if -40 <= v <= 130 else None
    if key.startswith("apt"):
        return v if 35 <= v <= 110 else None
    return v if v != 0 and -40 <= v <= 1500 else None


def sensor_catalog(box):
    """Every sensor this box can chart: the main ones, its own named system
    sensors, its apartment sensors."""
    data = (box.status.data or {}) if box.status else {}
    st, names = data.get("status") or {}, data.get("names") or {}
    out = [{"key": k, "label": l, "group": "Main"} for k, l in BASE_SENSORS]
    for i in range(16):
        name = (names.get(f"systemSensorName{i}") or "").strip()
        v = history._num(st.get(f"systemTemperature{i}"))
        if name and v not in (None, 0):
            out.append({"key": f"sys{i}", "label": name, "group": "Box sensors"})
    for i in range(32):
        v = history._num(st.get(f"apartmentTemperature{i}"))
        if v and 35 <= v <= 110:
            out.append({"key": f"apt{i}", "label": f"Apartment #{i + 1}", "group": "Apartments"})
    return out


def box_sensor_series(box, days, keys, now=None) -> dict:
    """Chart data for the pop-up: the requested sensors' lines (every read up
    to 7 days, hourly averages beyond), and burner minutes per hour (heat,
    hot water) on the same time axis. Box sensors are only in the reads."""
    from app.models import LegacyHourly, LegacyReading
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    catalog = sensor_catalog(box)
    labels = {c["key"]: c["label"] for c in catalog}
    keys = [k for k in keys if k in labels][:12]
    out = {"range": [_ms(start), _ms(now)], "labels": {k: labels[k] for k in keys}, "catalog": catalog,
           "series": {}, "days": days, "note": None}
    hours = {}
    if days <= 7:
        rs = LegacyReading.query.filter(LegacyReading.box_id == box.id, LegacyReading.ts >= start - history.LOOKBACK) \
            .order_by(LegacyReading.ts).all()
        shown = [r for r in rs if r.ts >= start]
        out["res"] = "every read"
        out["t"] = [_ms(r.ts) for r in shown]

        def val(r, k):
            if k.startswith("sys"):
                arr, i = r.sys or [], int(k[3:])
            elif k.startswith("apt"):
                arr, i = r.apt or [], int(k[3:])
            else:
                return _ok_temp(k, getattr(r, k))
            return _ok_temp(k, arr[i] if i < len(arr) else None)
        for k in keys:
            out["series"][k] = [val(r, k) for r in shown]
        nb = range(1, 5)
        for a, b in zip(rs, rs[1:]):                  # run minutes per hour, from the counters
            if b.ts < start:
                continue
            h = history.floor_hour(b.ts)
            heat = sum(history.used([a, b], f"heat{i}") for i in nb)
            hw = sum(history.used([a, b], f"hw{i}") for i in nb)
            acc = hours.setdefault(h, [0, 0])
            acc[0] += heat
            acc[1] += hw
    else:
        hs = LegacyHourly.query.filter(LegacyHourly.box_id == box.id, LegacyHourly.hour >= start) \
            .order_by(LegacyHourly.hour).all()
        out["res"] = "hourly averages"
        out["t"] = [_ms(h.hour) for h in hs]
        for k in keys:
            if k.startswith("apt"):
                i = int(k[3:])
                out["series"][k] = [(h.apt_avg[i] if h.apt_avg and len(h.apt_avg) > i else None) for h in hs]
            elif k in _HOURLY_COL:
                out["series"][k] = [_ok_temp(k, getattr(h, _HOURLY_COL[k])) for h in hs]
            else:
                out["series"][k] = [None] * len(hs)
                out["note"] = "Box sensors and the riser are kept as every read for 90 days, not as hourly summaries: pick 7 days or less for them."
        for h in hs:
            hours[h.hour] = [h.heat_min, h.hw_min]
    hk = sorted(hours)
    out["bars"] = {"t": [_ms(h) for h in hk], "heat": [min(hours[h][0], 240) for h in hk],
                   "hw": [min(hours[h][1], 240) for h in hk], "step": "hour"}
    return out
