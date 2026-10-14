"""
app/alerts.py — tell someone when a controller turns red, and when it recovers

Runs after every poll cycle (app/fleet_poller.py, inside its advisory lock,
so one process decides per cycle). "Red" is exactly what the Fleet Overview
shows as crit (app/fleet_health.py): offline, an active heating alarm, power
failing, overheating, disk nearly full, a backup failing. Warn-level issues
never page anyone.

Per controller:
  * red for ALERT_AFTER_SECONDS (default 5 min, about 5 polls) -> ALERT
    (one Wi-Fi blip or a reboot never pages anyone)
  * still red every ALERT_REPEAT_HOURS (default 24; 0 = never) -> REMINDER
  * no longer red after an alert -> RECOVERED, with how long it lasted
  * muted (a technician on site): nothing is sent until the mute ends;
    an alert that recovers during a mute closes quietly
Every decision is kept as an AlertEvent (the Alerts page's history) whether
or not any channel is configured, so nothing is lost before notifications
are set up.

decide() is pure (no database, no clock) and is what the tests drive.
"""
import datetime as _dt
import logging

from app import fleet_health, notify

log = logging.getLogger(__name__)


def _secs(cfg, name, default):
    try:
        return float(cfg.get(name, default))
    except (TypeError, ValueError):
        return float(default)


def decide(st: dict, level: str, crit: list, now: _dt.datetime, cfg: dict):
    """One controller, one poll. st: {alerting, first_bad_at, opened_at,
    notified_at, muted_until}. Returns (new_st, event) where event is None
    or {"kind": open|reminder|recovered, "send": bool, "text": str}."""
    st = dict(st)
    after = _secs(cfg, "ALERT_AFTER_SECONDS", 300)
    repeat_h = _secs(cfg, "ALERT_REPEAT_HOURS", 24)
    muted = bool(st.get("muted_until") and st["muted_until"] > now)
    red = level == "crit" and bool(crit)

    if red:
        st["first_bad_at"] = st.get("first_bad_at") or now
        if not st.get("alerting"):
            if (now - st["first_bad_at"]).total_seconds() >= after and not muted:
                st.update(alerting=True, opened_at=now, notified_at=now)
                return st, {"kind": "open", "send": True, "text": "\n".join(crit)}
            return st, None
        if (repeat_h > 0 and not muted and st.get("notified_at")
                and (now - st["notified_at"]).total_seconds() >= repeat_h * 3600):
            st["notified_at"] = now
            return st, {"kind": "reminder", "send": True, "text": "\n".join(crit)}
        return st, None

    st["first_bad_at"] = None
    if st.get("alerting"):
        lasted = fleet_health.duration((now - (st.get("opened_at") or now)).total_seconds())
        st.update(alerting=False, opened_at=None, notified_at=None)
        return st, {"kind": "recovered", "send": not muted, "text": f"Back to normal after {lasted}."}
    return st, None


def _where(ctrl):
    b = ctrl.building
    p = b.property if b else None
    c = p.client if p else None
    return " · ".join(x for x in (c.name if c else None, p.name if p else None, b.name if b else None) if x)


_TITLES = {"open": "ALERT", "reminder": "Still in alarm", "recovered": "Recovered"}


def message(kind: str, ctrl, text: str):
    where = _where(ctrl)
    title = f"{_TITLES[kind]}: {ctrl.name}" + (f" ({ctrl.building.name})" if ctrl.building else "")
    body = text + (f"\n{where}" if where else "")
    return title, body


def check_all(app, now=None) -> int:
    """Evaluate every active controller once; send what decide() says.
    Returns how many notifications were attempted."""
    from app.extensions import db
    from app.models import AlertEvent, AlertState, Controller
    if not app.config.get("ALERTS_ENABLED", True):
        return 0
    now = now or _dt.datetime.utcnow()
    cfg = app.config
    sent = 0
    seen = set()
    for ctrl in Controller.query.filter_by(is_active=True).all():
        seen.add(ctrl.id)
        ev = fleet_health.evaluate(ctrl, ctrl.status, now_dt=now)
        crit = [t for lvl, t in ev["issues"] if lvl == "crit"]
        row = db.session.get(AlertState, ctrl.id) or AlertState(controller_id=ctrl.id)
        st = {k: getattr(row, k) for k in ("alerting", "first_bad_at", "opened_at", "notified_at", "muted_until")}
        st["alerting"] = bool(st["alerting"])
        new, event = decide(st, ev["level"], crit, now, cfg)
        for k, v in new.items():
            setattr(row, k, v)
        if ev["level"] == "crit" and crit:
            row.issue = crit[0][:300]
        elif not new["alerting"]:
            row.issue = None
        db.session.add(row)
        if event:
            title, body = message(event["kind"], ctrl, event["text"])
            delivered = "muted: not sent"
            if event["send"]:
                delivered = notify.send(title, body, urgent=event["kind"] != "recovered",
                                        good_news=event["kind"] == "recovered", path=f"/fleet/{ctrl.id}")
                sent += 1
            db.session.add(AlertEvent(controller_id=ctrl.id, controller_name=ctrl.name[:200], kind=event["kind"],
                                      title=title[:200], text=body[:1000], delivered=delivered[:300], created_at=now))
            log.info("Alert %s for %s: %s", event["kind"], ctrl.name, delivered)
    # a controller archived while in alarm: close it quietly
    for row in AlertState.query.filter(AlertState.alerting.is_(True)).all():
        if row.controller_id not in seen:
            row.alerting, row.opened_at, row.notified_at = False, None, None
    db.session.commit()
    return sent


def send_test(by: str) -> str:
    from app.extensions import db
    from app.models import AlertEvent
    title = "Test: Fleet Portal alerts work"
    body = f"Sent from the Alerts page by {by}. Real alerts look like this, with the problem and the building."
    delivered = notify.send(title, body, urgent=False, path="/alerts")
    db.session.add(AlertEvent(controller_id=None, controller_name=None, kind="test", title=title,
                              text=body, delivered=delivered[:300], created_at=_dt.datetime.utcnow()))
    db.session.commit()
    return delivered
