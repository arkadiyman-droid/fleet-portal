"""
app/ctrl_history.py — a short history of each EchoTherm controller, for charts

The portal already reads every controller's /api/status each poll (about a
minute apart, app/fleet_poller.py) but kept only the latest one. This keeps
a compact snapshot every SNAPSHOT_EVERY (5 minutes): the temperatures worth
charting, every apartment sensor, and each boiler's heat / hot-water run
counters, for KEEP_DAYS (90). Nothing new is asked of the controller: its
fleet token still reaches only /api/status and /api/fleet/software.

A controller's run counters are seconds since ITS midnight: usage between
two snapshots is the difference, a drop is the midnight reset, no step may
exceed the time that passed, and nothing is trusted across a long gap (the
same rules as the 2010 boxes' run minutes, app/history.py).

series() feeds the same chart window as the 2010 boxes (static/js/sensorchart.js).
"""
import datetime as _dt
import logging

from sqlalchemy import func

log = logging.getLogger(__name__)

SNAPSHOT_EVERY = _dt.timedelta(minutes=5)
KEEP_DAYS = 90
MAX_GAP = _dt.timedelta(minutes=70)

# (chart key, /api/status field, label)
FIELDS = [("out", "bestOutdoor", "Outside"), ("inside", "avgAptTemp", "Inside average"),
          ("dit", "tit", "Desired inside"), ("aqua1", "aquastat1", "Aquastat 1"), ("aqua2", "aquastat2", "Aquastat 2"),
          ("supp", "supTemp", "Supply"), ("dsupp", "dSupTemp", "Desired supply"), ("hwr", "hwrTemp", "Hot-water return"),
          ("coil1", "coil1Temp", "Coil 1"), ("coil2", "coil2Temp", "Coil 2"), ("mix", "mixTemp", "Hot-water mix"),
          ("stack1", "stackTemp", "Stack 1"), ("stack2", "blr2StackTemp", "Stack 2"),
          ("wireless", "wirelessAvgTemp", "Wireless sensors average")]
COUNTERS = {"heat": ("blr1HtDaySecs", "blr2HtDaySecs"), "hw": ("blr1HwDaySecs", "blr2HwDaySecs")}

_purged_at = None


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    try:
        return round(float(v), 1)
    except (TypeError, ValueError):
        return None


def snapshot(payload) -> dict:
    """The compact row kept from one /api/status reply."""
    vals = {key: _num(payload.get(field)) for key, field, _l in FIELDS}
    for kind, fields in COUNTERS.items():
        for i, f in enumerate(fields, 1):
            vals[f"{kind}{i}"] = _num(payload.get(f))
    apt = [int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else 0 for v in (payload.get("aptTemps") or [])]
    while apt and not apt[-1]:
        apt.pop()
    return {"values": {k: v for k, v in vals.items() if v is not None}, "apt": apt[:128] or None}


def record(ctrl_id, payload, ts) -> bool:
    """Keep a snapshot if the last one is SNAPSHOT_EVERY old (called after
    each good poll; own transaction). Old snapshots are cleared hourly."""
    global _purged_at
    from app.extensions import db
    from app.models import ControllerReading
    last = db.session.query(func.max(ControllerReading.ts)).filter(ControllerReading.controller_id == ctrl_id).scalar()
    if last is not None and ts - last < SNAPSHOT_EVERY - _dt.timedelta(seconds=30):
        return False
    snap = snapshot(payload)
    db.session.add(ControllerReading(controller_id=ctrl_id, ts=ts, values=snap["values"], apt=snap["apt"]))
    if _purged_at is None or ts - _purged_at > _dt.timedelta(hours=1):
        db.session.query(ControllerReading).filter(
            ControllerReading.ts < ts - _dt.timedelta(days=KEEP_DAYS)).delete(synchronize_session=False)
        _purged_at = ts
    db.session.commit()
    return True


def _ok(key, v):
    if v is None:
        return None
    if key == "out":
        return v if -40 <= v <= 130 else None
    if key.startswith("apt"):
        return v if 35 <= v <= 110 else None
    return v if v != 0 and -40 <= v <= 1500 else None


def catalog(ctrl):
    """What this controller can chart, from its latest reading."""
    raw = (ctrl.status.raw_json if ctrl.status else None) or {}
    out = [{"key": k, "label": l, "group": "Main"} for k, f, l in FIELDS if _ok(k, _num(raw.get(f))) is not None]
    for i, v in enumerate(raw.get("aptTemps") or []):
        if _ok("apt", _num(v)) is not None:
            out.append({"key": f"apt{i}", "label": f"Apartment #{i + 1}", "group": "Apartments"})
    return out


def _seconds_used(a, b, field):
    """Run seconds between two snapshots for one counter."""
    x, y = (a.values or {}).get(field), (b.values or {}).get(field)
    if x is None or y is None:
        return 0
    gap = (b.ts - a.ts).total_seconds()
    if gap <= 0 or gap > MAX_GAP.total_seconds():
        return 0
    d = y - x if y >= x else y                       # a drop is the controller's midnight
    return max(0.0, min(d, gap + 60))


def series(ctrl, days, keys, now=None) -> dict:
    """The chart window's data for one controller: lines for the chosen
    keys, burner minutes per hour (heat / hot water) as bars."""
    from app import history
    from app.models import ControllerReading
    now = now or _dt.datetime.utcnow()
    start = now - _dt.timedelta(days=days)
    cat = catalog(ctrl)
    labels = {c["key"]: c["label"] for c in cat}
    keys = [k for k in keys if k in labels][:12]
    rs = ControllerReading.query.filter(ControllerReading.controller_id == ctrl.id,
                                        ControllerReading.ts >= start - MAX_GAP).order_by(ControllerReading.ts).all()
    shown = [r for r in rs if r.ts >= start]
    ms = lambda t: int(t.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)

    def val(r, k):
        if k.startswith("apt"):
            i = int(k[3:])
            return _ok(k, (r.apt or [])[i] if i < len(r.apt or []) else None)
        return _ok(k, (r.values or {}).get(k))
    hours = {}
    for a, b in zip(rs, rs[1:]):
        if b.ts < start:
            continue
        acc = hours.setdefault(history.floor_hour(b.ts), [0.0, 0.0])
        acc[0] += sum(_seconds_used(a, b, f"heat{i}") for i in (1, 2)) / 60
        acc[1] += sum(_seconds_used(a, b, f"hw{i}") for i in (1, 2)) / 60
    hk = sorted(hours)
    first = rs[0].ts if rs else None
    return {"range": [ms(start), ms(now)], "t": [ms(r.ts) for r in shown], "res": "a reading every 5 minutes",
            "series": {k: [val(r, k) for r in shown] for k in keys}, "labels": {k: labels[k] for k in keys},
            "catalog": cat, "days": days,
            "note": (None if first and first <= start else
                     "The portal keeps a controller's history from 2026-10-15 on: earlier days are empty."),
            "bars": {"t": [ms(h) for h in hk], "heat": [round(min(hours[h][0], 240)) for h in hk],
                     "hw": [round(min(hours[h][1], 240)) for h in hk], "step": "hour"}}
