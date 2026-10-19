"""
app/history.py — the history of the installed 2010 boxes (/live)

Three layers, sized on the real boxes (2026-10-14: ~180 boxes, 94 fields a
read, every 5 minutes; JSON would cost 22 GB a year, typed columns 7 GB):

  legacy_readings  every successful read, typed (LegacyReading); kept
                   LEGACY_RAW_DAYS (default 90) in monthly partitions that are
                   dropped whole, so old reads cost nothing to remove
  legacy_hourly    one row per box per hour, kept for good (~0.5 GB a year)
  legacy_daily     one row per box per New York day, kept for good (tiny)
  legacy_events    status changes, box alarms, dark/back: written on change

What the rollups have to get right, learned from the boxes:
  * Run minutes are counters since the BOX's midnight, and several box
    clocks are an hour off: usage is the sum of the differences between
    consecutive reads, a drop means the counter reset, and no step may
    exceed the minutes that passed (nor be trusted across a long gap).
  * Many boxes have no outside sensor: the fleet's median outside
    temperature that hour stands in (out_fleet), so degree-days and the
    heat-season rule still work.
  * Apartment sensors read 0 when absent: only 35-110 F counts.

Heat-season minimums (a benchmark, NYC's rule; the buildings are in the New
York area): October 1 to May 31, 6 am to 10 pm at least 68 F inside when it
is below 55 F outside; 10 pm to 6 am at least 62 F whatever the weather. An
apartment sensor whose HOURLY AVERAGE is below the minimum counts as one
sensor-hour under it (one cold read alone does not).

Everything runs from the legacy poller's thread under its advisory lock:
record() after each cycle, maintain() after that.
"""
import datetime as _dt
import logging
import re
import statistics

from sqlalchemy import func, insert, text

log = logging.getLogger(__name__)

HOUR = _dt.timedelta(hours=1)
SETTLE = _dt.timedelta(minutes=6)        # an hour is summed this long after it ends
LOOKBACK = _dt.timedelta(minutes=15)     # the read before an hour, for its first difference
MAX_GAP_MIN = 70                         # a longer gap between reads: usage across it is unknown

SEASON_MONTHS = (10, 11, 12, 1, 2, 3, 4, 5)
DAY_FROM, DAY_TO = 6, 22
DAY_MIN, NIGHT_MIN, DAY_RULE_BELOW = 68, 62, 55
HDD_BASE = 65
WARM_OUTSIDE = 60                        # heating with this or warmer outside is waste
OVERHEAT = 4                             # inside this far above the desired temperature

_UTC = _dt.timezone.utc
_PART = re.compile(r"legacy_readings_(\d{4})_(\d{2})$")


# ── one read ─────────────────────────────────────────────────────────────────

def _num(v):
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def _small(v):
    n = _num(v)
    return n if n is not None and -32768 <= n <= 32767 else None


def _int32(v):
    n = _num(v)
    return n if n is not None and -2 ** 31 <= n < 2 ** 31 else None


def _big(v):
    n = _num(v)
    return n if n is not None and -2 ** 63 <= n < 2 ** 63 else None


def _array(st, prefix, count):
    vals = [_small(st.get(f"{prefix}{i}")) for i in range(count)]
    while vals and vals[-1] is None:
        vals.pop()
    return vals or None


def reading(box_id, ts, data) -> dict:
    """The typed row for one read (LegacyStatus.data merged: status + names)."""
    from app import legacy
    st = (data or {}).get("status") or {}
    c = legacy.card(data or {})
    nb = legacy.boilers(data)
    return {
        "box_id": box_id, "ts": ts, "code": legacy.status_code(st.get("callForHeat"))[:12] or None,
        "out": _small(c["out"]), "inside": _small(c["inside"]), "dit": _small(c["dit"]),
        "aqua": _small(c["aqua"]), "coil": _small(c["coil"]), "mix": _small(c["mix"]),
        "supp": _small(c["supp"]), "retn": _small(c["retn"]), "risr": _small(c["risr"]),
        "dsup": _small(st.get("desiredSupplyTemp")),
        "stack": _small(c["stack"]), "hi_stack": _small(c["hi_stack"]),
        "valve": _small(st.get("controlValvePctOpen")), "pressure": _small(st.get("pressure0")),
        # only the boilers the box has (legacy.boilers): the others' counters are noise
        **{f"heat{i}": _small(st.get(f"boiler{i}HeatRunMinutes")) if i <= nb else None for i in range(1, 5)},
        **{f"hw{i}": _small(st.get(f"boiler{i}HWtrRunMinutes")) if i <= nb else None for i in range(1, 5)},
        "fuel_level": _small(st.get("fuelLevel")), "gallons": _int32(st.get("gallons")),
        "fuel_count": _big(st.get("fuelMeterCount")), "water_count": _big(st.get("waterMeterCount")),
        "ign_count": _small(st.get("ignitionFailCount")),
        "outputs": _int32(st.get("outputImage")), "inputs": _int32(st.get("inputImage")),
        "sys": _array(st, "systemTemperature", 16), "apt": _array(st, "apartmentTemperature", 32),
    }


def alarm_key(data):
    a = (data or {}).get("alarm")
    return f"{a.get('name')}@{a.get('at')}"[:120] if a else ""


DARK_AFTER_MISSES = 2       # one missed read is a reset connection, not a dark box
NO_ALARM_AFTER = 12         # empty alarm answers in a row before "this box has no alarm" is believed


def remember(prev_data, ok, data):
    """What the stored reading keeps between polls for the event log:
    'last_alarm' survives reads that answer an empty alarm list (some boxes
    alternate, 2026-10-15), 'fails' counts misses in a row. Returns the new
    stored dict (data when the read was ok, the old one otherwise)."""
    if ok:
        out = dict(data)
        out["fails"] = 0
        if out.get("alarm"):
            out["last_alarm"], out["empty_alarms"] = alarm_key(out), 0
        else:
            # A box that flickers never answers empty for an hour straight, so
            # its old alarm is never mistaken for a new one when it reappears;
            # a box that truly has none is believed after NO_ALARM_AFTER reads.
            out["empty_alarms"] = int(prev_data.get("empty_alarms") or 0) + 1
            if "last_alarm" in prev_data:
                out["last_alarm"] = prev_data["last_alarm"]
            elif out["empty_alarms"] >= NO_ALARM_AFTER:
                out["last_alarm"] = ""
        return out
    out = dict(prev_data)
    out["fails"] = int(prev_data.get("fails") or 0) + 1
    return out


def changes(box_id, ts, prev_data, ok, data, error=None) -> list:
    """Events for one box this cycle: data is what remember() stored now,
    prev_data what was stored before. Nothing is invented on a box's first
    read (no 'before' to compare)."""
    from app import legacy
    out = []
    if ok and prev_data and prev_data.get("status"):
        old = legacy.status_code((prev_data.get("status") or {}).get("callForHeat"))
        new = legacy.status_code(((data or {}).get("status") or {}).get("callForHeat"))
        if old and new and old != new:
            words = lambda code: legacy.STATUS.get(code, (code, ""))[0]
            out.append({"box_id": box_id, "ts": ts, "kind": "status", "code": new[:40],
                        "text": f"{words(old)} -> {words(new)}"[:200]})
        if ("last_alarm" in prev_data and (data or {}).get("alarm")
                and alarm_key(data) != prev_data["last_alarm"]):
            info = legacy.alarm_info(data["alarm"], ts, legacy.clock_drift(data)) or {}
            out.append({"box_id": box_id, "ts": ts, "kind": "alarm", "code": str(data["alarm"].get("name"))[:40],
                        "text": f"{info.get('words', 'Alarm')} at {info.get('when', '?')}"[:200]})
    if not ok and (data or {}).get("fails") == DARK_AFTER_MISSES and prev_data.get("status"):
        out.append({"box_id": box_id, "ts": ts, "kind": "dark", "code": None, "text": (error or "no answer")[:200]})
    elif ok and int(prev_data.get("fails") or 0) >= DARK_AFTER_MISSES:
        out.append({"box_id": box_id, "ts": ts, "kind": "back", "code": None, "text": "answering again"})
    return out


def save(readings: list, events: list, now) -> None:
    """Write one cycle's reads and events (own transaction: a failure here
    never costs the Live page its latest readings, which are already saved)."""
    from app.extensions import db
    from app.models import LegacyEvent, LegacyReading
    ensure_partitions(now)
    if readings:
        db.session.execute(insert(LegacyReading), readings)
    if events:
        db.session.execute(insert(LegacyEvent), events)
    db.session.commit()


# ── monthly partitions ───────────────────────────────────────────────────────

def _month(d):
    return _dt.datetime(d.year, d.month, 1)


def _next_month(m):
    return _dt.datetime(m.year + (m.month == 12), m.month % 12 + 1, 1)


_made = set()


def _partitioned() -> bool:
    from app.extensions import db
    return db.session.execute(text(
        "SELECT relkind FROM pg_class WHERE relname = 'legacy_readings' "
        "AND relnamespace = 'public'::regnamespace")).scalar() == "p"


def ensure_partitions(now) -> None:
    """This month's and next month's partitions exist (a no-op on a plain
    table, as the tests' schema is)."""
    from app.extensions import db
    this = _month(now)
    want = [this, _next_month(this)]
    if all(m in _made for m in want) or not _partitioned():
        return
    for m in want:
        name = f"legacy_readings_{m:%Y_%m}"
        db.session.execute(text(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF legacy_readings "
            f"FOR VALUES FROM ('{m:%Y-%m-%d}') TO ('{_next_month(m):%Y-%m-%d}')"))
        _made.add(m)
    db.session.commit()


def drop_old(now, days) -> list:
    """Drop every monthly partition that ended more than `days` ago."""
    from app.extensions import db
    if not _partitioned():
        return []
    names = db.session.execute(text(
        "SELECT c.relname FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'legacy_readings'")).scalars().all()
    dropped = []
    for name in names:
        m = _PART.match(name)
        if m and _next_month(_dt.datetime(int(m.group(1)), int(m.group(2)), 1)) <= now - _dt.timedelta(days=days):
            db.session.execute(text(f"DROP TABLE {name}"))
            dropped.append(name)
    db.session.commit()
    if dropped:
        log.info("History: dropped %s", ", ".join(dropped))
    return dropped


# ── hourly ───────────────────────────────────────────────────────────────────

def floor_hour(t):
    return t.replace(minute=0, second=0, microsecond=0)


def local(t_utc):
    from app import legacy
    return t_utc.replace(tzinfo=_UTC).astimezone(legacy.LOCAL)


def _ok(v, lo, hi):
    return v if v is not None and lo <= v <= hi else None


def _vals(rows, attr, lo, hi):
    return [v for v in (_ok(getattr(r, attr), lo, hi) for r in rows) if v is not None]


def _mean(v):
    return round(sum(v) / len(v), 1) if v else None


def used(seq, attr, counter=False) -> int:
    """Units used between consecutive reads. Run minutes (counter=False):
    a drop is the box's midnight reset, and a step never exceeds the minutes
    that passed. Meters (counter=True): a drop is not usage."""
    total = 0
    for a, b in zip(seq, seq[1:]):
        x, y = getattr(a, attr), getattr(b, attr)
        if x is None or y is None:
            continue
        gap = (b.ts - a.ts).total_seconds() / 60
        if gap <= 0 or gap > MAX_GAP_MIN:
            continue
        if y >= x:
            d = y - x
        else:
            d = 0 if counter else y
        if not counter:
            d = min(d, gap + 1)
        total += max(d, 0)
    return int(round(total))


def law_minimum(hour_utc, outside):
    """The heat-season minimum for an hour, or None (out of season, or a
    daytime hour that is 55 F or warmer outside)."""
    lt = local(hour_utc)
    if lt.month not in SEASON_MONTHS:
        return None
    if DAY_FROM <= lt.hour < DAY_TO:
        return DAY_MIN if outside is not None and outside < DAY_RULE_BELOW else None
    return NIGHT_MIN


def _tone(code):
    from app import legacy
    return legacy.STATUS.get(code or "", (code, "idle"))[1]


def hour_row(box_id, hour, prev, cur, fleet_out, poll_min, ignore_ignition=False, hot_water=False) -> dict:
    """The hourly row for one box: prev = its last read before the hour (or
    None), cur = its reads inside the hour, oldest first."""
    seq = ([prev] if prev is not None else []) + cur
    outs = _vals(cur, "out", -40, 130)
    out = _mean(outs) if outs else fleet_out
    ins = _vals(cur, "inside", 35, 110)
    dits = _vals(cur, "dit", 40, 90)
    aq = _vals(cur, "aqua", 0, 260)
    stk = _vals(cur, "stack", 0, 1500)
    hst = _vals(cur, "hi_stack", 0, 1500)
    coil = _vals(cur, "coil", 0, 260)
    mix = _vals(cur, "mix", 0, 260)
    width = max((len(r.apt) for r in cur if r.apt), default=0)
    apt_avg = []
    for i in range(width):
        v = [r.apt[i] for r in cur if r.apt and len(r.apt) > i and _ok(r.apt[i], 35, 110) is not None]
        apt_avg.append(int(round(sum(v) / len(v))) if v else None)
    while apt_avg and apt_avg[-1] is None:
        apt_avg.pop()
    apt_all = [v for r in cur for v in (r.apt or []) if _ok(v, 35, 110) is not None]
    req = law_minimum(hour, out)
    minutes = lambda tone: min(60, sum(poll_min for r in cur if _tone(r.code) == tone
                                       and not (ignore_ignition and r.code == "Ign Fail")))
    fuel = used(seq, "fuel_count", counter=True) if any(r.fuel_count for r in cur) else None
    water = used(seq, "water_count", counter=True) if any(r.water_count for r in cur) else None
    return {
        "box_id": box_id, "hour": hour, "n": len(cur),
        "out_avg": out, "out_fleet": not outs and out is not None,
        "in_avg": _mean(ins), "in_min": min(ins) if ins else None, "in_max": max(ins) if ins else None,
        "dit": dits[-1] if dits else None,
        "aqua_avg": _mean(aq), "aqua_max": max(aq) if aq else None,
        "supp_avg": _mean(_vals(cur, "supp", 0, 260)), "retn_avg": _mean(_vals(cur, "retn", 0, 260)),
        "coil_min": min(coil) if coil else None, "coil_max": max(coil) if coil else None,
        "mix_min": min(mix) if mix else None, "mix_max": max(mix) if mix else None,
        "stack_avg": _mean(stk), "stack_max": max(stk) if stk else None, "hi_stack": max(hst) if hst else None,
        "heat_min": min(32767, sum(used(seq, f"heat{i}") for i in range(1, 5))),
        "hw_min": min(32767, sum(used(seq, f"hw{i}") for i in range(1, 5))),
        "calling_min": minutes("heat"), "safety_min": minutes("crit"), "bypass_min": minutes("warn"),
        **_target_stats(cur, poll_min, hot_water),
        "fuel": fuel, "water": water,
        "hdd": round(max(0.0, HDD_BASE - out) / 24, 4) if out is not None else 0.0,
        "law_req": req, "law_low": sum(1 for a in apt_avg if a is not None and req and a < req),
        "apt_low": min(apt_all) if apt_all else None, "apt_avg": apt_avg or None,
    }


def _target_stats(cur, poll_min, hot_water) -> dict:
    """Hot water, the reads calling for heat: average target, average supply,
    and minutes the loop was cold (supply and/or return under
    legacy.LOOP_COLD_F; stored as short_min)."""
    from app import legacy
    if not hot_water:
        return {"dsup_avg": None, "supp_call_avg": None, "short_min": 0}
    calls = [r for r in cur if _tone(r.code) == "heat"]
    supps = [r.supp for r in calls if _ok(r.supp, 1, 260) is not None]
    cold = sum(1 for r in calls if legacy.loop_cold(r.supp, r.retn))
    return {"dsup_avg": _mean([r.dsup for r in calls if r.dsup]), "supp_call_avg": _mean(supps),
            "short_min": min(60, cold * poll_min)}


def rollup_hour(hour, poll_min=5) -> int:
    """(Re)build every box's row for one hour; returns how many."""
    from app.extensions import db
    from app.models import LegacyHourly, LegacyReading
    rows = LegacyReading.query.filter(LegacyReading.ts >= hour - LOOKBACK, LegacyReading.ts < hour + HOUR) \
        .order_by(LegacyReading.box_id, LegacyReading.ts).all()
    by_box = {}
    for r in rows:
        by_box.setdefault(r.box_id, []).append(r)
    groups = {}
    for box_id, rs in by_box.items():
        cur = [r for r in rs if r.ts >= hour]
        if cur:
            before = [r for r in rs if r.ts < hour]
            groups[box_id] = (before[-1] if before else None, cur)
    outs = [_mean(_vals(cur, "out", -40, 130)) for _p, cur in groups.values()]
    outs = [o for o in outs if o is not None]
    fleet_out = round(statistics.median(outs), 1) if outs else None
    db.session.query(LegacyHourly).filter(LegacyHourly.hour == hour).delete()
    # hot-water boxes: an "Ign Fail" status is not counted as Safety time (legacy.ignition_ignored)
    from app import legacy
    from app.models import LegacyStatus
    hot = {s.box_id for s in LegacyStatus.query.filter(LegacyStatus.box_id.in_(list(groups) or [-1])).all()
           if legacy.ignition_ignored(s.data)}
    out = [hour_row(box_id, hour, prev, cur, fleet_out, poll_min, ignore_ignition=box_id in hot, hot_water=box_id in hot)
           for box_id, (prev, cur) in groups.items()]
    if out:
        db.session.execute(insert(LegacyHourly), out)
    return len(out)


# ── daily ────────────────────────────────────────────────────────────────────

def day_bounds(day):
    """A New York calendar day as a UTC [start, end) (23 or 25 hours at DST)."""
    from app import legacy
    start = _dt.datetime.combine(day, _dt.time(0), tzinfo=legacy.LOCAL)
    end = _dt.datetime.combine(day + _dt.timedelta(days=1), _dt.time(0), tzinfo=legacy.LOCAL)
    return (start.astimezone(_UTC).replace(tzinfo=None), end.astimezone(_UTC).replace(tzinfo=None))


def day_row(box_id, day, hrs) -> dict:
    s = lambda a: sum(getattr(h, a) or 0 for h in hrs)
    opt = lambda a: (sum(getattr(h, a) for h in hrs if getattr(h, a) is not None)
                     if any(getattr(h, a) is not None for h in hrs) else None)
    outs = [h.out_avg for h in hrs if h.out_avg is not None]
    ins = [h.in_avg for h in hrs if h.in_avg is not None]
    fire = [h.stack_max for h in hrs if h.stack_max is not None and h.heat_min >= 10]
    night = [h.water for h in hrs if h.water is not None and 1 <= local(h.hour).hour < 5]
    lows = [h.apt_low for h in hrs if h.apt_low is not None]
    dits = [h.dit for h in hrs if h.dit is not None]
    return {
        "box_id": box_id, "day": day, "n": s("n"), "hours": len(hrs),
        "out_avg": _mean(outs), "out_min": min(outs) if outs else None, "out_max": max(outs) if outs else None,
        "hdd": round(s("hdd"), 2),
        "in_avg": _mean(ins),
        "in_min": min((h.in_min for h in hrs if h.in_min is not None), default=None),
        "in_max": max((h.in_max for h in hrs if h.in_max is not None), default=None),
        "dit": dits[-1] if dits else None,
        "heat_min": s("heat_min"), "hw_min": s("hw_min"), "calling_min": s("calling_min"),
        "safety_min": s("safety_min"), "fuel": opt("fuel"), "water": opt("water"),
        "water_night": sum(night) if night else None,
        "law_hours": sum(1 for h in hrs if h.law_low), "law_sensor_hours": s("law_low"),
        "apt_low": min(lows) if lows else None, "stack_fire": _mean(fire),
        "overheat_hours": sum(1 for h in hrs if h.in_avg is not None and h.dit and h.heat_min > 0
                              and h.in_avg >= h.dit + OVERHEAT),
        "warm_heat_min": sum(h.heat_min for h in hrs if h.out_avg is not None and h.out_avg >= WARM_OUTSIDE),
    }


def rollup_day(day) -> int:
    from app.extensions import db
    from app.models import LegacyDaily, LegacyHourly
    start, end = day_bounds(day)
    by_box = {}
    for h in LegacyHourly.query.filter(LegacyHourly.hour >= start, LegacyHourly.hour < end) \
            .order_by(LegacyHourly.hour).all():
        by_box.setdefault(h.box_id, []).append(h)
    db.session.query(LegacyDaily).filter(LegacyDaily.day == day).delete()
    out = [day_row(box_id, day, hrs) for box_id, hrs in by_box.items()]
    if out:
        db.session.execute(insert(LegacyDaily), out)
    return len(out)


# ── keeping it all up to date ────────────────────────────────────────────────

def rollup(now, poll_min=5, max_hours=48, max_days=7):
    """Sum every finished hour and New York day not yet summed (a bounded
    amount per call, so catching up after downtime never holds up polling).
    Returns (hours, days) processed."""
    from app.extensions import db
    from app.models import LegacyDaily, LegacyHourly, LegacyReading
    end = floor_hour(now - SETTLE)
    last = db.session.query(func.max(LegacyHourly.hour)).scalar()
    if last is not None:
        h = last + HOUR
    else:
        first = db.session.query(func.min(LegacyReading.ts)).scalar()
        h = floor_hour(first) if first else end
    hours = 0
    while h < end and hours < max_hours:
        n = rollup_hour(h, poll_min)
        hours += 1
        if n:
            h += HOUR
        else:                                    # nothing that hour: skip to the next read
            nxt = db.session.query(func.min(LegacyReading.ts)).filter(LegacyReading.ts >= h + HOUR).scalar()
            h = floor_hour(nxt) if nxt and floor_hour(nxt) < end else end
    db.session.commit()
    summed_to = h                                # every hour before this is summed
    last_day = db.session.query(func.max(LegacyDaily.day)).scalar()
    if last_day is not None:
        d = last_day + _dt.timedelta(days=1)
    else:
        first = db.session.query(func.min(LegacyHourly.hour)).scalar()
        d = local(first).date() if first else None
    days = 0
    while d is not None and day_bounds(d)[1] <= summed_to and days < max_days:
        n = rollup_day(d)
        days += 1
        if n:
            d += _dt.timedelta(days=1)
        else:
            nxt = db.session.query(func.min(LegacyHourly.hour)).filter(LegacyHourly.hour >= day_bounds(d)[1]).scalar()
            d = local(nxt).date() if nxt else None
    db.session.commit()
    return hours, days


def maintain(app, now=None):
    """After each poll cycle: sum what finished, drop reads past retention."""
    now = now or _dt.datetime.utcnow()
    poll_min = max(1, round(app.config.get("LEGACY_POLL_INTERVAL_SECONDS", 300) / 60))
    hours, days = rollup(now, poll_min)
    drop_old(now, int(app.config.get("LEGACY_RAW_DAYS", 90)))
    if hours or days:
        log.info("History: summed %d hour(s), %d day(s)", hours, days)
    return hours, days
