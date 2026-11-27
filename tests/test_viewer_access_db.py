"""
tests/test_viewer_access_db.py — a view-only account sees its own properties
and nothing else (Postgres tier).

Two owners, three properties. One person is given view access to ONE
property. Every monitoring page and data feed is then tried as that person:
their own buildings open, everyone else's are refused, and nothing that
changes anything is reachable. The administrator is unaffected.
"""
import datetime as dt

import pytest
from werkzeug.security import check_password_hash, generate_password_hash

from tests.conftest import requires_postgres
from tests.test_bloom_fans import _building

pytestmark = requires_postgres


@pytest.fixture
def world(app, session):
    """Returns ids. Owner A: property Berry (2 buildings: one 2010 box, one
    Bloom asset, one fan site) and property DCA (1 building, box + fan site).
    Owner B: property Other (1 building, box + Bloom asset). Plus an unfiled
    box, an unfiled Bloom asset and an unfiled fan site."""
    import app.models as m
    from app import bloom_fans as bf
    app.config["WTF_CSRF_ENABLED"] = False
    now = dt.datetime.utcnow()
    a, b = m.Client(name="Owner A"), m.Client(name="Owner B")
    session.add_all([a, b]); session.flush()
    berry, dca, other = (m.Property(client_id=a.id, name="Berry"), m.Property(client_id=a.id, name="DCA"),
                         m.Property(client_id=b.id, name="Other"))
    session.add_all([berry, dca, other]); session.flush()
    b1, b2 = m.Building(property_id=berry.id, name="50 South 9 St"), m.Building(property_id=berry.id, name="440 Berry St")
    b3, b4 = m.Building(property_id=dca.id, name="18 Monroe St"), m.Building(property_id=other.id, name="9 Elsewhere Ave")
    session.add_all([b1, b2, b3, b4]); session.flush()
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    acct = m.BloomAccount(label="omni", username="u", password="p")
    session.add_all([login, acct]); session.flush()

    def box(name, port, building):
        return m.LegacyBox(name=name, host="203.0.113.77", port=port, identity="gateway", login_id=login.id,
                           building_id=building.id if building else None)
    boxes = [box("50 South 9 St", 8001, b1), box("18 Monroe St", 8002, b3), box("9 Elsewhere Ave", 8003, b4),
             box("Loose Box", 8004, None)]
    assets = [m.BloomAsset(account_id=acct.id, asset_key="440-Berry-St", name="440 Berry St", building_id=b2.id),
              m.BloomAsset(account_id=acct.id, asset_key="9-Elsewhere", name="9 Elsewhere Ave", building_id=b4.id),
              m.BloomAsset(account_id=acct.id, asset_key="loose", name="Loose Asset", building_id=None)]

    def fan(name, key):
        raw = _building()
        raw["name"], raw["locid"] = name, key
        return m.BloomFanSite(account_id=acct.id, asset_key=key, name=name, data=bf.trim(raw), polled_at=now)
    fans = [fan("440 Berry St", "440-Berry-St"), fan("18 Monroe St", "18-Monroe-St"),
            fan("9 Elsewhere Ave", "9-Elsewhere-Ave"), fan("77 Nowhere Rd", "77-Nowhere-Rd")]
    viewer = m.User(name="Pat Viewer", email="pat@example.test", login="pat",
                    password_hash=generate_password_hash("first-password-1"))
    session.add_all(boxes + assets + fans + [viewer]); session.flush()
    session.add(m.Permission(user_id=viewer.id, scope_type="property", scope_id=berry.id, role="viewer"))
    session.commit()
    return {"viewer": viewer.id, "berry": berry.id, "dca": dca.id, "other": other.id,
            "box_mine": boxes[0].id, "box_dca": boxes[1].id, "box_other": boxes[2].id, "box_loose": boxes[3].id,
            "asset_mine": assets[0].id, "asset_other": assets[1].id, "asset_loose": assets[2].id,
            "fan_mine": fans[0].id, "fan_dca": fans[1].id, "fan_other": fans[2].id, "fan_loose": fans[3].id}


def _as(app, **who):
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(who)
    return c


@pytest.fixture
def pat(app, world):
    return _as(app, user_id=world["viewer"])


@pytest.fixture
def root(app, world):
    return _as(app, superuser=True)


def test_a_viewer_lands_on_their_own_properties(pat, world):
    r = pat.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/home")
    page = pat.get("/home")
    html = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "Berry" in html and "50 South 9 St" in html and "440 Berry St" in html
    for foreign in ("18 Monroe St", "9 Elsewhere Ave", "Owner B", "Loose Box", "Loose Asset", "77 Nowhere Rd"):
        assert foreign not in html
    assert "Roof exhaust fans" in html                      # Berry's fan site is on the page
    # the short menu: no fleet-wide pages, no management pages
    for hidden in (">Portfolio<", ">Live<", ">Users<", ">Billing<", ">Analytics<", ">Adopt<", ">Clients<"):
        assert hidden not in html
    assert ">My properties<" in html and ">Fans<" in html and "view only" in html


def test_a_viewer_opens_their_own_buildings_and_no_one_elses(pat, world):
    w = world
    assert pat.get(f"/live/{w['box_mine']}").status_code == 200
    assert pat.get(f"/skyline/{w['asset_mine']}").status_code == 200
    for url in (f"/live/{w['box_dca']}", f"/live/{w['box_other']}", f"/live/{w['box_loose']}",
                f"/skyline/{w['asset_other']}", f"/skyline/{w['asset_loose']}"):
        assert pat.get(url).status_code == 403, url


def test_the_data_feeds_behind_a_page_follow_the_same_rule(pat, world):
    w = world
    for path in ("history.json", "series.json", "history.csv?res=hourly&days=30"):
        assert pat.get(f"/live/{w['box_mine']}/{path}").status_code == 200, path
        assert pat.get(f"/live/{w['box_other']}/{path}").status_code == 403, path
        assert pat.get(f"/live/{w['box_loose']}/{path}").status_code == 403, path
    assert pat.get(f"/skyline/{w['asset_mine']}/series.json").status_code == 200
    assert pat.get(f"/skyline/{w['asset_other']}/series.json").status_code == 403
    assert pat.get(f"/fans/{w['fan_mine']}/series.json").status_code == 200
    for k in ("fan_dca", "fan_other", "fan_loose"):
        assert pat.get(f"/fans/{w[k]}/series.json").status_code == 403, k


def test_a_viewers_box_page_shows_readings_but_no_controls_or_address(pat, root, world):
    html = pat.get(f"/live/{world['box_mine']}").get_data(as_text=True)
    assert "50 South 9 St" in html
    for secret in ("203.0.113.77", "8001", "Delete", ">Hide<", "Mute for", "/live/action"):
        assert secret not in html, secret
    admin = root.get(f"/live/{world['box_mine']}").get_data(as_text=True)
    assert "203.0.113.77:8001" in admin and "Delete" in admin and "Mute for" in admin


def test_the_fans_page_is_only_their_buildings_and_has_no_switches(pat, root, world):
    page = pat.get("/fans")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and "440 Berry St" in html
    for foreign in ("18 Monroe St", "9 Elsewhere Ave", "77 Nowhere Rd"):
        assert foreign not in html
    for control in ("Schedules &amp; switching", "Push plan schedules", "Raw Bloom values", "Open schedules in Bloom"):
        assert control not in html, control
    admin = root.get("/fans").get_data(as_text=True)
    for name in ("440 Berry St", "18 Monroe St", "9 Elsewhere Ave", "77 Nowhere Rd", "Schedules &amp; switching"):
        assert name in admin


def test_nothing_that_changes_anything_is_reachable(pat, world):
    w = world
    for url in ("/portfolio", "/live", "/skyline", "/heat", "/sensors", "/analytics", "/users", "/billing",
                "/fans/property?name=Berry", f"/fans/{w['fan_mine']}/push", f"/fans/{w['fan_mine']}/raw",
                f"/users/{w['viewer']}/permissions"):
        assert pat.get(url).status_code == 403, url
    posts = {"/live/action": {"action": "delete", "box_id": w["box_mine"]},
             f"/live/{w['box_mine']}/mute": {"hours": "4"},
             "/fans/property": {"name": "Berry", "action": "auto_on"},
             f"/fans/{w['fan_mine']}/push": {"digest": "x", "note": "x"},
             "/portfolio/delete": {}, "/portfolio/move": {}, "/portfolio/assign": {},
             f"/users/{w['viewer']}/properties": {"property_id": w["other"]},
             f"/users/{w['viewer']}/permissions": {"scope_type": "client", "scope_id": 1, "role": "admin"}}
    for url, data in posts.items():
        assert pat.post(url, data=data).status_code == 403, url
    import app.models as m
    assert m.LegacyBox.query.get(w["box_mine"]).deleted_at is None
    assert m.BloomFanAuto.query.count() == 0
    assert [p.scope_id for p in m.Permission.query.filter_by(user_id=w["viewer"]).all()] == [w["berry"]]


def test_a_second_property_is_one_more_tick(root, pat, world, session):
    import app.models as m
    w = world
    assert pat.get(f"/live/{w['box_dca']}").status_code == 403
    r = root.post(f"/users/{w['viewer']}/properties", data={"property_id": [w["berry"], w["dca"]]})
    assert r.status_code == 302
    assert pat.get(f"/live/{w['box_dca']}").status_code == 200
    assert "18 Monroe St" in pat.get("/home").get_data(as_text=True)
    assert "18 Monroe St" in pat.get("/fans").get_data(as_text=True)
    # unticking takes it away again; an operator grant is never removed by the tick list
    session.add(m.Permission(user_id=w["viewer"], scope_type="property", scope_id=w["other"], role="operator"))
    session.commit()
    root.post(f"/users/{w['viewer']}/properties", data={"property_id": [w["berry"]]})
    held = {(p.scope_id, p.role) for p in m.Permission.query.filter_by(user_id=w["viewer"]).all()}
    assert held == {(w["berry"], "viewer"), (w["other"], "operator")}
    assert pat.get(f"/live/{w['box_dca']}").status_code == 403


def test_a_grant_on_one_building_shows_only_that_building(app, world, session):
    import app.models as m
    w = world
    solo = m.User(name="Sam Single", email="sam@example.test", login="sam", password_hash=generate_password_hash("x" * 12))
    session.add(solo); session.flush()
    b2 = m.Building.query.filter_by(name="440 Berry St").one()
    session.add(m.Permission(user_id=solo.id, scope_type="building", scope_id=b2.id, role="viewer"))
    session.commit()
    sam = _as(app, user_id=solo.id)
    html = sam.get("/home").get_data(as_text=True)
    assert "440 Berry St" in html and "50 South 9 St" not in html
    assert sam.get(f"/skyline/{w['asset_mine']}").status_code == 200
    assert sam.get(f"/live/{w['box_mine']}").status_code == 403
    assert sam.get(f"/fans/{w['fan_mine']}/series.json").status_code == 200


def test_an_account_with_nothing_shared_sees_nothing(app, world, session):
    import app.models as m
    nobody = m.User(name="New Person", email="new@example.test", login="new", password_hash=generate_password_hash("x" * 12))
    session.add(nobody); session.commit()
    c = _as(app, user_id=nobody.id)
    html = c.get("/home").get_data(as_text=True)
    assert "Nothing has been shared with this account yet" in html and "Berry" not in html
    fans = c.get("/fans").get_data(as_text=True)
    assert "440 Berry St" not in fans
    assert c.get(f"/live/{world['box_mine']}").status_code == 403


def test_a_hidden_or_archived_building_is_not_shown_to_a_viewer(pat, world, session):
    import app.models as m
    box = m.LegacyBox.query.get(world["box_mine"])
    box.is_active = False                                   # hidden by the administrator
    session.commit()
    assert pat.get(f"/live/{world['box_mine']}").status_code == 403
    m.Building.query.filter_by(name="440 Berry St").one().is_active = False
    session.commit()
    assert "440 Berry St" not in pat.get("/home").get_data(as_text=True).split("Roof exhaust fans")[0]


def test_alerts_show_a_viewer_only_their_own_boxes(pat, root, world, session):
    import app.models as m
    w, now = world, dt.datetime.utcnow()
    session.add_all([
        m.LegacyAlertState(box_id=w["box_mine"], alerting=True, opened_at=now, issue="Safety"),
        m.LegacyAlertState(box_id=w["box_other"], alerting=True, opened_at=now, issue="Safety"),
        m.AlertEvent(legacy_box_id=w["box_mine"], controller_name="50 South 9 St", kind="open", title="mine tripped",
                     delivered="not sent", created_at=now),
        m.AlertEvent(legacy_box_id=w["box_other"], controller_name="9 Elsewhere Ave", kind="open", title="theirs tripped",
                     delivered="not sent", created_at=now)])
    session.commit()
    html = pat.get("/alerts").get_data(as_text=True)
    assert "50 South 9 St" in html and "mine tripped" in html
    assert "9 Elsewhere Ave" not in html and "theirs tripped" not in html
    assert "Send a test alert" not in html
    admin = root.get("/alerts").get_data(as_text=True)
    assert "9 Elsewhere Ave" in admin and "50 South 9 St" in admin


def test_a_person_can_change_their_own_password(pat, world, session):
    import app.models as m
    bad = pat.post("/account", data={"current": "wrong", "new": "second-password-2", "again": "second-password-2"})
    assert bad.status_code == 302
    session.expire_all()
    assert check_password_hash(m.User.query.get(world["viewer"]).password_hash, "first-password-1")
    for new, again in (("short", "short"), ("second-password-2", "different-one-22")):
        pat.post("/account", data={"current": "first-password-1", "new": new, "again": again})
        session.expire_all()
        assert check_password_hash(m.User.query.get(world["viewer"]).password_hash, "first-password-1")
    pat.post("/account", data={"current": "first-password-1", "new": "second-password-2", "again": "second-password-2"})
    session.expire_all()
    assert check_password_hash(m.User.query.get(world["viewer"]).password_hash, "second-password-2")
    page = pat.get("/account").get_data(as_text=True)
    assert "Property: Berry" in page and "view only" in page


def test_a_new_user_goes_straight_to_choosing_what_they_see(root, world):
    r = root.post("/users/new", data={"name": "Lee New", "email": "lee@example.test", "login": "lee",
                                      "password": "a-first-password"})
    assert r.status_code == 302 and "/permissions" in r.headers["Location"]
    page = root.get(r.headers["Location"]).get_data(as_text=True)
    assert "Save access" in page and "Berry" in page and "DCA" in page and "Other" in page
    listing = root.get("/users").get_data(as_text=True)
    assert "Berry (view)" in listing and "nothing yet" in listing


def test_the_administrator_still_sees_everything(root, world):
    w = world
    for url in ("/portfolio", "/live", "/fans", "/home", "/users", f"/live/{w['box_other']}", f"/live/{w['box_loose']}",
                f"/skyline/{w['asset_loose']}", f"/fans/{w['fan_loose']}/series.json", f"/fans/{w['fan_mine']}/raw"):
        assert root.get(url).status_code == 200, url
    home = root.get("/home").get_data(as_text=True)
    assert "Owner A" in home and "Owner B" in home and "9 Elsewhere Ave" in home


# ── "See as": the administrator looks at the portal as one of the users ────────

def test_the_administrator_can_see_the_portal_as_a_viewer(root, world):
    w = world
    assert "See as" in root.get("/users").get_data(as_text=True)
    r = root.post(f"/view-as/{w['viewer']}")
    assert r.status_code == 302
    home = root.get("/", follow_redirects=True).get_data(as_text=True)
    assert "seeing the portal exactly as <b>Pat Viewer</b>" in home and "Back to administrator" in home
    assert "50 South 9 St" in home and "440 Berry St" in home and "18 Monroe St" not in home and "9 Elsewhere" not in home
    # their rule, not the administrator's: someone else's box is refused, the Users page is not theirs
    assert root.get(f"/live/{w['box_mine']}").status_code == 200
    assert root.get(f"/live/{w['box_other']}").status_code in (403, 404)
    assert root.get("/users").status_code in (302, 403)
    fans = root.get("/fans").get_data(as_text=True)
    assert "440 Berry St" in fans and "18 Monroe St" not in fans and "Schedules &amp; switching" not in fans
    # and back: the administrator again, landing on that person's access page
    r = root.post("/view-as/stop")
    assert r.status_code == 302 and f"/users/{w['viewer']}/permissions" in r.headers["Location"]
    assert root.get("/users").status_code == 200
    assert "seeing the portal exactly as" not in root.get("/fans").get_data(as_text=True)


def test_nothing_can_be_changed_while_seeing_as_someone(root, world, session):
    import app.models as m
    w = world
    root.post(f"/view-as/{w['viewer']}")
    before = session.get(m.User, w["viewer"]).password_hash
    r = root.post("/account", data={"current": "first-password-1", "new": "a-brand-new-password", "again": "a-brand-new-password"})
    assert r.status_code == 303                                         # turned back, not carried out
    session.expire_all()
    assert session.get(m.User, w["viewer"]).password_hash == before
    assert "Nothing can be changed from here" in root.get("/home").get_data(as_text=True)
    root.post("/view-as/stop")
    assert root.get("/users").status_code == 200


def test_only_the_administrator_can_see_as_someone(pat, root, world, session):
    import app.models as m
    w = world
    other = m.User(name="Sam Other", email="sam@example.test", login="sam", password_hash="!email-sign-in-only")
    session.add(other); session.commit()
    assert pat.post(f"/view-as/{other.id}").status_code == 403           # a viewer cannot become someone else
    assert "Back to administrator" not in pat.get("/home").get_data(as_text=True)
    # an archived account cannot be looked through, and one archived mid-look drops back to the administrator
    root.post(f"/view-as/{other.id}")
    assert "as <b>Sam Other</b>" in root.get("/home").get_data(as_text=True)
    other.is_active = False; session.commit()
    assert root.get("/users").status_code == 200
    assert root.post(f"/view-as/{other.id}", follow_redirects=True).status_code == 200
    assert "seeing the portal exactly as" not in root.get("/home").get_data(as_text=True)


# ── My properties: the average and the hot water ───────────────────────────────

def test_my_properties_shows_the_average_and_the_mix_temperature(pat, world, session):
    import re
    import app.models as m
    now = dt.datetime.utcnow()

    def reading(apartments, mix):
        st = {"callForHeat": "0", "averageTemperature": "71", "targetInsideTemperature": "72",
              "systemTemperature0": "48", "systemTemperature1": "151", "systemTemperature2": "139",
              "systemTemperature3": str(mix)}
        st.update({f"apartmentTemperature{i}": str(v) for i, v in enumerate(apartments)})
        names = {"systemSensorName0": "OUT", "systemSensorName1": "HWTS", "systemSensorName2": "HWTR",
                 "systemSensorName3": "MIX"}
        return {"status": st, "names": names}

    session.add(m.LegacyStatus(box_id=world["box_mine"], polled_at=now, ok=True, last_ok_at=now,
                               data=reading([70, 72, 71], 124)))
    session.commit()
    page = pat.get("/home").get_data(as_text=True)
    assert ">Average</th>" in page and ">Hot water</th>" in page
    row = re.search(r"<tr>(?:(?!</tr>).)*50 South 9 St.*?</tr>", page, re.S).group(0)
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r'<td class="num"[^>]*>(.*?)</td>', row, re.S)]
    assert cells[:6] == ["48°", "71°", "72°", "151°", "139°", "124°"]      # outdoor, average, setpoint, supply, return, hot water
    assert "Read from: mixing valve" in row

    # every apartment sensor silent: the box's 71 is a placeholder, not an average; a mix of 0 is not hot water
    st = session.get(m.LegacyStatus, world["box_mine"])
    st.data = reading([0, 0, 0], 0)
    session.commit()
    page = pat.get("/home").get_data(as_text=True)
    row = re.search(r"<tr>(?:(?!</tr>).)*50 South 9 St.*?</tr>", page, re.S).group(0)
    cells = [re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r'<td class="num"[^>]*>(.*?)</td>', row, re.S)]
    assert cells[1] == "—" and cells[5] == "—" and "No apartment sensor is reporting" in row


def test_the_users_list_has_a_search_box(root, world):
    users = root.get("/users").get_data(as_text=True)
    assert 'id="el-q"' in users and "Search by name, email or property" in users and 'id="el-table"' in users
    assert 'id="el-q"' not in root.get("/clients").get_data(as_text=True)      # only where it was asked for


# ── My properties: readable by someone seeing it for the first time ────────────

def test_my_properties_explains_itself(pat, world, session):
    import re
    import app.models as m
    from app import portfolio
    now = dt.datetime.utcnow()
    session.add(m.LegacyStatus(box_id=world["box_mine"], polled_at=now, ok=True, last_ok_at=now,
                               data={"status": {"callForHeat": "Outsd Sat", "averageTemperature": "74"}, "names": {}}))
    session.commit()
    page = pat.get("/home").get_data(as_text=True)
    assert "How to read this page" in page and ">What to do</th>" in page and "Roof fan status" in page
    assert "email the person who gave you this access" in page
    assert "Send your own maintenance person or electrician to that fan first" in page
    row = re.search(r"<tr>(?:(?!</tr>).)*50 South 9 St.*?</tr>", page, re.S).group(0)
    assert ">OK</span>" in row and 'title="Normal. The controller is reporting' in row
    assert "Resting: warm enough outside" in row                              # the box's code, in plain words
    # a controller that stopped answering says since when, so a short gap and a long one do not look alike
    st = m.LegacyStatus.query.get(world["box_mine"])
    st.ok, st.last_ok_at = False, now - dt.timedelta(days=2)
    session.commit()
    row = re.search(r"<tr>(?:(?!</tr>).)*50 South 9 St.*?</tr>", pat.get("/home").get_data(as_text=True), re.S).group(0)
    assert ">Not reporting</span>" in row and "The controller has not answered since " in row
    assert portfolio._silent(None) == "The controller has never answered"
    assert portfolio._silent(dt.datetime(2026, 11, 15, 18, 36)) == "The controller has not answered since Nov 15, 1:36 PM"


def test_the_portfolio_reads_every_building_in_a_few_queries(app, world, session):
    """Portfolio and My properties fetch every building's driver up front
    (portfolio.drivers()) instead of ~4 queries a building: same entries, and
    the query count does not grow with the fleet."""
    import time
    from sqlalchemy import event
    from app import portfolio
    from app.extensions import db
    from app.models import Building
    now, now_dt = time.time(), dt.datetime.utcnow()
    rc, found = portfolio.rc_dhw_by_building(), portfolio.drivers()
    for b in Building.query.all():
        assert portfolio.building_entry(b, now, now_dt, rc, found) == portfolio.building_entry(b, now, now_dt, rc), b.name
    by_name = {e["name"]: e for o in portfolio.tree() for p in o["props"] for e in p["b"]}
    assert by_name["9 Elsewhere Ave"]["source"] == "Bloom"            # Bloom wins over the 2010 box, as before
    assert by_name["50 South 9 St"]["source"] == "2010" and by_name["440 Berry St"]["source"] == "Bloom"
    n = [0]
    count = lambda *a, **k: n.__setitem__(0, n[0] + 1)
    event.listen(db.engine, "before_cursor_execute", count)
    try:
        db.session.expunge_all()
        portfolio.tree()
        few = n[0]
        import app.models as m
        prop = m.Property.query.filter_by(name="DCA").one()
        for i in range(12):                                         # a dozen more buildings, each with a box
            bl = m.Building(property_id=prop.id, name=f"{i} New St")
            session.add(bl); session.flush()
            session.add(m.LegacyBox(name=f"{i} New St", host="203.0.113.78", port=9000 + i, identity="gateway",
                                    login_id=m.LegacyLogin.query.first().id, building_id=bl.id))
        session.commit()
        db.session.expunge_all()
        n[0] = 0
        assert len([e for o in portfolio.tree() for p in o["props"] for e in p["b"]]) == 16
        assert n[0] == few <= 12, (few, n[0])
    finally:
        event.remove(db.engine, "before_cursor_execute", count)


def test_the_sensors_page_says_when_it_was_checked(root, world):
    html = root.get("/sensors").get_data(as_text=True)
    assert "Checked " in html and "No sensors look faulty" in html


def _box_reading(session, box_id, avg, target):
    import app.models as m
    now = dt.datetime.utcnow()
    session.add(m.LegacyStatus(box_id=box_id, polled_at=now, ok=True, last_ok_at=now, data={
        "read_at": now.isoformat(), "config": {"heatControlType": "11"},
        "status": {"averageTemperature": str(avg), "targetInsideTemperature": str(target), "callForHeat": "Circ Pump",
                   "apartmentTemperature0": str(avg), "apartmentTemperature1": str(avg),
                   "serverTime": now.strftime("%Y-%m-%dT%H:%M:%SZ")}}))
    session.commit()


def test_the_fans_page_shows_the_apartments_under_the_fans(root, world, session):
    _box_reading(session, world["box_dca"], 76, 71)                     # 18 Monroe St, under the DCA fan site
    html = root.get("/fans").get_data(as_text=True)
    assert ">Apartments<" in html                                       # the column
    assert "Apartments average 76°, target 71° (18 Monroe St)" in html  # the row's reading and where it is from
    assert "fn-apt hot" in html                                         # 5 over target
    assert "Apartments <b>76.0°</b> (target 71.0°)" in html             # the property's average
    assert "Fan effect →" in html
    j = root.get(f"/fans/{world['fan_dca']}/series.json?days=1").get_json()
    assert j["apt"]["box"] == "18 Monroe St" and j["apt"]["res"] == "every read"


def test_the_fan_effect_page_is_the_administrators(root, pat, world):
    html = root.get("/fans/effect").get_data(as_text=True)
    assert "Fan effect" in html and "How this is measured" in html
    assert "Not enough switches yet" in html or "Not measurable yet" in html
    assert pat.get("/fans/effect").status_code == 403
