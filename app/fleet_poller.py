"""
app/fleet_poller.py — background status poller for Fleet Portal Phase 2.

Polls each active Controller's GET /api/status over Tailscale, using the
Bearer token EMS's own use_controller/fleet/token.py validates, and stores
the result as a single latest-snapshot ControllerStatus row (see
app/models.py). Read-only — no control actions, no writes to any site.

Runs as a daemon thread started once from create_app() — the only pattern
available, since this app has no persistent-process infra (no Celery, no
APScheduler) yet. Guarded by a Postgres advisory lock per poll cycle so
running multiple workers/replicas against the same database never causes
duplicate polling: whichever process holds the lock polls that cycle,
everyone else skips and retries next interval.

Log-and-swallow throughout, matching EMS's own backup/OTA worker style —
a single controller's poll failure (or a whole cycle's) must never take
the poller thread down.
"""

import datetime
import logging
import threading
import time

import requests
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Controller, ControllerStatus

log = logging.getLogger(__name__)

_ADVISORY_LOCK_KEY = 875_219_001  # arbitrary fixed id, scoped to this feature


class FleetPoller:
    def __init__(self):
        self._thread = None

    def start(self, app) -> None:
        if not app.config.get("FLEET_POLLER_ENABLED", True):
            log.info("Fleet poller disabled (FLEET_POLLER_ENABLED=0)")
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="fleet-poller", daemon=True)
        self._thread.start()
        log.info("Fleet poller started")

    def _run(self, app) -> None:
        interval = app.config.get("FLEET_POLL_INTERVAL_SECONDS", 60)
        while True:
            try:
                self._run_cycle(app)
            except Exception as e:
                log.warning("Fleet poller cycle failed: %s", e)
            time.sleep(interval)

    def _run_cycle(self, app) -> None:
        with app.app_context():
            conn = db.engine.connect()
            try:
                got_lock = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}).scalar()
                if not got_lock:
                    return  # another worker/process is already polling this cycle
                controllers = Controller.query.filter(
                    Controller.is_active.is_(True),
                    Controller.tailscale_ip.isnot(None),
                    Controller.fleet_api_token.isnot(None),
                ).all()
                for ctrl in controllers:
                    try:
                        poll_controller(app, ctrl)
                    except Exception as e:
                        log.warning("Poll failed for controller %s (%s): %s", ctrl.id, ctrl.name, e)
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
                conn.close()


def poll_controller(app, ctrl: Controller) -> ControllerStatus:
    """Shared by the background loop and the on-demand 'Poll Now' route.
    Must be called inside an app context with an active db session."""
    port = app.config.get("FLEET_OBIX_PORT", 8061)
    timeout = app.config.get("FLEET_POLL_TIMEOUT_SECONDS", 8)
    url = f"http://{ctrl.tailscale_ip}:{port}/api/status"
    started = time.monotonic()

    # The network call comes FIRST and the row is looked up only afterwards.
    # Looking it up before the call (as Phase 2 did) left a window as long as
    # the timeout -- 8 s on an offline controller -- in which the background
    # poller and a "Refresh now" click could both see "no row yet", both
    # INSERT, and the second crash on the primary key (found 2026-10-13 on
    # the first Refresh of a never-polled, unreachable controller).
    result = {}
    try:
        resp = requests.get(url, timeout=timeout, headers={"Authorization": f"Bearer {ctrl.fleet_api_token}"})
        resp.raise_for_status()
        payload = resp.json()
        result = {"ok": True, "http_status": resp.status_code, "error": None, "raw_json": payload,
                  "building_name": payload.get("buildingName"), "version": payload.get("version"),
                  "last_ok_at": datetime.datetime.utcnow()}
    except Exception as e:
        # raw_json/building_name/version deliberately left untouched — the
        # rollup page shows last-known-good data plus the error, not a blank.
        result = {"ok": False, "http_status": getattr(getattr(e, "response", None), "status_code", None),
                  "error": str(e)[:500]}
    result["response_ms"] = int((time.monotonic() - started) * 1000)
    result["polled_at"] = datetime.datetime.utcnow()

    # Even after the call the window is not zero, so a lost race on the
    # INSERT is retried once as an UPDATE of the row the other side made.
    for attempt in (1, 2):
        row = db.session.get(ControllerStatus, ctrl.id) or ControllerStatus(controller_id=ctrl.id)
        for k, v in result.items():
            setattr(row, k, v)
        db.session.add(row)
        try:
            db.session.commit()
            return row
        except IntegrityError:
            db.session.rollback()
            if attempt == 2:
                raise
    return row


fleet_poller = FleetPoller()
