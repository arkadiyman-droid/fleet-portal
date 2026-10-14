"""
tests/test_alerts_db.py — alerts against the real schema (Postgres tier)

A controller goes offline, stays offline, comes back; the poll-cycle hook
(app/alerts.check_all) must page once, keep state across calls, record the
history, show it on the Alerts page and the menu badge, and respect a mute.
Sending is stubbed (app/notify.send); tests/test_alerts.py covers channels.
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

T0 = dt.datetime(2026, 10, 14, 12, 0, 0)


@pytest.fixture
def client(app):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def sent(monkeypatch):
    import app.notify as n
    calls = []
    monkeypatch.setattr(n, "send", lambda title, body, **kw: calls.append((title, body, kw)) or "ntfy ok")
    return calls


@pytest.fixture
def ctrl(session):
    import app.models as m
    c = m.Client(name="Acme"); session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Main St"); session.add(p); session.flush()
    b = m.Building(property_id=p.id, name="12 Main St"); session.add(b); session.flush()
    k = m.Controller(building_id=b.id, name="Boiler room", tailscale_ip="100.64.0.7", fleet_api_token="x" * 64)
    session.add(k); session.commit()
    return k


def _status(session, ctrl, ok, at):
    import app.models as m
    row = session.get(m.ControllerStatus, ctrl.id) or m.ControllerStatus(controller_id=ctrl.id)
    row.ok, row.polled_at = ok, at
    row.error = None if ok else "timed out"
    row.last_ok_at = at if ok else T0 - dt.timedelta(minutes=1)
    row.raw_json = {"buildingName": "12 Main St"} if ok else row.raw_json
    session.add(row); session.commit()


def _tick(app, session, ctrl, minute, ok):
    from app import alerts
    at = T0 + dt.timedelta(minutes=minute)
    _status(session, ctrl, ok, at)
    return alerts.check_all(app, now=at)


class Paging:

    def test_offline_pages_once_then_recovers(self, app, session, ctrl, sent, client):
        import app.models as m
        for minute in (0, 1, 2, 3, 4):
            _tick(app, session, ctrl, minute, ok=False)
        assert sent == []                                           # not yet: under five minutes
        _tick(app, session, ctrl, 5, ok=False)
        _tick(app, session, ctrl, 6, ok=False)
        assert len(sent) == 1
        title, body, kw = sent[0]
        assert title == "ALERT: Boiler room (12 Main St)" and body.startswith("Offline")
        assert kw["urgent"] is True and kw["path"] == f"/fleet/{ctrl.id}"
        st = session.get(m.AlertState, ctrl.id)
        assert st.alerting and st.issue.startswith("Offline")

        page = client.get("/alerts")
        assert page.status_code == 200 and b"In alarm now" in page.data and b"Boiler room" in page.data
        assert b'class="navbadge">1<' in page.data

        _tick(app, session, ctrl, 40, ok=True)
        assert len(sent) == 2 and sent[1][0].startswith("Recovered: ") and sent[1][2]["good_news"] is True
        assert not session.get(m.AlertState, ctrl.id).alerting
        kinds = [e.kind for e in m.AlertEvent.query.order_by(m.AlertEvent.created_at).all()]
        assert kinds == ["open", "recovered"]
        assert b'class="navbadge"' not in client.get("/alerts").data

    def test_mute_from_the_controller_page(self, app, session, ctrl, sent, client):
        import app.models as m
        r = client.post(f"/fleet/{ctrl.id}/mute", data={"hours": "4"})
        assert r.status_code == 302
        st = session.get(m.AlertState, ctrl.id)
        assert st.muted_until > dt.datetime.utcnow() + dt.timedelta(hours=3)
        page = client.get(f"/fleet/{ctrl.id}")
        assert b"Muted until" in page.data and b"Turn alerts back on" in page.data
        client.post(f"/fleet/{ctrl.id}/mute", data={"hours": "0"})
        session.expire_all()
        assert session.get(m.AlertState, ctrl.id).muted_until is None
        assert [e.kind for e in m.AlertEvent.query.all()] == ["mute", "mute"]

    def test_a_muted_controller_does_not_page(self, app, session, ctrl, sent):
        import app.models as m
        session.add(m.AlertState(controller_id=ctrl.id, alerting=False, muted_until=T0 + dt.timedelta(hours=4)))
        session.commit()
        for minute in range(0, 30, 5):
            _tick(app, session, ctrl, minute, ok=False)
        assert sent == []

    def test_test_alert_button(self, app, session, sent, client):
        import app.models as m
        r = client.post("/alerts/test")
        assert r.status_code == 302 and len(sent) == 1 and sent[0][0].startswith("Test:")
        assert m.AlertEvent.query.one().kind == "test"

    def test_alerts_can_be_switched_off(self, app, session, ctrl, sent):
        app.config["ALERTS_ENABLED"] = False
        for minute in range(0, 30, 5):
            _tick(app, session, ctrl, minute, ok=False)
        assert sent == []


class Setup:

    def test_the_alerts_page_shows_the_topic_and_a_qr_code_to_the_administrator(self, app, client):
        app.config["ALERT_NTFY_TOPIC"] = "echotherm-abc123def456ghi7"
        page = client.get("/alerts").data
        assert b"echotherm-abc123def456ghi7" in page and b"<svg" in page

    def test_an_expired_button_says_so_instead_of_400(self, app, session):
        app.config["WTF_CSRF_ENABLED"] = True
        c = app.test_client()
        with c.session_transaction() as s:
            s["superuser"] = True
        r = c.post("/alerts/test", headers={"Referer": "http://localhost/alerts"})
        assert r.status_code == 302 and r.headers["Location"].endswith("/alerts")
        assert b"open too long" in c.get("/alerts").data
