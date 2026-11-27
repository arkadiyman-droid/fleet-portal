"""
app/fan_effect.py — the apartments under the roof fans, and what the fans do to them

Roof exhaust fans pull warm air out of a building. Whether that cools the
apartments, and what it costs in heat, is the question this module answers
for the Fans page (the user, 2026-11-26).

The fan buildings are on Bloom (fan points); their heat and apartment sensors
are on the building's 2010 box. A fan site is filed under a portal Building
(bloom_fans.place); the box on that Building is the one read here.

  apartments(building_ids)      {building_id: {avg, target, live, ...}} now
  history(building_id, days)    the apartment average, its target and the
                                burner on/off over time, for the row's chart
  study(now)                    every time a building's fans switched off or on,
                                what the apartments and the burner did over the
                                next WINDOW_H hours, against the same hours on
                                nearby days when the fans did not switch

THE STUDY, and why it is built this way. A plain "hours with fans on vs off"
comparison shows almost nothing (measured 2026-11-26: +0.01 F/h across 23
buildings), for three reasons: the heat fights back (cooler apartments make
the box run the burner longer, so the cost shows up as burner time, not
temperature); fans are off at particular hours and in particular weather;
and the apartment average is in whole degrees. So:

  * an EVENT is a building's fans switching off (or on) and staying that way
    for the whole window, after being the other way for the hour before;
  * its CONTROLS are the same building at the same clock time on other days
    within CONTROL_DAYS, where the fans stayed as they were before the event
    and it was within CONTROL_OUT_F degrees outside;
  * for both: how much the apartment average moved from the hour before to
    the last hour of the window, and how many minutes the burner ran in it;
  * the effect of an event = its numbers minus the average of its controls.
    Averaged over many events, the time of day, the weather and the
    building's own habits cancel out.

Each gunicorn worker keeps the study for CACHE_S (it takes a few seconds).
"""
import bisect
import datetime as _dt
import math
import statistics
import threading
import time

WINDOW_H = 2                 # hours after a switch that are looked at
BEFORE_MIN = 60              # the baseline: the hour before the switch
HOLD_TOL = 0.15              # share of a window's fan samples allowed in the other state
ON_SHARE, OFF_SHARE = 0.5, 0.1   # a building's fans count as on / off at these shares of its fans running
MAX_GAP_MIN = 20             # samples further apart than this break a run
CONTROL_DAYS = 7             # controls come from this many days either side
CONTROL_SHIFT_MIN = 20       # ...at the same clock time, give or take this
CONTROL_OUT_F = 6            # ...and this close outside
MAX_DAYS = 30                # history looked at
CACHE_S = 1800
PROFILE_STEP_MIN = 15        # the event-average chart: one point per this many minutes


# ── the apartments now ───────────────────────────────────────────────────────

def apartments(building_ids) -> dict:
    """{building_id: {avg, target, live, no_sensors, box_id, box}} from the
    2010 box on each building (the Live wall's own reading of it)."""
    from sqlalchemy.orm import joinedload
    from app import legacy
    from app.models import LegacyBox
    ids = [i for i in set(building_ids) if i]
    if not ids:
        return {}
    now = _dt.datetime.utcnow()
    out = {}
    for b in (LegacyBox.query.options(joinedload(LegacyBox.status))
              .filter(LegacyBox.building_id.in_(ids), LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None))
              .order_by(LegacyBox.id).all()):
        if b.building_id in out:
            continue                                 # one box per building: the first, as everywhere else
        st = b.status
        c = legacy.card(st.data, guard_out=True) if st and st.data else None
        live = bool(st and st.last_ok_at and now - st.last_ok_at <= legacy.STALE)
        out[b.building_id] = {"box_id": b.id, "box": b.name, "live": live and bool(c),
                              "avg": (c or {}).get("inside"), "target": (c or {}).get("dit"),
                              "no_sensors": bool((c or {}).get("no_sensors"))}
    return out


def tone(a) -> str:
    """'hot' / 'warm' / 'ok' / 'cool' / 'none' for an apartments() entry."""
    if not a or not a.get("live") or a.get("no_sensors") or a.get("avg") is None or a.get("target") is None:
        return "none"
    d = a["avg"] - a["target"]
    return "hot" if d >= 5 else "warm" if d >= 2 else "cool" if d <= -2 else "ok"


def group_average(rows) -> dict:
    """{avg, target, n} over the rows' live apartment readings (one per building)."""
    seen, avgs, tgts = set(), [], []
    for r in rows:
        a = r.get("apts")
        if tone(a) == "none" or a["box_id"] in seen:
            continue
        seen.add(a["box_id"])
        avgs.append(a["avg"])
        tgts.append(a["target"])
    return {"avg": round(sum(avgs) / len(avgs), 1) if avgs else None,
            "target": round(sum(tgts) / len(tgts), 1) if tgts else None, "n": len(avgs)}


# ── the row's chart ──────────────────────────────────────────────────────────

def _heat_sum(r):
    return sum(v or 0 for v in (r.heat1, r.heat2, r.heat3, r.heat4))


def _ran(prev_heat, heat, gap_min):
    """Burner minutes between two reads from the run-minute counters (which
    reset at midnight: a drop counts the new value)."""
    if heat is None or prev_heat is None or gap_min <= 0 or gap_min > MAX_GAP_MIN:
        return None
    d = heat - prev_heat if heat >= prev_heat else heat
    return max(0, min(d, gap_min + 1))


def history(box_id, days, now=None) -> dict:
    """{t, avg, target, burner} for the chart under a fan row. Every read up to
    7 days (burner = 1 when it ran since the read before); the hourly
    roll-up beyond (burner = share of the hour it ran)."""
    from app.extensions import db
    from app.models import LegacyHourly, LegacyReading
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    ms = lambda t: int(t.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)
    out = {"t": [], "avg": [], "target": [], "burner": []}
    if days > 7:
        H = LegacyHourly
        for h in (db.session.query(H.hour, H.in_avg, H.dit, H.heat_min)
                  .filter(H.box_id == box_id, H.hour >= start).order_by(H.hour)):
            out["t"].append(ms(h.hour))
            out["avg"].append(round(h.in_avg, 1) if h.in_avg else None)
            out["target"].append(h.dit if h.dit and 40 <= h.dit <= 99 else None)
            out["burner"].append(round(min(1.0, (h.heat_min or 0) / 60), 2))
        out["res"] = "hourly averages"
        return out
    R = LegacyReading
    prev = None
    for r in (db.session.query(R.ts, R.inside, R.dit, R.heat1, R.heat2, R.heat3, R.heat4)
              .filter(R.box_id == box_id, R.ts >= start - _dt.timedelta(minutes=15)).order_by(R.ts)):
        heat = _heat_sum(r)
        if r.ts >= start:
            ran = _ran(prev[1], heat, (r.ts - prev[0]).total_seconds() / 60) if prev else None
            out["t"].append(ms(r.ts))
            out["avg"].append(r.inside if r.inside else None)
            out["target"].append(r.dit if r.dit and 40 <= r.dit <= 99 else None)
            out["burner"].append(None if ran is None else (1 if ran > 0 else 0))
        prev = (r.ts, heat)
    out["res"] = "every read"
    return out


# ── the study ────────────────────────────────────────────────────────────────

def _state(share):
    if share is None:
        return None
    return "on" if share >= ON_SHARE else "off" if share <= OFF_SHARE else "mixed"


def events(fan, start=None, end=None, window_h=WINDOW_H):
    """Switch events in one building's fan samples [(ts, share), ...] oldest
    first: [(ts, 'off' | 'on')] where the fans had been the other way for the
    hour before and stay the new way for window_h hours (HOLD_TOL slack).
    A switch may ramp: groups and fans often start one after another, so the
    readings between the old and the new state may be part-way ('mixed') for
    up to MAX_GAP_MIN; the event is timed from the first reading that left
    the old state."""
    out = []
    fan = _series(fan)
    n = len(fan)
    i = 1
    while i < n:
        a = _state(fan[i - 1][1])
        if a not in ("on", "off") or _state(fan[i][1]) == a:
            i += 1
            continue
        t_left = fan[i][0]
        j = i
        while j < n and _state(fan[j][1]) not in ("on", "off") and (fan[j][0] - t_left).total_seconds() / 60 <= MAX_GAP_MIN:
            j += 1                                   # the ramp
        if j >= n:
            break
        b = _state(fan[j][1])
        if b in ("on", "off") and b != a and (t_left - fan[i - 1][0]).total_seconds() / 60 <= MAX_GAP_MIN                 and (fan[j][0] - t_left).total_seconds() / 60 <= MAX_GAP_MIN                 and not (start and t_left < start) and not (end and t_left > end)                 and _holds(fan, t_left - _dt.timedelta(minutes=BEFORE_MIN), t_left, a)                 and _holds(fan, fan[j][0], t_left + _dt.timedelta(hours=window_h), b):
            out.append((t_left, b))
        i = j + 1
    return out


class _Series(list):
    """A list of (ts, ...) tuples, oldest first, with its timestamps kept apart
    so a time window is found by binary search, not a scan."""
    def __init__(self, rows):
        super().__init__(rows)
        self.ts = [r[0] for r in self]

    def between(self, t0, t1, inclusive=False):
        i = bisect.bisect_left(self.ts, t0)
        j = bisect.bisect_right(self.ts, t1) if inclusive else bisect.bisect_left(self.ts, t1)
        return self[i:j]


def _series(rows):
    return rows if isinstance(rows, _Series) else _Series(rows)


def _holds(fan, t0, t1, state, frm=0, upto=None):
    """True when the samples in [t0, t1) are (nearly all) `state`, with no gap
    longer than MAX_GAP_MIN and the span actually covered."""
    seg = _series(fan).between(t0, t1)
    if not seg or (seg[0][0] - t0).total_seconds() / 60 > MAX_GAP_MIN or (t1 - seg[-1][0]).total_seconds() / 60 > MAX_GAP_MIN:
        return False
    for (ta, _), (tb, _) in zip(seg, seg[1:]):
        if (tb - ta).total_seconds() / 60 > MAX_GAP_MIN:
            return False
    other = sum(1 for _, s in seg if _state(s) != state)
    return other <= HOLD_TOL * len(seg)


def _window(reads, t, window_h=WINDOW_H):
    """What the apartments and the burner did around time t, from one box's
    reads [(ts, inside, out, heat), ...]: {delta, burner, out, profile} or None.
    delta = average of the last hour of the window minus the hour before t."""
    t0, t1 = t - _dt.timedelta(minutes=BEFORE_MIN), t + _dt.timedelta(hours=window_h)
    seg = _series(reads).between(t0 - _dt.timedelta(minutes=15), t1, inclusive=True)
    before = [r[1] for r in seg if t0 <= r[0] < t and r[1]]
    last = [r[1] for r in seg if t1 - _dt.timedelta(minutes=60) <= r[0] <= t1 and r[1]]
    if len(before) < 6 or len(last) < 6:
        return None                                  # need about an hour of reads each side
    burner, prev, covered = 0, None, 0.0
    for r in seg:
        if prev is not None and t <= r[0] <= t1:
            gap = (r[0] - prev[0]).total_seconds() / 60
            ran = _ran(prev[3], r[3], gap)
            if ran is not None:
                burner += ran
                covered += gap
        prev = r
    if covered < window_h * 60 * 0.8:
        return None
    base = sum(before) / len(before)
    profile = {}
    for r in seg:
        if r[1]:
            k = int(((r[0] - t).total_seconds() / 60) // PROFILE_STEP_MIN) * PROFILE_STEP_MIN
            if -BEFORE_MIN <= k < window_h * 60:
                profile.setdefault(k, []).append(r[1] - base)
    outs = [r[2] for r in seg if r[2] is not None and -40 <= r[2] <= 130]
    return {"delta": sum(last) / len(last) - base, "burner": burner * (window_h * 60 / covered),
            "out": sum(outs) / len(outs) if outs else None,
            "profile": {k: sum(v) / len(v) for k, v in profile.items()}}


def _controls(fan, reads, t, before_state, out_f, window_h=WINDOW_H):
    """The same building at the same clock time on other days, fans steady in
    `before_state` throughout, about as cold outside."""
    found = []
    for day in range(-CONTROL_DAYS, CONTROL_DAYS + 1):
        if day == 0:
            continue
        for shift in (0, -CONTROL_SHIFT_MIN, CONTROL_SHIFT_MIN):
            tc = t + _dt.timedelta(days=day, minutes=shift)
            if not _holds(fan, tc - _dt.timedelta(minutes=BEFORE_MIN), tc + _dt.timedelta(hours=window_h), before_state):
                continue
            w = _window(reads, tc, window_h)
            if w is None or (out_f is not None and w["out"] is not None and abs(w["out"] - out_f) > CONTROL_OUT_F):
                continue
            found.append(w)
            break                                    # one control per day
    return found


def analyse(sites, window_h=WINDOW_H) -> dict:
    """The study over prepared data, pure: `sites` = [{site, name, prop, fan:
    [(ts, share)], reads: [(ts, inside, out, heat)]}]. Returns {"off": summary,
    "on": summary, "by_prop": {...}, "by_building": [...], "events": n}."""
    found = []
    for s in sites:
        if len(s["fan"]) < 10 or len(s["reads"]) < 10:
            continue
        s = {**s, "fan": _series(s["fan"]), "reads": _series(s["reads"])}
        for t, new in events(s["fan"], window_h=window_h):
            w = _window(s["reads"], t, window_h)
            if w is None:
                continue
            ctl = _controls(s["fan"], s["reads"], t, "on" if new == "off" else "off", w["out"], window_h)
            if not ctl:
                continue
            cd = sum(c["delta"] for c in ctl) / len(ctl)
            cb = sum(c["burner"] for c in ctl) / len(ctl)
            prof = {}
            for k in set(w["profile"]):
                cv = [c["profile"][k] for c in ctl if k in c["profile"]]
                if cv:
                    prof[k] = (w["profile"][k], sum(cv) / len(cv))
            found.append({"site": s["site"], "name": s["name"], "prop": s.get("prop"), "t": t, "dir": new,
                          "effect": w["delta"] - cd, "burner_effect": w["burner"] - cb,
                          "delta": w["delta"], "control_delta": cd, "burner": w["burner"], "control_burner": cb,
                          "out": w["out"], "controls": len(ctl), "profile": prof})

    def summarise(evs):
        if not evs:
            return None
        e = [x["effect"] for x in evs]
        bu = [x["burner_effect"] for x in evs]
        se = lambda v: (statistics.stdev(v) / math.sqrt(len(v))) if len(v) > 1 else None
        steps = sorted({k for x in evs for k in x["profile"]})
        profile = []
        for k in steps:
            pairs = [x["profile"][k] for x in evs if k in x["profile"]]
            if len(pairs) >= max(3, len(evs) // 3):
                profile.append({"min": k, "event": sum(p[0] for p in pairs) / len(pairs),
                                "control": sum(p[1] for p in pairs) / len(pairs)})
        return {"n": len(evs), "buildings": len({x["site"] for x in evs}),
                "effect": sum(e) / len(e), "se": se(e),
                "burner": sum(bu) / len(bu), "burner_se": se(bu),            # minutes, event minus its controls
                "burner_event": sum(x["burner"] for x in evs) / len(evs),
                "burner_control": sum(x["control_burner"] for x in evs) / len(evs),
                "delta": sum(x["delta"] for x in evs) / len(evs),
                "control_delta": sum(x["control_delta"] for x in evs) / len(evs),
                "out": _mean([x["out"] for x in evs]), "profile": profile}

    by_prop = {}
    for p in sorted({x["prop"] or "Not filed" for x in found}):
        sel = [x for x in found if (x["prop"] or "Not filed") == p]
        by_prop[p] = {"off": summarise([x for x in sel if x["dir"] == "off"]),
                      "on": summarise([x for x in sel if x["dir"] == "on"])}
    by_building = []
    for sid in sorted({x["site"] for x in found}):
        sel = [x for x in found if x["site"] == sid]
        by_building.append({"site": sid, "name": sel[0]["name"], "prop": sel[0]["prop"],
                            "off": summarise([x for x in sel if x["dir"] == "off"]),
                            "on": summarise([x for x in sel if x["dir"] == "on"])})
    return {"off": summarise([x for x in found if x["dir"] == "off"]),
            "on": summarise([x for x in found if x["dir"] == "on"]),
            "by_prop": by_prop, "by_building": by_building, "events": len(found),
            "window_h": window_h}


def _mean(v):
    v = [x for x in v if x is not None]
    return sum(v) / len(v) if v else None


def gather(site_rows, now=None, days=MAX_DAYS) -> list:
    """The study's input from the database: site_rows = [(site_id, name, prop,
    box_id)]. Fan shares from bloom_fan_samples, apartment / outdoor / burner
    reads from legacy_readings."""
    from sqlalchemy import text
    from app.extensions import db
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    site_ids = [s[0] for s in site_rows]
    box_ids = sorted({s[3] for s in site_rows if s[3]})
    if not site_ids or not box_ids:
        return []
    fan = {}
    for sid, ts, on, total in db.session.execute(text(
            "SELECT site_id, ts, (data->>'on')::float, (data->>'total')::float FROM bloom_fan_samples "
            "WHERE site_id = ANY(:ids) AND ts >= :start ORDER BY site_id, ts"), {"ids": site_ids, "start": start}):
        if total:
            fan.setdefault(sid, []).append((ts, on / total))
    # the box reads are only needed where there is fan history (from 2026-11-16 on)
    firsts = [v[0][0] for v in fan.values() if v]
    read_from = max(start, min(firsts) - _dt.timedelta(days=1)) if firsts else start
    reads = {}
    for bid, ts, inside, out, h1, h2, h3, h4 in db.session.execute(text(
            "SELECT box_id, ts, inside, out, heat1, heat2, heat3, heat4 FROM legacy_readings "
            "WHERE box_id = ANY(:ids) AND ts >= :start ORDER BY box_id, ts"), {"ids": box_ids, "start": read_from}):
        heat = None if all(v is None for v in (h1, h2, h3, h4)) else sum(v or 0 for v in (h1, h2, h3, h4))
        reads.setdefault(bid, []).append((ts, inside if inside and inside != 65 else None, out, heat))
    db.session.rollback()
    return [{"site": sid, "name": name, "prop": prop, "fan": fan.get(sid, []), "reads": reads.get(bid, [])}
            for sid, name, prop, bid in site_rows if bid]


_cache, _cache_lock = {}, threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


STALE_S = 6 * 3600              # an older answer is worked out again before it is shown
_refreshing = threading.Event()


def _compute(site_rows, now=None) -> dict:
    now = now or _dt.datetime.utcnow()
    data = gather(site_rows, now)
    res = analyse(data)
    firsts = [s["fan"][0][0] for s in data if s["fan"]]
    res.update(computed_at=now, since=min(firsts) if firsts else None,
               buildings_with_data=sum(1 for s in data if s["fan"] and s["reads"]))
    return res


def _store(key, res):
    with _cache_lock:
        _cache.clear()
        _cache[key] = (time.monotonic(), res)


def study(site_rows, now=None, app=None) -> dict:
    """analyse(gather(...)) for the Fans effect page. Under CACHE_S old it is
    shown as is; under STALE_S it is shown at once and worked out again in the
    background (`app` given); older or missing, it is worked out now (a few
    seconds). Adds {computed_at, since, buildings_with_data}."""
    key, t = tuple(sorted(site_rows)), time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and t - hit[0] < STALE_S:
        if t - hit[0] >= CACHE_S and app is not None and not _refreshing.is_set():
            _refreshing.set()

            def run():
                try:
                    with app.app_context():
                        _store(key, _compute(site_rows))
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception("fan effect refresh failed")
                finally:
                    _refreshing.clear()
            threading.Thread(target=run, name="fan-effect", daemon=True).start()
        return hit[1]
    res = _compute(site_rows, now)
    _store(key, res)
    return res


def verdict(x, min_n=10) -> str:
    """'clear' / 'unclear' / 'few' for a summarise() result: is the effect
    larger than its 95% margin, with enough switches to say?"""
    if not x or x["n"] < min_n:
        return "few"
    return "clear" if x["se"] and abs(x["effect"]) > 1.96 * x["se"] else "unclear"


def chart(profile, w=560, h=200, pad=34) -> dict:
    """The event-average curve as SVG coordinates: x = minutes from the switch,
    y = how far the apartments moved from the hour before it. {event, control:
    "x,y x,y ...", zero, ticks: [(x, label)], yticks: [(y, label)], w, h} or
    None when there is nothing to draw."""
    if not profile:
        return None
    xs = [p["min"] for p in profile]
    vals = [v for p in profile for v in (p["event"], p["control"])]
    lo, hi = min(vals + [0]), max(vals + [0])
    span = max(hi - lo, 0.2)
    lo, hi = lo - span * 0.15, hi + span * 0.15
    x0, x1 = min(xs), max(xs) + PROFILE_STEP_MIN
    X = lambda m: pad + (m - x0) / (x1 - x0) * (w - pad - 10)
    Y = lambda v: 10 + (hi - v) / (hi - lo) * (h - 10 - 24)
    pts = lambda k: " ".join(f"{X(p['min'] + PROFILE_STEP_MIN / 2):.1f},{Y(p[k]):.1f}" for p in profile)
    step = 0.1 if hi - lo < 0.8 else 0.25 if hi - lo < 2 else 0.5
    yt, v = [], math.floor(lo / step) * step
    while v <= hi:
        if v >= lo:
            yt.append((round(Y(v), 1), f"{v:+.1f}°".replace("+0.0", "0").replace("-0.0", "0")))
        v += step
    return {"event": pts("event"), "control": pts("control"), "zero": round(Y(0), 1), "switch": round(X(0), 1),
            "ticks": [(round(X(m), 1), f"{m:+d} min" if m else "switch") for m in range(int(x0), int(x1) + 1, 30)],
            "yticks": yt, "w": w, "h": h, "pad": pad}
