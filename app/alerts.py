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

The installed 2010 boxes (/live) go through the same decide() from their
own poller: check_legacy() below says what makes one of them red.
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


# ── the installed 2010 boxes (/live, app/legacy.py) ──────────────────────────
#
# Same decide() as the controllers, different evidence, learned from the real
# boxes (2026-10-14):
#   * a box keeps an alarm "active" in its list for weeks after it is over
#     (27 boxes still showed an old ignition failure), so the box's CURRENT
#     status code (Safety, Ignition Failure) is what says it is in trouble now;
#   * a NEW safety or ignition alarm is sent at once, without the two-poll
#     wait, because it may have cleared itself between two polls;
#   * a box that has never answered is never paged about; one that was
#     reading and stops (or starts refusing the login) is, after
#     LEGACY_DARK_MINUTES;
#   * when a large share of them go dark together it is the portal's own
#     connection, not the buildings: one message, not hundreds.

_FRESH = _dt.timedelta(hours=2)
_OUTAGE_MIN_BOXES = 10


def _local(utc):
    from app import legacy
    return utc.replace(tzinfo=_dt.timezone.utc).astimezone(legacy.LOCAL).strftime("%b %d %I:%M %p").replace(" 0", " ")


def legacy_dark(st, now, dark_after) -> bool:
    """Was reading, and has not been for dark_after."""
    return bool(st and not st.ok and st.last_ok_at and now - st.last_ok_at >= dark_after)


def legacy_issues(st, now, dark_after):
    """Why one box is red now: lines ([] = fine), or None for no news (not
    read this time, but not dark long enough to say anything yet)."""
    from app import legacy
    if st is None:
        return None
    if not st.ok:
        if not st.last_ok_at:
            return []                                    # never answered: never paged about
        if not legacy_dark(st, now, dark_after):
            return None
        since = _local(st.last_ok_at)
        if st.auth_failed:
            return [f"The box refuses the portal's login (last read {since})."]
        return [f"Not answering since {since}."]
    c = legacy.card(st.data or {})
    return [f"Status: {c['status']}."] if c["tone"] == "crit" else []


def legacy_fresh_alarm(data, last_key, now):
    """(key, line): the key of the box's latest alarm, and a line to send when
    it is a NEW safety or ignition alarm from the last two hours. last_key
    None means nothing was seen before (first look): remember, never send.
    Some boxes answer their latest alarm on one read and an empty list on
    the next (seen 2026-10-15: the same ignition failure alerted twice), so
    an empty answer keeps the last key: only a different alarm is news."""
    from app import legacy
    a = (data or {}).get("alarm")
    if not a:
        return last_key, None
    key = f"{a.get('name')}@{a.get('at')}"[:120]
    if last_key is None or key == last_key:
        return key, None
    info = legacy.alarm_info(a, now, legacy.clock_drift(data))
    if not info or info["level"] != "crit" or not info["utc"]:
        return key, None
    if not -_dt.timedelta(minutes=15) <= now - info["utc"] <= _FRESH:
        return key, None
    return key, f"Box alarm: {info['words']} at {info['when']}."


def legacy_message(kind: str, box, text: str):
    title = f"{_TITLES[kind]}: {box.name}"
    where = ""
    if box.building is not None:
        b = box.building
        p = b.property if b else None
        c = p.client if p else None
        where = " · ".join(x for x in (c.name if c else None, p.name if p else None, b.name) if x)
    return title, text + (f"\n{where}" if where else "")


def _legacy_outage(outage: bool, n_dark: int, n_ever: int, dark_min: int, now) -> int:
    """One message when most boxes go dark together, one when they are back."""
    from app.extensions import db
    from app.models import AlertEvent
    last = AlertEvent.query.filter(AlertEvent.kind.in_(("outage", "outage-end"))) \
        .order_by(AlertEvent.created_at.desc(), AlertEvent.id.desc()).first()
    in_outage = last is not None and last.kind == "outage"
    if outage == in_outage:
        return 0
    if outage:
        kind, title = "outage", "2010 boxes: most stopped answering"
        body = (f"{n_dark} of the {n_ever} boxes that were reading have not answered for {dark_min}+ minutes. "
                "So many at once is usually the portal's own internet connection, not the buildings. "
                "Single-box alerts for not answering are held back until this clears.")
    else:
        kind, title = "outage-end", "2010 boxes: answering again"
        body = "The boxes are answering again. Single-box alerts are back on."
    delivered = notify.send(title, body, urgent=outage, good_news=not outage, path="/live")
    db.session.add(AlertEvent(kind=kind, title=title, text=body[:1000], delivered=delivered[:300], created_at=now))
    log.info("Legacy %s: %s", kind, delivered)
    return 1


def check_legacy(app, now=None) -> int:
    """Evaluate every shown 2010 box once, after its poll cycle (same lock).
    Returns how many notifications were attempted."""
    from app.extensions import db
    from app.models import AlertEvent, LegacyAlertState, LegacyBox
    cfg = app.config
    if not cfg.get("ALERTS_ENABLED", True) or not cfg.get("LEGACY_ALERTS_ENABLED", True):
        return 0
    now = now or _dt.datetime.utcnow()
    dark_min = int(cfg.get("LEGACY_DARK_MINUTES", 30))
    dark_after = _dt.timedelta(minutes=dark_min)
    base = {"ALERT_AFTER_SECONDS": cfg.get("LEGACY_ALERT_AFTER_SECONDS", 240),
            "ALERT_REPEAT_HOURS": cfg.get("ALERT_REPEAT_HOURS", 24)}
    boxes = LegacyBox.query.filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None)).all()
    ever = [b for b in boxes if b.status is not None and b.status.last_ok_at]
    dark_ids = {b.id for b in ever if legacy_dark(b.status, now, dark_after)}
    outage = (len(ever) >= _OUTAGE_MIN_BOXES
              and len(dark_ids) >= max(_OUTAGE_MIN_BOXES, float(cfg.get("LEGACY_OUTAGE_SHARE", 0.3)) * len(ever)))
    sent = _legacy_outage(outage, len(dark_ids), len(ever), dark_min, now)
    seen = set()
    for b in boxes:
        seen.add(b.id)
        st = b.status
        row = db.session.get(LegacyAlertState, b.id) or LegacyAlertState(box_id=b.id, alerting=False)
        db.session.add(row)
        fresh = None
        if st is not None and st.ok:
            row.last_alarm, fresh = legacy_fresh_alarm(st.data, row.last_alarm, now)
        issues = None if (outage and b.id in dark_ids) else legacy_issues(st, now, dark_after)
        if issues is None:
            continue
        rule = dict(base)
        if fresh and not row.alerting:
            issues = [fresh] + issues
            rule["ALERT_AFTER_SECONDS"] = 0              # a new trip goes out at once
        state = {k: getattr(row, k) for k in ("alerting", "first_bad_at", "opened_at", "notified_at", "muted_until")}
        state["alerting"] = bool(state["alerting"])
        new, event = decide(state, "crit" if issues else "ok", issues, now, rule)
        for k, v in new.items():
            setattr(row, k, v)
        if issues:
            row.issue = issues[0][:300]
        elif not new["alerting"]:
            row.issue = None
        if event:
            title, body = legacy_message(event["kind"], b, event["text"])
            delivered = "muted: not sent"
            if event["send"]:
                delivered = notify.send(title, body, urgent=event["kind"] != "recovered",
                                        good_news=event["kind"] == "recovered", path=f"/live/{b.id}")
                sent += 1
            db.session.add(AlertEvent(legacy_box_id=b.id, controller_name=b.name[:200], kind=event["kind"],
                                      title=title[:200], text=body[:1000], delivered=delivered[:300], created_at=now))
            log.info("Legacy alert %s for %s: %s", event["kind"], b.name, delivered)
    # a box hidden or deleted while in alarm: close it quietly
    for row in LegacyAlertState.query.filter(LegacyAlertState.alerting.is_(True)).all():
        if row.box_id not in seen:
            row.alerting, row.opened_at, row.notified_at, row.first_bad_at = False, None, None, None
    db.session.commit()
    return sent
