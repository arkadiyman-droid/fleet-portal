"""
app/heat_overnight.py — did each building actually run for heat overnight?

The Live wall is a snapshot, and by mid-morning the outdoor temperature has
warmed up: a building that failed to heat all night can look fine now. This
reads back over the overnight window and decides, per building, whether the
boiler fired — so the ones that needed heat and never ran stand out instead of
being chased one supply-chart at a time.

What counts as a run (the user, 2026-11-08): the loop ran only if BOTH supply
and return climbed above the cold-loop ceiling (80°). The temperatures are the
evidence — a boiler run counter ticking while the loop stayed cold is not a run,
it is a problem. Boxes with no supply/return sensors at all (steam) fall back to
the run counter, since the temperature rule can't apply to them.

Demand comes from the coldest outdoor reading overnight: at or below
DEMAND_OUT_F the building needed heat, so a loop that never got hot is the catch
("no-heat"). Mild out and a quiet boiler is correct ("idle"), not a problem.

The window is local (America/New_York via legacy.LOCAL), 9 pm to 9 am: the
coldest stretch and the one the morning review cares about. Before 9 am it runs
to now (last night, still going); through the day it is the night that just
ended; from 9 pm it is tonight so far.
"""
import datetime as _dt
import logging

log = logging.getLogger(__name__)

NIGHT_START_H = 21          # 9 pm
NIGHT_END_H = 9             # 9 am

# Tunables. A real heat run shows BOTH supply and return climb above the
# cold-loop ceiling (the user, 2026-11-08: "status counts as run only if supply
# and return went above 80°"), which is the user's own hydronic rule
# (app/legacy.LOOP_COLD_F = 80 F, 2026-10-19). COLD_SUPPLY_F mirrors it for the
# sources without oBIX status.
COLD_SUPPLY_F = 80         # every reporting loop pipe must peak ABOVE this to count as a run
DEMAND_OUT_F = 45          # Bloom / EchoTherm: coldest outdoor at or under this = heat was needed
MIN_CALL_READS = 2         # 2010: this many calling-for-heat reads = a sustained call, not a blip

_UTC = _dt.timezone.utc


def window(now=None):
    """(start_utc, end_utc, label) for the overnight window, naive UTC to match
    the stored timestamps."""
    from app import legacy
    now = now or _dt.datetime.utcnow()
    lt = now.replace(tzinfo=_UTC).astimezone(legacy.LOCAL)
    d = lt.date()

    def at(day, h):
        return _dt.datetime.combine(day, _dt.time(h), tzinfo=legacy.LOCAL)

    if lt.hour < NIGHT_END_H:                       # early morning: last night, still going
        start, end, label = at(d - _dt.timedelta(days=1), NIGHT_START_H), lt, "last night"
    elif lt.hour < NIGHT_START_H:                   # daytime: the night that ended this morning
        start, end, label = at(d - _dt.timedelta(days=1), NIGHT_START_H), at(d, NIGHT_END_H), "last night"
    else:                                           # evening: tonight so far
        start, end, label = at(d, NIGHT_START_H), lt, "tonight so far"
    to_utc = lambda t: t.astimezone(_UTC).replace(tzinfo=None)
    return to_utc(start), to_utc(end), label


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _hm(mins):
    mins = int(round(mins))
    h, m = divmod(mins, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


_TONE = {"ran": "ok", "no-heat": "crit", "idle": "idle", "unknown": "unknown"}
_LABEL = {"ran": "Ran", "no-heat": "No heat", "idle": "Idle", "unknown": "No data"}


def _mk(state, detail, **nums):
    return {"state": state, "tone": _TONE[state], "label": _LABEL[state], "detail": detail, **nums}


def _unknown():
    return _mk("unknown", "No readings recorded overnight.",
               needed=None, minutes=None, peak=None, retn_peak=None, rise=None, min_out=None)


def _peak_rise(vals):
    """(highest, how much it climbed) for one pipe's readings over the window."""
    if not vals:
        return (None, None)
    peak = max(vals)
    return (peak, peak - min(vals))


def _loop_words(peak_s, peak_r):
    """'supply 70° / return 68°' from whichever pipes have a reading."""
    parts = []
    if peak_s is not None:
        parts.append(f"supply {round(peak_s)}°")
    if peak_r is not None:
        parts.append(f"return {round(peak_r)}°")
    return " / ".join(parts) if parts else "no supply or return reading"


def _warm_loop(peak_s, peak_r, thresh):
    """The run test: every loop pipe that reported must have PEAKED above thresh
    (both, when both report — the user's supply-and-return rule). None when no
    loop pipe reported at all, so the caller can fall back to the run counter."""
    peaks = [p for p in (peak_s, peak_r) if p is not None]
    if not peaks:
        return None
    return all(p > thresh for p in peaks)


def _ran_text(minutes, peak_s, peak_r, out_txt):
    loop = _loop_words(peak_s, peak_r)
    has_loop = peak_s is not None or peak_r is not None
    if minutes:
        return f"Ran {_hm(minutes)} overnight — {loop}{out_txt}" if has_loop \
            else f"Ran {_hm(minutes)} overnight{out_txt}"
    if has_loop:
        return f"Loop hot — {loop}{out_txt}"
    return f"Boiler produced heat overnight{out_txt}"


def _cold_pipes(peak_s, peak_r, thresh):
    """The reporting pipes that never cleared thresh — 'supply', 'return', or both."""
    return [n for n, p in (("supply", peak_s), ("return", peak_r)) if p is not None and p <= thresh]


def _warmest(peak_s, peak_r):
    peaks = [p for p in (peak_s, peak_r) if p is not None]
    return max(peaks) if peaks else None


def _mismatch_detail(peak_s, peak_r, thresh, out_txt):
    """One pipe cleared the ceiling, the other didn't: the loop got hot somewhere,
    so it isn't a dead-cold miss — most often one loop sensor reading wrong."""
    cold = _cold_pipes(peak_s, peak_r, thresh)
    return (f"One pipe got hot but {' and '.join(cold)} stayed under {thresh}° — likely a loop "
            f"sensor reading low: peak {_loop_words(peak_s, peak_r)}{out_txt}")


# ── per-source verdicts (pure; take the window's rows + its start) ──────────────

def verdict_legacy(rows, start):
    """A 2010 box. It RAN only if supply and return both climbed above
    legacy.LOOP_COLD_F (80°) — the user's rule (2026-11-08); the boiler run
    counter is trusted only on steam boxes with no supply/return sensors. It is a
    failure when the box was CALLING for heat yet the loop never got both pipes
    above 80°."""
    from app import history, legacy
    rows = sorted(rows, key=lambda r: r.ts)
    cur = [r for r in rows if r.ts >= start]
    if not cur:
        return _unknown()
    from app import despike
    rng = lambda v, lo, hi: v if (v is not None and lo <= v <= hi) else None
    supps = despike.drop([rng(r.supp, 1, 260) for r in cur])       # drop single-sample glitches
    retns = despike.drop([rng(r.retn, 1, 260) for r in cur])
    outs = despike.drop([rng(r.out, -40, 130) for r in cur])
    peak, rise = _peak_rise(supps)
    retn_peak, retn_rise = _peak_rise(retns)
    min_out = min(outs) if outs else None
    minutes = sum(history.used(rows, f"heat{i}") for i in range(1, 5))   # pre-window read seeds the first diff
    calls = [r for r in cur if history._tone(r.code) == "heat"]
    out_txt = f" · {round(min_out)}° out" if min_out is not None else ""
    nums = dict(needed=len(calls) >= MIN_CALL_READS, minutes=minutes or None,
                peak=peak, retn_peak=retn_peak, rise=rise, min_out=min_out)
    warm = _warm_loop(peak, retn_peak, legacy.LOOP_COLD_F)        # True / False / None (no loop sensors)
    ran = warm if warm is not None else (minutes > 0)            # steam / sensorless: trust the run counter
    if ran:
        return _mk("ran", _ran_text(minutes, peak, retn_peak, out_txt), **nums)
    called = len(calls) >= MIN_CALL_READS
    warmest = _warmest(peak, retn_peak)
    thr = legacy.LOOP_COLD_F
    # A real miss is a loop that stayed cold EVERYWHERE while the box called for
    # heat. One pipe hot and the other under 80 is not a dead-cold miss (the loop
    # got hot somewhere) — almost always a loop sensor reading low, so don't cry
    # "no heat" at it; flag it to look at instead.
    if called and warmest is not None and warmest <= thr:
        cold = _cold_pipes(peak, retn_peak, thr)
        return _mk("no-heat", f"Called for heat but {' and '.join(cold)} never rose above "
                              f"{thr}° overnight — peak {_loop_words(peak, retn_peak)}{out_txt}", **nums)
    if called and warmest is None and not minutes:              # steam box called, logged no run time
        return _mk("no-heat", f"Called for heat but the boiler logged no run time overnight{out_txt}", **nums)
    if warm is False and warmest is not None and warmest > thr:
        return _mk("idle", _mismatch_detail(peak, retn_peak, thr, out_txt), **nums)
    return _mk("idle", f"No sustained heat call overnight{out_txt}", **nums)


def _verdict_counter(minutes, peak_s, rise_s, peak_r, rise_r, min_out, run_extra, cold_label):
    """Shared tail for EchoTherm / Bloom, which have no oBIX call state: demand
    is the coldest outdoor (DEMAND_OUT_F). The loop RAN only if supply and return
    both peaked above COLD_SUPPLY_F (80°) — the user's rule (2026-11-08); a run
    counter or boiler % is trusted only when there are no supply/return readings
    at all. A failure is a cold night where the loop never got both pipes hot."""
    needed = min_out is not None and min_out <= DEMAND_OUT_F
    out_txt = f" · {round(min_out)}° out" if min_out is not None else ""
    warm = _warm_loop(peak_s, peak_r, COLD_SUPPLY_F)            # True / False / None (no loop sensors)
    counter_ran = (minutes is not None and minutes > 0) or run_extra
    ran = warm if warm is not None else counter_ran            # sensorless: trust the run counter / boiler %
    nums = dict(needed=needed, minutes=(round(minutes) or None) if minutes is not None else None,
                peak=peak_s, retn_peak=peak_r, rise=rise_s, min_out=min_out)
    warmest = _warmest(peak_s, peak_r)
    if ran:
        return _mk("ran", _ran_text(minutes, peak_s, peak_r, out_txt), **nums)
    # A real miss is a cold night where the loop stayed cold everywhere. One pipe
    # hot and the other under 80 isn't a dead-cold miss — most often a bad sensor.
    if needed and warmest is not None and warmest <= COLD_SUPPLY_F:
        cold = _cold_pipes(peak_s, peak_r, COLD_SUPPLY_F)
        return _mk("no-heat", f"Cold out and {' and '.join(cold)} never rose above {COLD_SUPPLY_F}° "
                              f"overnight — peak {_loop_words(peak_s, peak_r)}{out_txt}", **nums)
    if needed and warmest is None and not counter_ran:
        return _mk("no-heat", cold_label, **nums)
    if warm is False and warmest is not None and warmest > COLD_SUPPLY_F:
        return _mk("idle", _mismatch_detail(peak_s, peak_r, COLD_SUPPLY_F, out_txt), **nums)
    return _mk("idle", f"No heat run overnight{out_txt}", **nums)


def verdict_echo(rows, start):
    from app import ctrl_history
    rows = sorted(rows, key=lambda r: r.ts)
    cur = [r for r in rows if r.ts >= start]
    if not cur:
        return _unknown()
    secs = 0.0
    for a, b in zip(rows, rows[1:]):
        if b.ts < start:
            continue
        secs += sum(ctrl_history._seconds_used(a, b, f"heat{i}") for i in (1, 2))
    minutes = secs / 60.0
    from app import despike
    rng = lambda v, lo, hi: v if (v is not None and lo <= v <= hi) else None
    supps = despike.drop([rng(_num((r.values or {}).get("supp")), 1, 260) for r in cur])
    retns = despike.drop([rng(_num((r.values or {}).get("hwr")), 1, 260) for r in cur])
    outs = despike.drop([rng(_num((r.values or {}).get("out")), -40, 130) for r in cur])
    peak, rise = _peak_rise(supps)
    retn_peak, retn_rise = _peak_rise(retns)
    min_out = min(outs) if outs else None
    return _verdict_counter(minutes, peak, rise, retn_peak, retn_rise, min_out, False,
                            f"Cold out ({round(min_out)}°) and the boiler never fired" if min_out is not None
                            else "The boiler never fired")


def verdict_bloom(rows, start):
    rows = sorted(rows, key=lambda r: r.ts)
    cur = [r for r in rows if r.ts >= start]
    if not cur:
        return _unknown()
    from app import despike
    supps = despike.drop([_num((r.data or {}).get("supp")) for r in cur])
    retns = despike.drop([_num((r.data or {}).get("retn")) for r in cur])
    outs = despike.drop([_num((r.data or {}).get("out")) for r in cur])
    boilers = [v for v in (_num((r.data or {}).get("boiler")) for r in cur) if v is not None]
    peak, rise = _peak_rise(supps)
    retn_peak, retn_rise = _peak_rise(retns)
    min_out = min(outs) if outs else None
    run_extra = bool(boilers and max(boilers) > 0)
    return _verdict_counter(None, peak, rise, retn_peak, retn_rise, min_out, run_extra,
                            f"Cold out ({round(min_out)}°) and no boiler run" if min_out is not None
                            else "No boiler run")


# ── bulk loaders for the Live wall (one query per source) ──────────────────────

def _safe(fn, rows, start):
    """A verdict bug must never 500 the boss-facing Live wall: fall back to
    'no data' and keep the page up."""
    try:
        return fn(rows, start)
    except Exception:
        log.exception("heat_overnight verdict failed")
        return _unknown()

def for_legacy(box_ids, now=None):
    from app import history
    from app.models import LegacyReading
    box_ids = list(box_ids)
    if not box_ids:
        return {}
    start, end, _ = window(now)
    rows = (LegacyReading.query
            .filter(LegacyReading.box_id.in_(box_ids),
                    LegacyReading.ts >= start - history.LOOKBACK, LegacyReading.ts <= end).all())
    by = {}
    for r in rows:
        by.setdefault(r.box_id, []).append(r)
    return {bid: _safe(verdict_legacy, by.get(bid, []), start) for bid in box_ids}


def for_echo(ctrl_ids, now=None):
    from app import ctrl_history
    from app.models import ControllerReading
    ctrl_ids = list(ctrl_ids)
    if not ctrl_ids:
        return {}
    start, end, _ = window(now)
    rows = (ControllerReading.query
            .filter(ControllerReading.controller_id.in_(ctrl_ids),
                    ControllerReading.ts >= start - ctrl_history.MAX_GAP, ControllerReading.ts <= end).all())
    by = {}
    for r in rows:
        by.setdefault(r.controller_id, []).append(r)
    return {cid: _safe(verdict_echo, by.get(cid, []), start) for cid in ctrl_ids}


def for_bloom(asset_ids, now=None):
    from app.models import BloomSample
    asset_ids = list(asset_ids)
    if not asset_ids:
        return {}
    start, end, _ = window(now)
    rows = (BloomSample.query
            .filter(BloomSample.asset_id.in_(asset_ids),
                    BloomSample.ts >= start, BloomSample.ts <= end).all())
    by = {}
    for r in rows:
        by.setdefault(r.asset_id, []).append(r)
    return {aid: _safe(verdict_bloom, by.get(aid, []), start) for aid in asset_ids}
