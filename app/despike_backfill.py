"""
app/despike_backfill.py — de-spike the stored readings, not just the view

Rewrites each single-sample sensor glitch in the raw reading tables to the
previous good value (carry-forward, app/despike.fill) so the stored data has no
gap. Covers the three sources — the 2010 boxes (LegacyReading, typed columns),
EchoTherm controllers (ControllerReading.values JSONB) and Bloom
(BloomSample.data JSONB) — and rebuilds the 2010 hourly/daily rollups for any
hour whose readings changed.

Run it over everything with `flask despike-history --apply`, or over a recent
window for the incremental pass the legacy poller makes each hour
(app/history.maintain -> correct_recent).

Stack temperatures, set-points and run counters are left alone: a stack can
spike for real on a short burner cycle. Apartment / system arrays are cleaned
on the chart at read time (app/despike.fill in the series builders) but not
rewritten here — only the scalar sensor channels are.
"""
import datetime as _dt
import logging

from app import despike

log = logging.getLogger(__name__)

# slow water-loop / space temperatures only
LEGACY_COLS = ("supp", "retn", "mix", "aqua", "coil", "out", "inside", "risr")
CTRL_KEYS = ("supp", "hwr", "out", "inside", "aqua1", "aqua2", "coil1", "coil2", "mix", "wireless")
BLOOM_SKIP = {"boiler", "target", "dit"}        # a % and two set-points, not sensor temps


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _fix_scalar(rows, cols, apply):
    """De-spike one entity's time-ordered rows across typed columns. Returns the
    set of row indices changed; writes in place when apply."""
    changed = set()
    for col in cols:
        series = [getattr(r, col) for r in rows]
        filled = despike.fill(series)
        for i, (old, new) in enumerate(zip(series, filled)):
            if new != old:
                changed.add(i)
                if apply:
                    setattr(rows[i], col, new)
    return changed


def _fix_jsonb(rows, attr, want, apply):
    """De-spike one entity's time-ordered rows across the numeric keys of a JSONB
    column (`want(key)->bool` selects them). Returns the row indices changed;
    reassigns the dict in place when apply so SQLAlchemy sees it dirty."""
    keys = set()
    for r in rows:
        for k, v in (getattr(r, attr) or {}).items():
            if want(k) and _num(v) is not None:
                keys.add(k)
    patch = {}                                   # row index -> {key: new}
    for k in keys:
        series = [_num((getattr(r, attr) or {}).get(k)) for r in rows]
        filled = despike.fill(series)
        for i, (old, new) in enumerate(zip(series, filled)):
            if new != old and new is not None:
                patch.setdefault(i, {})[k] = new
    if apply:
        for i, kv in patch.items():
            setattr(rows[i], attr, {**(getattr(rows[i], attr) or {}), **kv})
    return set(patch)


def run(apply=False, since=None, rebuild_rollups=True, now=None) -> dict:
    """De-spike the stored readings. `since` limits to ts >= since (None = all
    history). Processes one entity at a time and commits per entity so memory
    and the transaction stay bounded. Returns a count report."""
    from app import history
    from app.extensions import db
    from app.models import BloomAsset, BloomSample, Controller, ControllerReading, LegacyBox, LegacyReading
    now = now or _dt.datetime.utcnow()
    rep = {"apply": apply, "legacy_rows": 0, "controller_rows": 0, "bloom_rows": 0, "hours_rebuilt": 0}
    hours = set()

    # entity ids come from the small entity tables, not a DISTINCT over the
    # millions of partitioned reading rows.
    legacy_ids = [r[0] for r in db.session.query(LegacyBox.id).all()]
    ctrl_ids = [r[0] for r in db.session.query(Controller.id).all()]
    bloom_ids = [r[0] for r in db.session.query(BloomAsset.id).all()]

    # ---- 2010 boxes (typed columns) ----
    for box_id in legacy_ids:
        q = LegacyReading.query.filter(LegacyReading.box_id == box_id)
        if since is not None:
            q = q.filter(LegacyReading.ts >= since)
        rows = q.order_by(LegacyReading.ts).all()
        changed = _fix_scalar(rows, LEGACY_COLS, apply)
        rep["legacy_rows"] += len(changed)
        hours.update(history.floor_hour(rows[i].ts) for i in changed)
        db.session.commit() if apply else db.session.expunge_all()

    # ---- EchoTherm controllers (values JSONB) ----
    for cid in ctrl_ids:
        q = ControllerReading.query.filter(ControllerReading.controller_id == cid)
        if since is not None:
            q = q.filter(ControllerReading.ts >= since)
        rows = q.order_by(ControllerReading.ts).all()
        rep["controller_rows"] += len(_fix_jsonb(rows, "values", lambda k: k in CTRL_KEYS, apply))
        db.session.commit() if apply else db.session.expunge_all()

    # ---- Bloom (data JSONB) ----
    for aid in bloom_ids:
        q = BloomSample.query.filter(BloomSample.asset_id == aid)
        if since is not None:
            q = q.filter(BloomSample.ts >= since)
        rows = q.order_by(BloomSample.ts).all()
        rep["bloom_rows"] += len(_fix_jsonb(rows, "data", lambda k: k not in BLOOM_SKIP, apply))
        db.session.commit() if apply else db.session.expunge_all()

    rep["affected_hours"] = len(hours)
    if apply and rebuild_rollups and hours:                 # past rollups kept the old spiked maxes
        for h in sorted(hours):
            history.rollup_hour(h)
        for d in sorted({history.local(h).date() for h in hours}):
            history.rollup_day(d)
        db.session.commit()
        rep["hours_rebuilt"] = len(hours)
    return rep


_last_recent = None


def correct_recent(app, now=None) -> dict:
    """The incremental pass: de-spike the last few hours of readings so new
    glitches are written back (with a one-sample delay). Throttled to about once
    an hour; the normal rollup — which also de-spikes — handles those hours, so
    this does not rebuild rollups."""
    global _last_recent
    now = now or _dt.datetime.utcnow()
    if _last_recent is not None and now - _last_recent < _dt.timedelta(minutes=55):
        return {}
    _last_recent = now
    rep = run(apply=True, since=now - _dt.timedelta(hours=3), rebuild_rollups=False, now=now)
    if rep.get("legacy_rows") or rep.get("controller_rows") or rep.get("bloom_rows"):
        log.info("despike: corrected %d legacy / %d controller / %d bloom reading(s) in the last 3 h",
                 rep["legacy_rows"], rep["controller_rows"], rep["bloom_rows"])
    return rep
