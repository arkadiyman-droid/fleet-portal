"""
app/sensor_health.py — find bad sensors across the 2010 boxes (/sensors report)

A temperature probe fails in a few recognisable ways; this flags them from the
stored reads (LegacyReading) over a recent window. Water-loop and stack sensors
only for now — they are typed columns where 0 means "no sensor"; the outdoor and
apartment channels have placeholder semantics (median outdoor, the 65 F
no-sensor inside) and are left for a later pass.

Faults, highest-confidence first (every threshold is a tunable constant):
  * stuck-high  — an open / disconnected probe rails to full scale: reads over
                  the channel CEILING for most of the window (e.g. 620-630 Gates
                  Ave, mix pinned ~255 F for days, 2026-11).
  * dropped-out — a probe that HAD been reading has sat absent (0) for FLAT_HOURS.
  * frozen      — a live probe always wiggles a little; the SAME value for
                  FLAT_HOURS is a stale / disconnected reading.
  * noisy       — many large jumps between consecutive reads: a loose / failing
                  probe (the isolated single-read glitches are already removed by
                  app/despike, so this looks at the jumps that remain).

scan(now) returns one finding per bad (box, sensor), worst-first.
"""
import datetime as _dt
import itertools
import threading
import time

WINDOW_DAYS = 3
FLAT_HOURS = 8                 # a frozen / dead run must last at least this long
RAIL_FRAC = 0.5               # this share of live reads over/under the limit = railed
MIN_READS = 24               # need at least this many live reads to judge a channel
FLAT_MIN_READS = 40          # and this many identical reads for a confident flatline
FROZEN_HOT = 110             # a FLAT reading only counts as frozen when it is this hot:
                             # a hot loop cycles, so a hot sensor that never moves is
                             # stuck, whereas a loop idling at ~70-90 F sits flat for real.
NOISE_JUMP = 40              # F between consecutive reads that counts as a jolt
NOISE_PER_DAY = 12           # this many jolts a day = a noisy probe
MAX_GAP = _dt.timedelta(minutes=70)      # a longer gap breaks a run (the box was dark)

# attr, label, ceiling F, floor F (None = skip), priority (1 = control/safety), faults, wireless
# The riser is a wireless Inovonics sensor with a remote probe (the user, 2026-11-06);
# a dead one holds its last value, fails open, or stops reporting. The rest are wired to
# the controller in the boiler room.
_WATER = frozenset({"stuck", "stucklow", "dropped", "frozen", "noisy"})
CHANNELS = [
    ("aqua", "Aquastat", 260, 40, 1, _WATER, False),
    ("supp", "Supply", 230, 40, 1, _WATER, False),
    ("mix", "DHW mix", 230, 40, 2, _WATER, False),
    ("retn", "Return", 230, 40, 3, _WATER, False),
    ("coil", "Coil", 230, 40, 3, _WATER, False),
    ("risr", "Riser", 230, 40, 3, _WATER, True),
    ("stack", "Stack", 1500, None, 3, frozenset({"stuck"}), False),  # stack swings for real; rail only
    ("hi_stack", "Hi stack", 1500, None, 4, frozenset({"stuck"}), False),
]
ATTRS = [c[0] for c in CHANNELS]

_LABELS = {"stuck": "Stuck high", "stucklow": "Stuck low", "dropped": "Dropped out",
           "frozen": "Frozen", "noisy": "Noisy"}


def _present(v):
    return v is not None and v != 0            # 0 = no sensor on these channels


def _hrs(td):
    h = td.total_seconds() / 3600
    return f"{h / 24:.0f} days" if h >= 48 else f"{h:.0f} h"


def _runs(times, vals):
    """Yield (start, end, value, n) for each maximal run of an identical present
    value (n >= 2), broken by a gap longer than MAX_GAP or a different value."""
    v0 = s = e = None
    n = 0
    for t, v in zip(times, vals):
        if v and v == v0 and (t - e) <= MAX_GAP:         # `v` true = _present(v): not None, not 0
            e = t
            n += 1
        else:
            if v0 is not None and n >= 2:
                yield s, e, v0, n
            if v:
                v0, s, e, n = v, t, t, 1
            else:
                v0, n = None, 0
    if v0 is not None and n >= 2:
        yield s, e, v0, n


def fault(times, vals, ch, now):
    """The worst fault for one channel of one box, or None. `times`/`vals` are
    the window's reads in order (vals may hold None / 0 for missing / no sensor)."""
    attr, label, ceiling, floor, prio, faults, wireless = ch
    live = [(t, v) for t, v in zip(times, vals) if v]           # present: not None, not 0
    nlive = len(live)
    why = lambda w, wired: f" — {w}" if wireless else f" — {wired}"

    if "stuck" in faults and nlive >= MIN_READS:
        railed = [(t, v) for t, v in live if v > ceiling]
        if len(railed) / nlive >= RAIL_FRAC:
            avg = round(sum(v for _t, v in railed) / len(railed))
            return _mk("stuck", label, prio, railed[0][0], now,
                       f"reading {avg}° (over {ceiling}°) on {len(railed)} of {nlive} reads"
                       + why("the wireless Inovonics probe has failed open", "an open probe"))

    if "stucklow" in faults and floor is not None and nlive >= MIN_READS:
        low = [(t, v) for t, v in live if v < floor]
        if len(low) / nlive >= RAIL_FRAC:
            avg = round(sum(v for _t, v in low) / len(low))
            return _mk("stucklow", label, prio, low[0][0], now,
                       f"reading {avg}° (under {floor}°) on {len(low)} of {nlive} reads"
                       + why("the wireless Inovonics probe is shorted or failed", "a shorted or miscalibrated probe"))

    if "dropped" in faults and nlive >= MIN_READS:
        last_i = next((i for i in range(len(vals) - 1, -1, -1) if vals[i]), None)   # the last present read
        if last_i is not None and last_i < len(vals) - 1:            # ...followed only by absent ones
            dead = now - times[last_i]
            if dead >= _dt.timedelta(hours=FLAT_HOURS):
                return _mk("dropped", label, prio + 1, times[last_i], now,
                           f"was reading, then nothing for {_hrs(dead)} (last {vals[last_i]}°)"
                           + why("the wireless Inovonics probe stopped reporting", "the sensor stopped reporting"))

    # a frozen run needs a hot reading somewhere in the window: skip the walk when there is none
    if "frozen" in faults and any(FROZEN_HOT <= v <= ceiling for _t, v in live):
        best = None
        for s, e, val, n in _runs(times, vals):
            # only a HOT flat line is a fault; a loop idling at ~70-90 F sits flat for real
            if FROZEN_HOT <= val <= ceiling and e - s >= _dt.timedelta(hours=FLAT_HOURS) and n >= FLAT_MIN_READS:
                if best is None or e - s > best[1] - best[0]:
                    best = (s, e, val, n)
        if best:
            s, e, val, n = best
            return _mk("frozen", label, prio, s, now,
                       f"stuck at exactly {val}° for {_hrs(e - s)} ({n} reads)"
                       + why("a dead wireless Inovonics probe holding its last value", "a hot loop should cycle"))

    if "noisy" in faults and nlive >= MIN_READS:
        jolts = sum(1 for (ta, va), (tb, vb) in zip(live, live[1:])
                    if tb - ta <= MAX_GAP and abs(vb - va) > NOISE_JUMP)
        days = max(1.0, (live[-1][0] - live[0][0]).total_seconds() / 86400)
        if jolts / days >= NOISE_PER_DAY:
            return _mk("noisy", label, prio + 2, live[0][0], now,
                       f"{round(jolts / days)} big jumps a day (over {NOISE_JUMP}°)"
                       + why("a flaky wireless Inovonics probe", "a loose probe"))
    return None


def _mk(kind, sensor, prio, since, now, evidence):
    return {"kind": kind, "fault": _LABELS[kind], "sensor": sensor, "priority": prio,
            "since": since, "age": _hrs(now - since), "evidence": evidence}


def _box_findings(box_id, box_name, d, now):
    """Every bad channel for one box's window of reads (d: {'t': [...], attr: [...]})."""
    out = []
    for ch in CHANNELS:
        f = fault(d["t"], d[ch[0]], ch, now)
        if f:
            out.append({**f, "box_id": box_id, "box": box_name, "attr": ch[0], "wireless": ch[6]})
    return out


# The /sensors page reads three days of every box (~150,000 reads) and walks them:
# about 3 s on the Pi. The boxes are read every 5 minutes and every fault here
# needs hours of evidence, so a slightly older answer says the same thing:
#   * under FRESH_S old        -> shown as is
#   * under STALE_S old        -> shown at once, and worked out again in the
#                                 background for the next visit
#   * older, or none yet       -> worked out now (the visitor waits)
# Each gunicorn worker keeps its own copy. The page shows when it was checked.
FRESH_S = 300
STALE_S = 1800
_cache, _cache_lock = {}, threading.Lock()
_refreshing = threading.Event()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _store(out, mono, at):
    with _cache_lock:
        _cache["scan"] = (mono, out, at)


def _refresh_in_background(app):
    def run():
        try:
            with app.app_context():
                at = _dt.datetime.utcnow()
                _store(_scan(at), time.monotonic(), at)
        except Exception:
            import logging
            logging.getLogger(__name__).exception("sensor scan refresh failed")
        finally:
            _refreshing.clear()
    threading.Thread(target=run, name="sensor-scan", daemon=True).start()


def scan(now=None, with_time=False):
    """Every bad (box, sensor) across the 2010 boxes, worst-first (control
    sensors and the longest-standing faults on top). Without `now` a recent
    answer is reused (see above); with_time=True also returns when it was
    worked out (naive UTC)."""
    if now is not None:
        out = _scan(now)
        return (out, now) if with_time else out
    t = time.monotonic()
    with _cache_lock:
        hit = _cache.get("scan")
    if hit and t - hit[0] < STALE_S:
        if t - hit[0] >= FRESH_S and not _refreshing.is_set():
            from flask import current_app
            _refreshing.set()
            _refresh_in_background(current_app._get_current_object())
        out, at = [dict(f) for f in hit[1]], hit[2]
    else:
        at = _dt.datetime.utcnow()
        res = _scan(at)
        _store(res, t, at)
        out = [dict(f) for f in res]
    return (out, at) if with_time else out


def _scan(now):
    from sqlalchemy import text
    from app.extensions import db
    from app.models import LegacyBox
    cutoff = now - _dt.timedelta(days=WINDOW_DAYS)
    # plain tuples, already in box/time order: grouping and turning rows into
    # columns is then one zip per box instead of a list append per value
    # enough sort memory for ~150k rows, or Postgres sorts them on disk (this query only)
    db.session.execute(text("SET LOCAL work_mem = '32MB'"))
    rows = db.session.execute(text(
        f"SELECT box_id, ts, {', '.join(ATTRS)} FROM legacy_readings "
        "WHERE ts >= :cutoff ORDER BY box_id, ts"), {"cutoff": cutoff}).all()
    live_boxes = {b.id: b.name for b in LegacyBox.query.filter(LegacyBox.deleted_at.is_(None)).all()}
    out = []
    for bid, grp in itertools.groupby(rows, key=lambda r: r[0]):
        if bid not in live_boxes:
            continue
        cols = list(zip(*grp))
        d = {"t": list(cols[1]), **{a: list(cols[2 + j]) for j, a in enumerate(ATTRS)}}
        out.extend(_box_findings(bid, live_boxes[bid], d, now))
    out.sort(key=lambda f: (f["priority"], f["since"]))     # control first, then longest-standing
    return out


def for_box(box_id, box_name=None, now=None):
    """Bad sensors for a single box, worst-first — for its detail page (reads
    only that box's recent history, so it is cheap to call per page view)."""
    from app.extensions import db
    from app.models import LegacyBox, LegacyReading
    now = now or _dt.datetime.utcnow()
    if box_name is None:
        b = LegacyBox.query.filter_by(id=box_id).first()
        if b is None or b.deleted_at is not None:
            return []
        box_name = b.name
    cutoff = now - _dt.timedelta(days=WINDOW_DAYS)
    cols = [getattr(LegacyReading, a) for a in ATTRS]
    rows = (db.session.query(LegacyReading.ts, *cols)
            .filter(LegacyReading.box_id == box_id, LegacyReading.ts >= cutoff)
            .order_by(LegacyReading.ts).all())
    if not rows:
        return []
    d = {"t": [r[0] for r in rows], **{a: [r[1 + j] for r in rows] for j, a in enumerate(ATTRS)}}
    out = _box_findings(box_id, box_name, d, now)
    out.sort(key=lambda f: (f["priority"], f["since"]))
    return out


def by_building(findings):
    """Group findings into a per-building tally, worst-first (most bad sensors,
    then most-critical sensor): a dispatch sheet — one visit clears several."""
    g = {}
    for f in findings:
        b = g.setdefault(f["box_id"], {"box_id": f["box_id"], "box": f["box"], "items": []})
        b["items"].append(f)
    buildings = list(g.values())
    for b in buildings:
        b["items"].sort(key=lambda x: (x["priority"], x["since"]))
        b["count"] = len(b["items"])
        b["top_priority"] = min(x["priority"] for x in b["items"])
    buildings.sort(key=lambda b: (-b["count"], b["top_priority"], b["box"].lower()))
    return buildings
