"""
app/rollouts.py — software updates from the portal, one batch at a time

A Rollout sends one release (a commit on the distribution repo's deploy
branch) to controllers in waves:

  wave 0   the test controller the operator picked
  wave 1+  everyone else that needs it, batch_size at a time

For each controller the portal POSTs /api/fleet/software {"action":
"update", "sha": target} with its polling token (EMS fleet/software.py).
The controller fetches that release itself, with its own read-only key,
and installs it with its own safeguards: validation first, a 10-minute
confirmation window, automatic rollback if it crash-loops. The portal then
watches the controller's own report (fleetHealth.ota in /api/status) until
it is on the release and confirmed.

A wave is finished when every controller in it is done, failed or skipped.
If any FAILED (refused, rolled back, did not finish in time), the rollout
HALTS: the release spreads no further and the operator is told on the
alert channels. Otherwise the portal waits soak_minutes and checks that
none of the wave turned red (app/fleet_health.py crit) before starting the
next wave; one that did halts it too. Skipped (not failed): a controller
offline the whole time it was tried, one whose site switched remote
updates off, one too old to accept remote updates.

The portal never rolls a fleet back by itself: a bad release is stopped
from spreading; the controllers it already reached keep their own
crash-loop rollback; putting the previous release back on the deploy
branch is a deliberate act. step() runs after every poll cycle inside the
poller's advisory lock, so one process drives each rollout.
"""
import datetime as _dt
import logging

import requests

from app import fleet_health, notify

log = logging.getLogger(__name__)

TRY_FOR = _dt.timedelta(minutes=60)        # keep asking an offline/busy controller this long
FINISH_WITHIN = _dt.timedelta(minutes=30)  # update + 10 min confirmation must be done by then
HTTP_TIMEOUT = 30                          # the controller reads the release branch before answering
ACTIVE = ("pending", "requested", "confirming")


def ota(ctrl) -> dict:
    raw = (ctrl.status.raw_json if ctrl.status else None) or {}
    return ((raw.get("fleetHealth") or {}).get("ota") or {})


def current_sha(ctrl):
    return ota(ctrl).get("currentSha")


def send_update(app, ctrl, sha):
    """(http status or None, json dict). Network errors -> (None, {...})."""
    port = app.config.get("FLEET_OBIX_PORT", 8061)
    try:
        r = requests.post(f"http://{ctrl.tailscale_ip}:{port}/api/fleet/software",
                          json={"action": "update", "sha": sha}, timeout=HTTP_TIMEOUT,
                          headers={"Authorization": f"Bearer {ctrl.fleet_api_token}"})
        try:
            body = r.json()
        except ValueError:
            body = {}
        return r.status_code, body if isinstance(body, dict) else {}
    except requests.RequestException as e:
        return None, {"error": f"unreachable ({type(e).__name__})"}


def ask_to_check(app, controllers, sender=None) -> dict:
    """Ask each controller to look at its release branch now (they look by
    themselves only once a day). Parallel, bounded; the answers show up in
    their next status poll. Returns {"asked": n, "answered": n, "seen": {sha: n}}."""
    from concurrent.futures import ThreadPoolExecutor
    port = app.config.get("FLEET_OBIX_PORT", 8061)

    def one(c):
        if sender:
            return sender(app, c)
        try:
            r = requests.post(f"http://{c.tailscale_ip}:{port}/api/fleet/software", json={"action": "check"},
                              timeout=HTTP_TIMEOUT, headers={"Authorization": f"Bearer {c.fleet_api_token}"})
            return r.json() if r.status_code == 200 else None
        except (requests.RequestException, ValueError):
            return None

    targets = [c for c in controllers if c.tailscale_ip and c.fleet_api_token]
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(one, targets))
    seen = {}
    for a in answers:
        if a and a.get("availableSha"):
            seen[a["availableSha"]] = seen.get(a["availableSha"], 0) + 1
    return {"asked": len(targets), "answered": sum(1 for a in answers if a), "seen": seen}


# ── creating one ─────────────────────────────────────────────────────────────

def busy_controller_ids():
    from app.models import Rollout, RolloutTarget
    rows = RolloutTarget.query.join(Rollout).filter(Rollout.state.in_(("running", "paused")),
                                                    RolloutTarget.state.in_(ACTIVE)).all()
    return {t.controller_id for t in rows}


def releases_seen(controllers):
    """{sha: number of controllers whose release branch shows it}, newest first by count."""
    seen = {}
    for c in controllers:
        o = ota(c)
        if o.get("availableSha"):
            seen[o["availableSha"]] = seen.get(o["availableSha"], 0) + 1
    return dict(sorted(seen.items(), key=lambda kv: -kv[1]))


def create(target_sha, controllers, test_controller_id, batch_size=10, soak_minutes=30, by=None):
    """A running Rollout over `controllers` (those already on the release are
    left out). Raises ValueError with a plain reason."""
    from app.extensions import db
    from app.models import Rollout, RolloutTarget
    target_sha = (target_sha or "").strip().lower()
    if len(target_sha) != 40 or any(ch not in "0123456789abcdef" for ch in target_sha):
        raise ValueError("pick a release")
    batch_size = max(1, min(int(batch_size or 10), 200))
    soak_minutes = max(0, min(int(soak_minutes or 0), 24 * 60))
    busy = busy_controller_ids()
    # A controller that has never reported its software cannot be followed:
    # it is left out (the Updates page lists it) rather than holding up a batch.
    need = [c for c in controllers if current_sha(c) and current_sha(c) != target_sha]
    if not need:
        raise ValueError("every chosen controller is already on that release")
    clash = [c.name for c in need if c.id in busy]
    if clash:
        raise ValueError("already being updated by another rollout: " + ", ".join(clash[:5]))
    first = next((c for c in need if c.id == test_controller_id), None)
    if first is None:
        raise ValueError("the test controller must be one that needs the update")
    rest = [c for c in need if c.id != first.id]
    r = Rollout(target_sha=target_sha, batch_size=batch_size, soak_minutes=soak_minutes, created_by=by,
                state="running", wave=0, created_at=_dt.datetime.utcnow())
    r.targets.append(RolloutTarget(controller_id=first.id, wave=0, from_sha=current_sha(first)))
    for i, c in enumerate(rest):
        r.targets.append(RolloutTarget(controller_id=c.id, wave=1 + i // batch_size, from_sha=current_sha(c)))
    db.session.add(r)
    db.session.commit()
    return r


# ── driving them ─────────────────────────────────────────────────────────────

def _finish(t, state, now, detail=None):
    t.state, t.finished_at = state, now
    if detail is not None:
        t.detail = detail[:300]


def _try(app, t, r, now, sender):
    ctrl = t.controller
    t.first_try_at = t.first_try_at or now
    if not ctrl.is_active or not ctrl.tailscale_ip or not ctrl.fleet_api_token:
        return _finish(t, "skipped", now, "not configured for polling")
    if current_sha(ctrl) == r.target_sha:
        return _finish(t, "done", now, "already on it")
    status, body = sender(app, ctrl, r.target_sha)
    code = body.get("code")
    if status == 200 and body.get("already"):
        return _finish(t, "done", now, "already on it")
    if status == 200 and body.get("ok"):
        t.state, t.requested_at, t.detail = "requested", now, "installing"
        return
    if status == 409 and code == "release_moved":
        _finish(t, "failed", now, body.get("error") or "the release branch moved")
        return "release_moved"
    if status == 409 and code == "known_bad":
        return _finish(t, "failed", now, body.get("error") or "this release failed here before")
    if status == 403:
        return _finish(t, "skipped", now, "remote updates are switched off at this site")
    if status == 404:
        return _finish(t, "skipped", now, "controller software too old for remote updates: update it by hand once")
    if status == 401:
        return _finish(t, "failed", now, "the controller refused the portal's token")
    # offline, busy confirming an earlier update, or a passing error: try again next cycle
    t.detail = (body.get("reason") or body.get("error") or f"answered {status}")[:300]
    if now - t.first_try_at > TRY_FOR:
        _finish(t, "skipped", now, f"could not start it for an hour: {t.detail}")


def _watch(t, r, now):
    o = ota(t.controller)
    cur, pending, ok = o.get("currentSha"), o.get("pendingConfirm"), o.get("lastUpdateOk")
    if cur == r.target_sha and pending:
        t.state, t.detail = "confirming", "installed, proving itself (10 min)"
        return
    if cur == r.target_sha and not pending and ok is not False and t.state == "confirming":
        return _finish(t, "done", now, "updated and confirmed")
    if cur == r.target_sha and not pending and ok is True:
        return _finish(t, "done", now, "updated and confirmed")
    if ok is False and cur != r.target_sha and t.state == "confirming":
        return _finish(t, "failed", now, "it crash-looped and rolled itself back")
    if ok is False and cur != r.target_sha and o.get("lastUpdateTs") and t.requested_at and \
            _dt.datetime.fromtimestamp(o["lastUpdateTs"], _dt.timezone.utc).replace(tzinfo=None) >= t.requested_at - _dt.timedelta(minutes=1):
        return _finish(t, "failed", now, "the update failed on the controller")
    if now - (t.requested_at or now) > FINISH_WITHIN:
        return _finish(t, "failed", now, "did not finish within 30 minutes")


def _halt(r, now, reason):
    r.state, r.reason, r.finished_at = "halted", reason[:300], now
    log.warning("Rollout %s halted: %s", r.id, reason)
    notify.send(f"Software rollout HALTED ({r.target_sha[:7]})", reason, urgent=True, path="/updates")


def step(app, now=None, sender=None) -> None:
    from app.extensions import db
    from app.models import Rollout
    now = now or _dt.datetime.utcnow()
    sender = sender or send_update
    for r in Rollout.query.filter_by(state="running").order_by(Rollout.id).all():
        wave = [t for t in r.targets if t.wave == r.wave]
        for t in wave:
            if t.state == "pending":
                if _try(app, t, r, now, sender) == "release_moved":
                    _halt(r, now, f"The release branch moved past {r.target_sha[:7]} while the rollout ran. "
                                  "Start a new rollout for the new release.")
                    break
            elif t.state in ("requested", "confirming"):
                _watch(t, r, now)
        if r.state != "running":
            continue
        if any(t.state in ACTIVE for t in wave):
            continue
        failed = [t for t in wave if t.state == "failed"]
        if failed:
            names = ", ".join(f"{t.controller.name} ({t.detail})" for t in failed[:3])
            _halt(r, now, f"Stopped after wave {r.wave}: {names}. No more controllers will get "
                          f"{r.target_sha[:7]} until you start again.")
            continue
        r.wave_done_at = r.wave_done_at or now
        if now - r.wave_done_at < _dt.timedelta(minutes=r.soak_minutes):
            continue
        red = [t.controller.name for t in wave if t.state == "done"
               and fleet_health.evaluate(t.controller, t.controller.status, now_dt=now)["level"] == "crit"]
        if red:
            _halt(r, now, f"Stopped after wave {r.wave}: {', '.join(red[:3])} turned red after updating.")
            continue
        if any(t.wave > r.wave for t in r.targets):
            r.wave, r.wave_done_at = r.wave + 1, None
            continue
        r.state, r.finished_at = "done", now
        done = sum(1 for t in r.targets if t.state == "done")
        skipped = [t for t in r.targets if t.state == "skipped"]
        msg = f"{done} controller(s) on {r.target_sha[:7]}." + (
            f" {len(skipped)} skipped: " + ", ".join(f"{t.controller.name} ({t.detail})" for t in skipped[:3])
            if skipped else "")
        notify.send(f"Software rollout complete ({r.target_sha[:7]})", msg, urgent=False, good_news=True,
                    path="/updates")
    db.session.commit()
