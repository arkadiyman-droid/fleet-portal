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
        if _present(v) and v == v0 and (t - e) <= MAX_GAP:
            e = t
            n += 1
        else:
            if v0 is not None and n >= 2:
                yield s, e, v0, n
            if _present(v):
                v0, s, e, n = v, t, t, 1
            else:
                v0, n = None, 0
    if v0 is not None and n >= 2:
        yield s, e, v0, n


def fault(times, vals, ch, now):
    """The worst fault for one channel of one box, or None. `times`/`vals` are
    the window's reads in order (vals may hold None / 0 for missing / no sensor)."""
    attr, label, ceiling, floor, prio, faults, wireless = ch
    live = [(t, v) for t, v in zip(times, vals) if _present(v)]
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
        last_i = max((i for i, v in enumerate(vals) if _present(v)), default=None)
        if last_i is not None and last_i < len(vals) - 1 and all(not _present(v) for v in vals[last_i + 1:]):
            dead = now - times[last_i]
            if dead >= _dt.timedelta(hours=FLAT_HOURS):
                return _mk("dropped", label, prio + 1, times[last_i], now,
                           f"was reading, then nothing for {_hrs(dead)} (last {vals[last_i]}°)"
                           + why("the wireless Inovonics probe stopped reporting", "the sensor stopped reporting"))

    if "frozen" in faults:
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


def scan(now=None):
    """Every bad (box, sensor) across the 2010 boxes, worst-first (control
    sensors and the longest-standing faults on top)."""
    from app.extensions import db
    from app.models import LegacyBox, LegacyReading
    now = now or _dt.datetime.utcnow()
    cutoff = now - _dt.timedelta(days=WINDOW_DAYS)
    cols = [getattr(LegacyReading, a) for a in ATTRS]
    rows = (db.session.query(LegacyReading.box_id, LegacyReading.ts, *cols)
            .filter(LegacyReading.ts >= cutoff)
            .order_by(LegacyReading.box_id, LegacyReading.ts).all())
    live_boxes = {b.id: b.name for b in LegacyBox.query.filter(LegacyBox.deleted_at.is_(None)).all()}
    by_box = {}
    for r in rows:
        bid = r[0]
        if bid not in live_boxes:
            continue
        d = by_box.setdefault(bid, {"t": [], **{a: [] for a in ATTRS}})
        d["t"].append(r[1])
        for j, a in enumerate(ATTRS):
            d[a].append(r[2 + j])
    out = []
    for bid, d in by_box.items():
        for ch in CHANNELS:
            f = fault(d["t"], d[ch[0]], ch, now)
            if f:
                out.append({**f, "box_id": bid, "box": live_boxes[bid], "attr": ch[0], "wireless": ch[6]})
    out.sort(key=lambda f: (f["priority"], f["since"]))     # control first, then longest-standing
    return out
