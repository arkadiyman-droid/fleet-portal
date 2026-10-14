"""
tests/test_legacy_db.py — the live view of the installed gateways (Postgres tier)

The poll cycle (app/legacy.poll_all) is run with a stand-in for the network;
then the Live page and a box's page must show what was read, dark boxes and
refused logins must stand out, and logins must be stored encrypted.
"""
import datetime as dt

import pytest
from sqlalchemy import text

from tests.conftest import requires_postgres
from tests.test_legacy import ALARM_XML, NAMES_XML, STATUS_XML

pytestmark = requires_postgres


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def boxes(session):
    import app.models as m
    login = m.LegacyLogin(label="main", username="Manager", password="s3cret-pw")
    session.add(login); session.flush()
    out = []
    for name, port, identity in (("10-12 Bedell St", 8060, "gateway"), ("Dark Building", 8061, "unreachable"),
                                 ("Other Login Bldg", 8062, "gateway"), ("Stale Address", 8063, "other")):
        b = m.LegacyBox(name=name, host="192.0.2.7", port=port, identity=identity, login_id=login.id)
        session.add(b); out.append(b)
    session.commit()
    return out


def fake_poll(job, now):
    from app import legacy
    if job["url"].endswith(":8060"):
        return {"id": job["id"], "polled_at": now, "ok": True, "response_ms": 900, "identity": "gateway",
                "data": {"status": legacy.values(STATUS_XML), "alarm": legacy.parse_alarm(ALARM_XML),
                         "names": legacy.values(NAMES_XML)}}
    if job["url"].endswith(":8062"):
        return {"id": job["id"], "polled_at": now, "ok": False, "auth_failed": True, "error": "the box refused the login"}
    return {"id": job["id"], "polled_at": now, "ok": False, "identity": "unreachable", "error": "no answer"}


def test_a_cycle_then_the_live_page(app, session, boxes, client):
    import app.models as m
    from app import legacy
    seen = []
    n = legacy.poll_all(app, poll=lambda j, now: seen.append(j["url"][-4:]) or fake_poll(j, now))
    assert n == 1
    assert sorted(seen) == ["8060", "8061", "8062"]                    # the 'other' address is never polled
    st = session.get(m.LegacyStatus, boxes[0].id)
    assert st.ok and st.last_ok_at and st.names_at and st.data["status"]["callForHeat"] == "Outsd Sat"

    page = client.get("/live").data.decode()
    assert "Outside Temp. Satisfied" in page and "10-12 Bedell St" in page
    assert "02:59" in page and "1 of 4 reading now" in page
    assert "The box refused the login" in page                         # Other Login Bldg
    assert "Another device answers at this address" in page             # Stale Address
    assert "no answer" in page                                         # Dark Building
    table = client.get("/live?view=table").data.decode()
    assert "<th>Hi stk</th>" in table and "Outside Temp. Satisfied" in table
    probs = client.get("/live?show=problems").data.decode()
    assert "Dark Building" in probs and "Other Login Bldg" in probs
    box = client.get(f"/live/{boxes[0].id}").data.decode()
    assert "Aqu1" in box and "Ignition failure" in box and "192.0.2.7:8060" in box


def test_names_are_fetched_once_a_day(app, session, boxes):
    from app import legacy
    jobs = []
    legacy.poll_all(app, poll=lambda j, now: jobs.append(j) or fake_poll(j, now))
    first = {j["url"][-4:]: j["names_due"] for j in jobs}
    jobs.clear()
    legacy.poll_all(app, now=dt.datetime.utcnow() + dt.timedelta(minutes=5), poll=lambda j, now: jobs.append(j) or fake_poll(j, now))
    again = {j["url"][-4:]: j["names_due"] for j in jobs}
    assert first["8060"] is True and again["8060"] is False


def test_logins_are_encrypted_at_rest(app, session, boxes):
    raw = session.execute(text("select username, password from legacy_logins")).one()
    assert "Manager" not in raw[0] and "s3cret" not in raw[1]


def test_only_the_administrator_sees_it(app, session, boxes):
    c = app.test_client()
    assert c.get("/live").status_code in (302, 403)
