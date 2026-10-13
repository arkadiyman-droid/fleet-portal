"""
tests/test_fleet_poller_db.py — polling a controller writes its one status row safely

Database tier (Postgres; skips without FLEET_TEST_DATABASE_URL).

THE RACE. ControllerStatus has one row per controller, keyed by the
controller id. The background poller and the "Refresh now" button both call
poll_controller(). Phase 2 looked the row up BEFORE the network call, so for
as long as the timeout (8 s on an offline controller) both could believe
there was no row and both INSERT; the loser crashed the page with a
primary-key violation. Found on 2026-10-13, the first time anyone pressed
Refresh on a never-polled, unreachable controller. Reproduced here by having
the "other poller" insert the row while our HTTP call is in flight.

LAST ANSWERED. last_ok_at moves only when the controller answers; on a
failure the last good data and that time are kept, so the dashboard can say
"offline, last seen 2 h ago" instead of a blank.
"""
import datetime
from unittest import mock

import pytest
import requests
from sqlalchemy import text

from tests.conftest import requires_postgres

pytestmark = requires_postgres


def _controller(session):
    import app.models as m
    c = m.Client(name="C"); session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="P"); session.add(p); session.flush()
    b = m.Building(property_id=p.id, name="B"); session.add(b); session.flush()
    ctrl = m.Controller(building_id=b.id, name="ctl", tailscale_ip="192.0.2.10", fleet_api_token="tok")
    session.add(ctrl); session.commit()
    return ctrl


def _resp(payload):
    # a function, not a class: pytest.ini collects every class as a test
    import types
    return types.SimpleNamespace(status_code=200, raise_for_status=lambda: None, json=lambda: payload)


def test_losing_the_insert_race_updates_instead_of_crashing(app, session):
    from app.extensions import db
    from app.fleet_poller import poll_controller
    import app.models as m
    ctrl = _controller(session)

    def other_poller_wins(*a, **kw):
        # while "our" HTTP call is in flight, the background poller writes the row
        with db.engine.connect() as conn:
            conn.execute(text("INSERT INTO controller_statuses (controller_id, polled_at, ok, error) "
                              "VALUES (:i, now(), false, 'from the other poller')"), {"i": ctrl.id})
            conn.commit()
        return _resp({"buildingName": "Bench", "version": "3.1.0"})

    with mock.patch("app.fleet_poller.requests.get", side_effect=other_poller_wins):
        row = poll_controller(app, ctrl)

    assert row.ok is True and row.building_name == "Bench"
    session.expire_all()
    rows = m.ControllerStatus.query.filter_by(controller_id=ctrl.id).all()
    assert len(rows) == 1 and rows[0].ok is True and rows[0].error is None


def test_success_sets_last_ok_and_failure_keeps_the_last_good_data(app, session):
    from app.fleet_poller import poll_controller
    ctrl = _controller(session)

    with mock.patch("app.fleet_poller.requests.get", return_value=_resp({"buildingName": "Bench", "version": "v1"})):
        first = poll_controller(app, ctrl)
    good_at = first.last_ok_at
    assert first.ok and good_at is not None

    with mock.patch("app.fleet_poller.requests.get", side_effect=requests.ConnectTimeout("timed out")):
        second = poll_controller(app, ctrl)
    assert second.ok is False
    assert "timed out" in second.error
    assert second.last_ok_at == good_at                 # how long it has really been gone
    assert second.raw_json == {"buildingName": "Bench", "version": "v1"}   # last known good kept
    assert second.polled_at >= good_at
