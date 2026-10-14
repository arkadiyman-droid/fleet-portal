"""
tests/test_alerts_legacy_db.py — alerts for the 2010 boxes (Postgres tier)

Readings are written straight into LegacyStatus (what the poller stores),
then app/alerts.check_legacy runs as the poller would after each cycle.
Sending is stubbed (app/notify.send).
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


def _boxes(session, n):
    import app.models as m
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    session.add(login); session.flush()
    out = [m.LegacyBox(name=f"{i} Bedell St", host="192.0.2.7", port=8060 + i, identity="gateway", login_id=login.id)
           for i in range(n)]
    session.add_all(out); session.commit()
    return out


def _read(session, box, at, ok=True, code="Outsd Sat", alarm=None, auth_failed=False):
    import app.models as m
    row = session.get(m.LegacyStatus, box.id) or m.LegacyStatus(box_id=box.id)
    row.polled_at, row.ok, row.auth_failed = at, ok, auth_failed
    if ok:
        row.last_ok_at = at
        row.data = {"status": {"callForHeat": code}, "alarm": alarm}
    session.add(row); session.commit()


def _check(app, session, minute):
    from app import alerts
    alerts.check_legacy(app, now=T0 + dt.timedelta(minutes=minute))
    session.expire_all()


def _alarm(name, at):
    return {"name": name, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "active": True}


def test_safety_two_reads_then_recovered(app, session, sent, client):
    import app.models as m
    (b,) = _boxes(session, 1)
    for minute, code in ((0, "Outsd Sat"), (5, "Safety")):
        _read(session, b, T0 + dt.timedelta(minutes=minute), code=code)
        _check(app, session, minute)
    assert sent == []                                                  # one read is not enough
    _read(session, b, T0 + dt.timedelta(minutes=10), code="Safety")
    _check(app, session, 10)
    assert len(sent) == 1 and sent[0][0] == "ALERT: 0 Bedell St" and "Status: Safety." in sent[0][1]
    assert sent[0][2]["path"] == f"/live/{b.id}" and sent[0][2]["urgent"]

    page = client.get("/alerts").data.decode()
    assert "0 Bedell St" in page and "2010 box" in page and "Status: Safety." in page
    assert "In alarm" in client.get(f"/live/{b.id}").data.decode()

    _read(session, b, T0 + dt.timedelta(minutes=15), code="Safety")
    _check(app, session, 15)
    assert len(sent) == 1                                              # no repeat inside 24 h
    _read(session, b, T0 + dt.timedelta(minutes=20), code="Outsd Sat")
    _check(app, session, 20)
    assert len(sent) == 2 and sent[1][0] == "Recovered: 0 Bedell St" and sent[1][2]["good_news"]
    kinds = [e.kind for e in m.AlertEvent.query.filter_by(legacy_box_id=b.id).order_by(m.AlertEvent.id)]
    assert kinds == ["open", "recovered"]


def test_old_alarms_never_sent_new_trip_sent_at_once(app, session, sent):
    (b,) = _boxes(session, 1)
    old = _alarm("ignitionFail", T0 - dt.timedelta(days=9))
    _read(session, b, T0, alarm=old)
    _check(app, session, 0)
    _read(session, b, T0 + dt.timedelta(minutes=5), alarm=old)
    _check(app, session, 5)
    assert sent == []
    new = _alarm("ignitionFail", T0 + dt.timedelta(minutes=8))
    _read(session, b, T0 + dt.timedelta(minutes=10), alarm=new)       # status already fine again
    _check(app, session, 10)
    assert len(sent) == 1 and "Box alarm: Ignition failure" in sent[0][1]
    _read(session, b, T0 + dt.timedelta(minutes=15), alarm=new)
    _check(app, session, 15)
    assert len(sent) == 2 and sent[1][0].startswith("Recovered")


def test_dark_after_reading_but_never_answered_is_quiet(app, session, sent):
    import app.models as m
    reading, never = _boxes(session, 2)
    session.add(m.LegacyStatus(box_id=never.id, polled_at=T0, ok=False)); session.commit()
    _read(session, reading, T0)
    for minute in (5, 20, 35, 40):
        _read(session, reading, T0 + dt.timedelta(minutes=minute), ok=False)
        _check(app, session, minute)
    assert len(sent) == 1 and sent[0][0] == "ALERT: 0 Bedell St" and "Not answering since" in sent[0][1]
    _read(session, reading, T0 + dt.timedelta(minutes=45))
    _check(app, session, 45)
    assert sent[-1][0] == "Recovered: 0 Bedell St"


def test_many_dark_together_is_one_outage_message(app, session, sent):
    import app.models as m
    boxes = _boxes(session, 12)
    for b in boxes:
        _read(session, b, T0)
    for minute in (35, 40, 45):
        for b in boxes:
            _read(session, b, T0 + dt.timedelta(minutes=minute), ok=False)
        _check(app, session, minute)
    assert [s[0] for s in sent] == ["2010 boxes: most stopped answering"]
    assert m.LegacyAlertState.query.filter_by(alerting=True).count() == 0
    for b in boxes:
        _read(session, b, T0 + dt.timedelta(minutes=50))
    _check(app, session, 50)
    assert [s[0] for s in sent] == ["2010 boxes: most stopped answering", "2010 boxes: answering again"]


def test_mute_and_hide(app, session, sent, client):
    import app.models as m
    a, b = _boxes(session, 2)
    r = client.post(f"/live/{a.id}/mute", data={"hours": "24"})
    assert r.status_code == 302
    for minute in (0, 5, 10):
        _read(session, a, T0 + dt.timedelta(minutes=minute), code="Safety")
        _read(session, b, T0 + dt.timedelta(minutes=minute), code="Safety")
        _check(app, session, minute)
    assert [s[0] for s in sent] == ["ALERT: 1 Bedell St"]             # the muted one stays quiet
    client.post("/live/action", data={"action": "hide", "box_id": str(b.id)})
    _check(app, session, 15)
    assert not session.get(m.LegacyAlertState, b.id).alerting       # closed quietly
    assert len(sent) == 1
    assert "Muted until" in client.get(f"/live/{a.id}").data.decode()
