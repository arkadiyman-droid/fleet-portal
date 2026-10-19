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
        secs = (now - (st.get("opened_at") or now)).total_seconds()
        lasted = fleet_health.duration(secs)
        what = (st.get("issue") or "The problem").strip().rstrip(".")
        if what.startswith("Status: "):
            what = what[len("Status: "):]
        st.update(alerting=False, opened_at=None, notified_at=None)
        # a problem that cleared quickly closes quietly: the alert already went out
        long_enough = secs >= _secs(cfg, "ALERT_RECOVERED_MIN_SECONDS", 1800)
        return st, {"kind": "recovered", "send": not muted and long_enough,
                    "quiet": None if long_enough else f"cleared after {lasted}: closed quietly",
                    "text": f"{what}: cleared after {lasted}."}
    return st, None


# ── flapping: one message for a building that keeps tripping ──────────────────

def flap_gate(event, owner, now, cfg, name, issue):
    """Turn a would-be "open" or "recovered" into something calmer when the
    building keeps tripping: the third alert in ALERT_FLAP_HOURS becomes one
    "keeps tripping" message, and nothing else is sent for that building until
    ALERT_FLAP_HOURS after it. owner: AlertEvent filter (controller_id= or
    legacy_box_id=). Returns (event, extra_event_or_None)."""
    from app.models import AlertEvent
    hours = _secs(cfg, "ALERT_FLAP_HOURS", 6)
    need = int(_secs(cfg, "ALERT_FLAP_OPENS", 3))
    if not event or hours <= 0 or need <= 0 or event["kind"] not in ("open", "recovered"):
        return event, None
    since = now - _dt.timedelta(hours=hours)
    q = AlertEvent.query.filter_by(**owner).filter(AlertEvent.created_at >= since)
    paused = q.filter(AlertEvent.kind == "flapping").order_by(AlertEvent.created_at.desc()).first()
    if paused:
        until = paused.created_at + _dt.timedelta(hours=hours)
        event = dict(event, send=False, quiet=f"paused until {_local(until)}: keeps tripping")
        return event, None
    if event["kind"] == "open" and q.filter(AlertEvent.kind == "open").count() >= need - 1:
        n = q.filter(AlertEvent.kind == "open").count() + 1
        flap = {"kind": "flapping", "send": True,
                "text": f"{n} alerts in {hours:g} hours ({(issue or 'tripping').strip().rstrip('.')}). "
                        f"Alerts for this building are paused for {hours:g} hours; it stays on the Alerts page "
                        f"and in the morning summary."}
        return dict(event, send=False, quiet="folded into the keeps-tripping message"), flap
    return event, None


def _where(ctrl):
    b = ctrl.building
    p = b.property if b else None
    c = p.client if p else None
    return " · ".join(x for x in (c.name if c else None, p.name if p else None, b.name if b else None) if x)


_TITLES = {"open": "ALERT", "reminder": "Still in alarm", "recovered": "Recovered", "flapping": "Keeps tripping"}


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
        st = {k: getattr(row, k) for k in ("alerting", "first_bad_at", "opened_at", "notified_at", "muted_until", "issue")}
        st["alerting"] = bool(st["alerting"])
        new, event = decide(st, ev["level"], crit, now, cfg)
        new.pop("issue", None)
        for k, v in new.items():
            setattr(row, k, v)
        if ev["level"] == "crit" and crit:
            row.issue = crit[0][:300]
        elif not new["alerting"]:
            row.issue = None
        db.session.add(row)
        event, extra = flap_gate(event, {"controller_id": ctrl.id}, now, cfg, ctrl.name, row.issue or st.get("issue"))
        for e in (event, extra):
            if not e:
                continue
            title, body = message(e["kind"], ctrl, e["text"])
            delivered = e.get("quiet") or "muted: not sent"
            if e["send"]:
                delivered = notify.send(title, body, urgent=e["kind"] not in ("recovered",),
                                        good_news=e["kind"] == "recovered", path=f"/fleet/{ctrl.id}")
                sent += 1
            db.session.add(AlertEvent(controller_id=ctrl.id, controller_name=ctrl.name[:200], kind=e["kind"],
                                      title=title[:200], text=body[:1000], delivered=delivered[:300], created_at=now))
            log.info("Alert %s for %s: %s", e["kind"], ctrl.name, delivered)
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
    if c["tone"] != "crit":
        return []
    if c["code"] == "Ign Fail" and legacy.ignition_ignored(st.data):
        return []                                        # hot water: no fuel-valve feedback to trust
    return [f"Status: {c['status']}."]


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
    if a.get("name") == "ignitionFail" and legacy.ignition_ignored(data):
        return key, None                                 # hot water: see legacy.ignition_ignored
    if not -_dt.timedelta(minutes=15) <= now - info["utc"] <= _FRESH:
        return key, None
    return key, f"Box alarm: {info['words']} at {info['when']}."


EVIDENCE_WINDOW = _dt.timedelta(minutes=45)


def _trend(rows, attr, label):
    vals = [(r.ts, getattr(r, attr)) for r in rows if getattr(r, attr)]
    if len(vals) < 2:
        return None, None
    (t0, v0), (t1, v1) = vals[0], vals[-1]
    d, mins = v1 - v0, round((t1 - t0).total_seconds() / 60)
    word = "falling" if d <= -5 else "rising" if d >= 5 else "steady"
    return f"{label} {v0} -> {v1} F in {mins} min ({word})", word


def ignition_evidence(box_id, now):
    """For a steam ignition failure: what the aquastat and the hot-water mix
    did over the last 45 minutes. Falling means the burner really is not
    making heat; holding means it may have relit or the alarm was a blip."""
    from app.models import LegacyReading
    rows = LegacyReading.query.filter(LegacyReading.box_id == box_id, LegacyReading.ts >= now - EVIDENCE_WINDOW,
                                      LegacyReading.ts <= now).order_by(LegacyReading.ts).all()
    aqua, aw = _trend(rows, "aqua", "Aquastat")
    mix, mw = _trend(rows, "mix", "Hot water mix")
    parts = [p for p in (aqua, mix) if p]
    if not parts:
        return None, False
    falling = "falling" in (aw, mw)
    verdict = ("looks real: the boiler is not making heat" if falling
               else "aquastat holding: it may have relit, or the alarm was a blip")
    return "Evidence: " + "; ".join(parts) + f" - {verdict}.", falling


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
    from app import legacy
    from app.extensions import db
    from app.models import AlertEvent, LegacyAlertState, LegacyBox
    cfg = app.config
    if not cfg.get("ALERTS_ENABLED", True) or not cfg.get("LEGACY_ALERTS_ENABLED", True):
        return 0
    now = now or _dt.datetime.utcnow()
    dark_min = int(cfg.get("LEGACY_DARK_MINUTES", 30))
    dark_after = _dt.timedelta(minutes=dark_min)
    base = {"ALERT_AFTER_SECONDS": cfg.get("LEGACY_ALERT_AFTER_SECONDS", 540),
            "ALERT_REPEAT_HOURS": cfg.get("ALERT_REPEAT_HOURS", 0),
            "ALERT_RECOVERED_MIN_SECONDS": cfg.get("ALERT_RECOVERED_MIN_SECONDS", 1800)}
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
        # A box alarm alone is not an alert: if the box clears it by the next
        # reads, nothing is sent (it stays in the building's history). It only
        # helps confirm what the status already says.
        if fresh and not row.alerting:
            issues = [fresh] + issues
        if issues and any("gnition" in i for i in issues) and st is not None and \
                legacy.heat_system(st.data) == "steam":
            ev_line, falling = ignition_evidence(b.id, now)   # steam: show whether it is really cold
            if ev_line:
                issues = issues + [ev_line]
            if falling and not row.alerting:
                rule["ALERT_AFTER_SECONDS"] = 0          # the aquastat / hot water is really dropping: at once
        state = {k: getattr(row, k) for k in ("alerting", "first_bad_at", "opened_at", "notified_at", "muted_until",
                                              "issue")}
        state["alerting"] = bool(state["alerting"])
        new, event = decide(state, "crit" if issues else "ok", issues, now, rule)
        new.pop("issue", None)
        for k, v in new.items():
            setattr(row, k, v)
        if issues:
            row.issue = issues[0][:300]
        elif not new["alerting"]:
            row.issue = None
        if event and event["kind"] == "recovered" and st is not None and st.ok:
            event = dict(event, text=event["text"] + f"\nStatus now: {legacy.card(st.data or {})['status']}.")
        event, extra = flap_gate(event, {"legacy_box_id": b.id}, now, cfg, b.name, row.issue or state.get("issue"))
        for e in (event, extra):
            if not e:
                continue
            title, body = legacy_message(e["kind"], b, e["text"])
            delivered = e.get("quiet") or "muted: not sent"
            if e["send"]:
                delivered = notify.send(title, body, urgent=e["kind"] != "recovered",
                                        good_news=e["kind"] == "recovered", path=f"/live/{b.id}")
                sent += 1
            db.session.add(AlertEvent(legacy_box_id=b.id, controller_name=b.name[:200], kind=e["kind"],
                                      title=title[:200], text=body[:1000], delivered=delivered[:300], created_at=now))
            log.info("Legacy alert %s for %s: %s", e["kind"], b.name, delivered)
    # a box hidden or deleted while in alarm: close it quietly
    for row in LegacyAlertState.query.filter(LegacyAlertState.alerting.is_(True)).all():
        if row.box_id not in seen:
            row.alerting, row.opened_at, row.notified_at, row.first_bad_at = False, None, None, None
    db.session.commit()
    return sent


# ── one morning summary instead of per-item reminders ─────────────────────────

def daily_digest(app, now=None) -> bool:
    """Once a day at ALERT_DIGEST_HOUR (New York): what is still in alarm,
    who is paused for tripping, and what tripped and cleared in the last 24
    hours. Sent only when there is something to say. True when sent."""
    from app import legacy
    from app.extensions import db
    from app.models import AlertEvent, AlertState, Controller, LegacyAlertState, LegacyBox
    hour = int(app.config.get("ALERT_DIGEST_HOUR", 8))
    if hour < 0 or not app.config.get("ALERTS_ENABLED", True):
        return False
    now = now or _dt.datetime.utcnow()
    lt = now.replace(tzinfo=_dt.timezone.utc).astimezone(legacy.LOCAL)
    if lt.hour < hour:
        return False
    today = lt.replace(hour=hour, minute=0, second=0, microsecond=0).astimezone(_dt.timezone.utc).replace(tzinfo=None)
    if AlertEvent.query.filter(AlertEvent.kind == "digest", AlertEvent.created_at >= today).first():
        return False
    still = []
    for s in LegacyAlertState.query.join(LegacyBox, LegacyBox.id == LegacyAlertState.box_id) \
            .filter(LegacyAlertState.alerting.is_(True), LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None)):
        still.append((s.opened_at or now, f"{s.box.name}: {(s.issue or 'in alarm').rstrip('.')}"
                      f" (since {_local(s.opened_at) if s.opened_at else '?'})"))
    for s in AlertState.query.join(Controller).filter(AlertState.alerting.is_(True), Controller.is_active.is_(True)):
        still.append((s.opened_at or now, f"{s.controller.name}: {(s.issue or 'in alarm').rstrip('.')}"
                      f" (since {_local(s.opened_at) if s.opened_at else '?'})"))
    since = now - _dt.timedelta(hours=24)
    recent = AlertEvent.query.filter(AlertEvent.created_at >= since, AlertEvent.kind.in_(("open", "flapping"))).all()
    paused = sorted({e.controller_name for e in recent if e.kind == "flapping" and e.controller_name})
    trips = {}
    for e in recent:
        if e.kind == "open" and e.controller_name:
            trips[e.controller_name] = trips.get(e.controller_name, 0) + 1
    still_names = {line.split(":")[0] for _t, line in still}
    cleared = sorted(((n, c) for n, c in trips.items() if n not in still_names), key=lambda x: -x[1])
    if not still and not paused and not cleared:
        return False
    lines = []
    if still:
        lines.append(f"Still in alarm ({len(still)}):")
        lines += ["- " + line for _t, line in sorted(still)[:15]]
        if len(still) > 15:
            lines.append(f"- and {len(still) - 15} more")
    if paused:
        lines.append(f"Kept tripping, alerts paused ({len(paused)}): " + ", ".join(paused[:10]))
    if cleared:
        lines.append(f"Tripped and cleared in the last 24 h ({len(cleared)}): "
                     + ", ".join(f"{n} x{c}" if c > 1 else n for n, c in cleared[:10]))
    title = f"Morning summary: {len(still)} in alarm" if still else "Morning summary: nothing in alarm"
    body = "\n".join(lines)
    delivered = notify.send(title, body, urgent=False, path="/alerts")
    db.session.add(AlertEvent(kind="digest", title=title[:200], text=body[:1000], delivered=delivered[:300],
                              created_at=now))
    db.session.commit()
    return True
