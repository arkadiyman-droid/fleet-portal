"""
app/heat_overnight.py — did each building actually run for heat overnight?

The Live wall is a snapshot, and by mid-morning the outdoor temperature has
warmed up: a building that failed to heat all night can look fine now. This
reads back over the overnight window and decides, per building, whether the
boiler fired — so the ones that needed heat and never ran stand out instead of
being chased one supply-chart at a time.

Signal, by source:
  * 2010 boxes  — boiler heat-run MINUTES over the window (LegacyReading
                  heat1..heat4, summed by app/history.used, authoritative).
  * EchoTherm   — boiler heat-run SECONDS (ControllerReading values heat1/heat2,
                  summed by app/ctrl_history._seconds_used, authoritative).
  * Bloom       — no run counter, so inferred from the supply curve: supply that
                  reached firing temperature, climbed clearly, or any boiler %.

Demand comes from the coldest outdoor reading overnight: at or below
DEMAND_OUT_F the building needed heat, so a boiler that never fired is the catch
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

# Tunables. The cold-loop ceiling is the user's own rule (app/legacy.LOOP_COLD_F
# = 80 F, 2026-10-19: "the only problematic hydronic systems are the ones with
# supply and/or return below 80"); COLD_SUPPLY_F mirrors it for the sources
# without oBIX status.
HOT_SUPPLY_F = 110         # supply at or above this = the loop is hot, the boiler produced heat
RISE_F = 15                # supply climbing this much over the window = it fired
COLD_SUPPLY_F = 80         # a failure needs the loop to have stayed under this (mirror of legacy.LOOP_COLD_F)
DEMAND_OUT_F = 45          # Bloom / EchoTherm fallback: coldest outdoor at or under this = heat was needed
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


def _ran_detail(minutes, peak, rise, out_txt):
    if minutes:
        return f"Ran {_hm(minutes)} overnight; supply to {round(peak)}°{out_txt}" if peak is not None \
            else f"Ran {_hm(minutes)} overnight{out_txt}"
    if rise is not None and rise >= RISE_F and peak is not None:
        return f"Supply climbed to {round(peak)}° (up {round(rise)}°){out_txt}"
    if peak is not None:
        return f"Loop hot — supply to {round(peak)}°{out_txt}"
    return f"Boiler produced heat overnight{out_txt}"


# ── per-source verdicts (pure; take the window's rows + its start) ──────────────

def verdict_legacy(rows, start):
    """A 2010 box: it ran if the loop got hot, climbed, or the boiler logged run
    minutes. It is a failure only if the box was CALLING for heat yet the loop
    stayed under legacy.LOOP_COLD_F (80°) and never fired — the user's own
    cold-loop rule, aggregated over the window instead of one read."""
    from app import history, legacy
    rows = sorted(rows, key=lambda r: r.ts)
    cur = [r for r in rows if r.ts >= start]
    if not cur:
        return _unknown()
    supps = [r.supp for r in cur if r.supp is not None and 1 <= r.supp <= 260]
    retns = [r.retn for r in cur if r.retn is not None and 1 <= r.retn <= 260]
    outs = [r.out for r in cur if r.out is not None and -40 <= r.out <= 130]
    peak, rise = _peak_rise(supps)
    retn_peak, retn_rise = _peak_rise(retns)
    min_out = min(outs) if outs else None
    minutes = sum(history.used(rows, f"heat{i}") for i in range(1, 5))   # pre-window read seeds the first diff
    calls = [r for r in cur if history._tone(r.code) == "heat"]
    # loop_cold() already weighs supply AND return (either under 80° is cold).
    cold_calls = [r for r in calls if legacy.loop_cold(r.supp, r.retn)]
    warm_calls = [r for r in calls if not legacy.loop_cold(r.supp, r.retn)
                  and ((r.supp and r.supp >= legacy.LOOP_COLD_F) or (r.retn and r.retn >= legacy.LOOP_COLD_F))]
    out_txt = f" · {round(min_out)}° out" if min_out is not None else ""
    nums = dict(needed=len(calls) >= MIN_CALL_READS, minutes=minutes or None,
                peak=peak, retn_peak=retn_peak, rise=rise, min_out=min_out)
    ran = (minutes > 0 or (rise is not None and rise >= RISE_F) or (retn_rise is not None and retn_rise >= RISE_F)
           or (peak is not None and peak >= HOT_SUPPLY_F) or (retn_peak is not None and retn_peak >= HOT_SUPPLY_F)
           or bool(warm_calls))
    if ran:
        return _mk("ran", _ran_detail(minutes, peak, rise, out_txt), **nums)
    if len(calls) >= MIN_CALL_READS and len(cold_calls) >= MIN_CALL_READS and not warm_calls:
        cold_s = min((r.supp for r in cold_calls if r.supp), default=None)
        cold_r = min((r.retn for r in cold_calls if r.retn), default=None)
        return _mk("no-heat", f"Called for heat but the loop stayed under {legacy.LOOP_COLD_F}° and never "
                              f"fired — coldest {_loop_words(cold_s, cold_r)}{out_txt}", **nums)
    return _mk("idle", f"No sustained heat call overnight{out_txt}", **nums)


def _verdict_counter(minutes, peak_s, rise_s, peak_r, rise_r, min_out, run_extra, cold_label):
    """Shared tail for EchoTherm / Bloom, which have no oBIX call state: demand
    is the coldest outdoor (DEMAND_OUT_F). The loop ran if EITHER pipe got hot
    or climbed; it is a failure only if BOTH supply and return stayed cold."""
    needed = min_out is not None and min_out <= DEMAND_OUT_F
    out_txt = f" · {round(min_out)}° out" if min_out is not None else ""
    peaks = [p for p in (peak_s, peak_r) if p is not None]
    warmest = max(peaks) if peaks else None
    rose = (rise_s is not None and rise_s >= RISE_F) or (rise_r is not None and rise_r >= RISE_F)
    ran = (minutes is not None and minutes > 0) or rose or (warmest is not None and warmest >= HOT_SUPPLY_F) or run_extra
    nums = dict(needed=needed, minutes=(round(minutes) or None) if minutes is not None else None,
                peak=peak_s, retn_peak=peak_r, rise=rise_s, min_out=min_out)
    if ran:
        best_rise = max([r for r in (rise_s, rise_r) if r is not None], default=None)
        return _mk("ran", _ran_detail(minutes, warmest, best_rise, out_txt), **nums)
    if needed and warmest is not None and warmest < COLD_SUPPLY_F:
        return _mk("no-heat", f"{cold_label} — {_loop_words(peak_s, peak_r)} stayed under {COLD_SUPPLY_F}°", **nums)
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
    supps = [v for v in (_num((r.values or {}).get("supp")) for r in cur) if v is not None and 1 <= v <= 260]
    retns = [v for v in (_num((r.values or {}).get("hwr")) for r in cur) if v is not None and 1 <= v <= 260]
    outs = [v for v in (_num((r.values or {}).get("out")) for r in cur) if v is not None and -40 <= v <= 130]
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
    supps = [v for v in (_num((r.data or {}).get("supp")) for r in cur) if v is not None]
    retns = [v for v in (_num((r.data or {}).get("retn")) for r in cur) if v is not None]
    outs = [v for v in (_num((r.data or {}).get("out")) for r in cur) if v is not None]
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
