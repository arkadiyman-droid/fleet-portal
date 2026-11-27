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
    assert "02:59" in page                                             # the hot-water run time that was read
    assert "1 of 4 reading (" in page and "1 dark" in page and "1 login refused" in page
    assert "The box refused the login" in page                         # Other Login Bldg
    assert "Another device answers at this address" in page             # Stale Address
    assert "no answer" in page                                         # Dark Building
    table = client.get("/live?view=table").data.decode()
    assert ">Hi stk</th>" in table and "Outside Temp. Satisfied" in table
    # the Read column: the box that answered carries the moment of this cycle, the rest were never read
    assert f'<td data-v="{int(st.last_ok_at.timestamp())}">just now</td>' in table
    assert table.count('<td data-v="">never</td>') == 3
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


def test_hide_show_and_delete(app, session, boxes, client):
    import app.models as m
    from app import legacy
    app.config["WTF_CSRF_ENABLED"] = False
    dark, other = boxes[1], boxes[2]

    r = client.post("/live/action", data={"action": "hide", "box_id": [str(dark.id), str(other.id)]})
    assert r.status_code == 302
    session.expire_all()
    assert not session.get(m.LegacyBox, dark.id).is_active
    seen = []
    legacy.poll_all(app, poll=lambda j, now: seen.append(j["url"][-4:]) or fake_poll(j, now))
    assert sorted(seen) == ["8060"]                                    # hidden boxes are not read
    page = client.get("/live").data.decode()
    assert "Dark Building" not in page and "Hidden 2" in page
    hidden = client.get("/live?show=hidden").data.decode()
    assert "Dark Building" in hidden and "Other Login Bldg" in hidden and "Show again" in hidden

    client.post("/live/action", data={"action": "show", "box_id": str(other.id)})
    client.post("/live/action", data={"action": "delete", "box_id": str(dark.id)})
    session.expire_all()
    assert session.get(m.LegacyBox, other.id).is_active
    gone = session.get(m.LegacyBox, dark.id)
    assert gone.deleted_at is not None and not gone.is_active          # kept as a marker for re-imports
    assert client.get(f"/live/{dark.id}").status_code == 404
    page = client.get("/live?show=hidden").data.decode()
    assert "Dark Building" not in page
    seen.clear()
    legacy.poll_all(app, now=dt.datetime.utcnow() + dt.timedelta(minutes=5),
                    poll=lambda j, now: seen.append(j["url"][-4:]) or fake_poll(j, now))
    assert sorted(seen) == ["8060", "8062"]


def _delete_onclick(page):
    """The one Delete button's onclick, as (how it is written, what a browser
    makes of it). The two differ exactly when a quote in the name ends the
    attribute early, which leaves a handler that cannot run and a button that
    deletes without asking."""
    import re
    from html.parser import HTMLParser
    found = []

    class Buttons(HTMLParser):
        def handle_starttag(self, tag, attrs):
            d = dict(attrs)
            if tag == "button" and d.get("name") == "action" and d.get("value") == "delete":
                found.append(d.get("onclick"))

    Buttons().feed(page)
    assert len(found) == 1, found
    written = [t for t in re.findall(r"<button\b[^>]*>", page) if 'value="delete"' in t]
    assert len(written) == 1, written
    return written[0], found[0]


def test_delete_confirm_is_safe_for_any_name(app, session, boxes, client):
    import json
    import re
    b = boxes[1]
    name = "O'Brien \"Court\""
    b.name, b.is_active = name, False
    session.commit()
    # the Hidden cards ask through lvConfirmDelete (type DELETE); the box's own page through confirm()
    for url, call in (("/live?view=cards&show=hidden", "lvConfirmDelete"), (f"/live/{b.id}", "confirm")):
        written, onclick = _delete_onclick(client.get(url).data.decode())
        assert "O\\u0027Brien" in written and "O" + chr(39) + "Brien" not in written, url
        # the browser must see one whole call whose only argument is one string holding the name
        whole = re.fullmatch(r"return %s\((.*)\)" % call, onclick or "", re.S)
        assert whole, (url, onclick)
        assert name in json.loads(whole.group(1)), (url, onclick)


def test_actions_need_the_administrator(app, session, boxes):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    assert c.post("/live/action", data={"action": "delete", "box_id": str(boxes[1].id)}).status_code in (302, 403)
    session.expire_all()
    import app.models as m
    assert session.get(m.LegacyBox, boxes[1].id).deleted_at is None


def test_the_flame_shows_a_burner_that_ran(app, session, boxes, client):
    from app import history
    now = dt.datetime.utcnow()
    for b, (h0, h1) in zip(boxes[:2], ((100, 104), (100, 100))):
        history.save([history.reading(b.id, now - dt.timedelta(minutes=m), {"status": {"boiler1HeatRunMinutes": str(h)}})
                      for m, h in ((6, h0), (1, h1))], [], now)
    from app.routes import _firing
    assert _firing(now, [b.id for b in boxes]) == {boxes[0].id: "heat"}


def test_the_badge_opens_the_building_issues(app, session, boxes, client):
    import json
    import re
    import app.models as m
    from app import legacy
    legacy.poll_all(app, poll=fake_poll)                                    # 8060 reads, 8061 dark, 8062 refuses
    now = dt.datetime.utcnow()
    session.add(m.LegacyAlertState(box_id=boxes[0].id, alerting=True, opened_at=now))
    session.add(m.LegacyEvent(box_id=boxes[0].id, ts=now, kind="status", code="Safety", text="Outside Temp. Satisfied -> Safety"))
    session.commit()
    page = client.get("/live?view=cards").data.decode()
    # one JSON list for the page; each badge carries its row number into it
    lst = json.loads(re.search(r'<script type="application/json" id="lv-issues">(.*?)</script>', page, re.S).group(1))
    panels = {lst[int(i)]["name"]: lst[int(i)] for i in re.findall(r'class="lv-badge lv-iss-open[^"]*" data-i="(\d+)"', page)}
    assert len(panels) == len(lst)
    bedell = panels["10-12 Bedell St"]
    assert any("Phone alert open" in p for p in bedell["problems"]) and bedell["events"][0]["kind"] == "status"
    assert panels["Dark Building"]["problems"] == ["no answer"] and panels["Dark Building"]["keys"] == "inside,out"
    assert panels["Other Login Bldg"]["problems"] == ["The box refused the login"]
    assert 'class="lv-badge lv-iss-open alarm"' in page
