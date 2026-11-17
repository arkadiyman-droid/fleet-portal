"""
tests/test_directory_db.py — the Directory page end to end (Postgres tier).

The violations portal's directory is stubbed (app.directory.fetch) and so is
Cloudflare's verdict on who is at the keyboard (app.cf_access.identify, keyed
on the header value). Everything else is real: reading and storing the
directory, linking, approving, signing in by company email, and removing.
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres


def _directory(**changes):
    props = {
        101: {"id": 101, "name": "Berry (NYCHA)", "pm_name": "Pat Lee", "pm_email": "Pat.Lee@pathsco.com",
              "rpm_name": "Rae Diaz", "rpm_email": "rae@pathsco.com", "site_email": "berry@pathsco.com",
              "unit_count": 150, "open_violations": 12, "overdue_obligations": 3,
              "buildings": [{"address": "440 Berry Street aka 73 S 10th Street", "bin": "3000001"},
                            {"address": "46 South 9TH St"}]},
        102: {"id": 102, "name": "Betty", "pm_name": "Sam Wu", "pm_email": "sam@pathsco.com",
              "rpm_name": "Rae Diaz", "rpm_email": "rae@pathsco.com", "buildings": [{"address": "650 Gates Avenue"}]},
        103: {"id": 103, "name": "Medgar Evers", "pm_name": "Sam Wu", "pm_email": "sam@pathsco.com",
              "buildings": [{"address": "700 Gates Avenue"}]},
        104: {"id": 104, "name": "DCA1", "pm_name": "Typo Tom", "pm_email": "tom@pathsco,com",
              "buildings": [{"address": "1756 Park Place"}]},
        105: {"id": 105, "name": "Woodland Springs", "pm_name": "Far Away", "pm_email": "far@pathsco.com",
              "buildings": [{"address": "9 Elsewhere Road"}]},
    }
    for k, v in changes.items():
        if v is None:
            props.pop(int(k))
        else:
            props[int(k)].update(v)
    return {"org": "Paths", "generated_at": "2026-11-17T03:00:00", "properties": list(props.values())}


@pytest.fixture
def world(app, session, monkeypatch):
    import app.cf_access as cf
    import app.directory as directory
    import app.models as m
    app.config.update(WTF_CSRF_ENABLED=False, DIRECTORY_URL="http://127.0.0.1:8000", DIRECTORY_TOKEN="bev_test",
                      DIRECTORY_EMAIL_DOMAINS=("pathsco.com",), CF_ACCESS_TEAM_DOMAIN="example.cloudflareaccess.com",
                      CF_ACCESS_AUD="aud")
    served = {"data": _directory(), "calls": 0}

    def fetch(url, token, timeout=20):
        served["calls"] += 1
        if isinstance(served["data"], Exception):
            raise served["data"]
        return served["data"]
    monkeypatch.setattr(directory, "fetch", fetch)
    # Cloudflare's verdict: the header value "ok:<email>" is a verified email, anything else is not.
    monkeypatch.setattr(cf, "identify", lambda tok, team, aud: tok[3:].lower() if tok and tok.startswith("ok:") else None)
    owner = m.Client(name="PATHS")
    session.add(owner); session.flush()
    berry, bm, dca = (m.Property(client_id=owner.id, name="Berry"), m.Property(client_id=owner.id, name="Betty and Medgar"),
                      m.Property(client_id=owner.id, name="DCA"))
    session.add_all([berry, bm, dca]); session.flush()
    session.add_all([m.Building(property_id=berry.id, name="50 South 9 St"), m.Building(property_id=berry.id, name="440 Berry St"),
                     m.Building(property_id=bm.id, name="590-600 Gates Ave"), m.Building(property_id=dca.id, name="1756 Park pl")])
    session.commit()
    return {"served": served, "berry": berry.id, "bm": bm.id, "dca": dca.id}


@pytest.fixture
def root(app, world):
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


def _rows():
    import app.models as m
    return {r.name: r for r in m.DirectoryProperty.query.all()}


def _link_all(root, world):
    """Open the page (which reads the directory) and save the links it offers."""
    root.get("/directory")
    r = _rows()
    form = {"action": "links", f"link_{r['Berry (NYCHA)'].id}": world["berry"], f"link_{r['Betty'].id}": world["bm"],
            f"link_{r['Medgar Evers'].id}": world["bm"], f"link_{r['DCA1'].id}": world["dca"],
            f"link_{r['Woodland Springs'].id}": ""}
    assert root.post("/directory", data=form).status_code == 302


def test_the_page_reads_the_directory_and_offers_the_links(root, world, session):
    page = root.get("/directory")
    html = page.get_data(as_text=True)
    assert page.status_code == 200 and world["served"]["calls"] == 1
    assert "5</b> properties" in html and "Paths" in html
    r = _rows()
    assert r["Berry (NYCHA)"].pm_email == "Pat.Lee@pathsco.com" and r["Berry (NYCHA)"].open_violations == 12
    assert [b["address"] for b in r["Berry (NYCHA)"].buildings] == ["440 Berry Street aka 73 S 10th Street", "46 South 9TH St"]
    assert all(x.property_id is None for x in r.values())           # reading links nothing by itself
    # the names agree, so the match is ready-chosen in the page; nobody is listed to approve yet
    berry_select = html.split(f'name="link_{r["Berry (NYCHA)"].id}"')[1].split("</select>")[0]
    assert f'value="{world["berry"]}" selected' in berry_select
    assert "Nobody yet" in html
    root.get("/directory")
    assert world["served"]["calls"] == 1                             # read moments ago: not read again on every visit


def test_links_survive_a_reread_and_a_dropped_property_is_kept_but_marked(root, world, session):
    _link_all(root, world)
    assert _rows()["Betty"].property_id == world["bm"] and _rows()["Betty"].linked_by == "the administrator"
    world["served"]["data"] = _directory(**{"103": None, "101": {"pm_email": "new.manager@pathsco.com", "pm_name": "New Manager"}})
    assert root.post("/directory", data={"action": "sync"}).status_code == 302
    session.expire_all()
    r = _rows()
    assert r["Berry (NYCHA)"].property_id == world["berry"] and r["Berry (NYCHA)"].pm_email == "new.manager@pathsco.com"
    assert r["Medgar Evers"].present is False and r["Medgar Evers"].property_id == world["bm"]
    html = root.get("/directory").get_data(as_text=True)
    assert "<b>Medgar Evers</b>" not in html and "New Manager" in html


def test_nobody_has_access_until_approved_and_approval_is_view_only(app, root, world, session):
    import app.models as m
    _link_all(root, world)
    assert m.User.query.count() == 0 and m.Permission.query.count() == 0
    html = root.get("/directory").get_data(as_text=True)
    for who in ("Pat Lee", "Rae Diaz", "Sam Wu", "Typo Tom"):
        assert who in html
    assert "Far Away" not in html.split("2 · Who would see what")[1]        # their property is not linked
    assert "cannot approve" in html and "not a valid email address" in html
    r = root.post("/directory", data={"action": "approve", "email": ["pat.lee@pathsco.com", "rae@pathsco.com",
                                                                   "tom@pathsco,com", "far@pathsco.com"]})
    assert r.status_code == 302
    users = {u.email: u for u in m.User.query.all()}
    assert set(users) == {"pat.lee@pathsco.com", "rae@pathsco.com"}         # the typo and the unlinked one are not created
    assert users["rae@pathsco.com"].name == "Rae Diaz" and users["rae@pathsco.com"].password_hash.startswith("!")
    grants = {(p.user_id, p.scope_type, p.scope_id, p.role, p.source) for p in m.Permission.query.all()}
    assert grants == {(users["pat.lee@pathsco.com"].id, "property", world["berry"], "viewer", "directory"),
                      (users["rae@pathsco.com"].id, "property", world["berry"], "viewer", "directory"),
                      (users["rae@pathsco.com"].id, "property", world["bm"], "viewer", "directory")}
    after = root.get("/directory").get_data(as_text=True)
    assert after.count("has access") == 2 and "no account yet" in after     # Sam is still waiting
    root.post("/directory", data={"action": "approve", "email": ["pat.lee@pathsco.com"]})
    assert m.Permission.query.count() == 3                                  # approving twice changes nothing


def test_an_approved_manager_signs_in_with_their_company_email_alone(app, root, world, session):
    _link_all(root, world)
    root.post("/directory", data={"action": "approve", "email": ["pat.lee@pathsco.com"]})
    pat = app.test_client()
    h = {"Cf-Access-Jwt-Assertion": "ok:Pat.Lee@PathsCo.com"}
    home = pat.get("/home", headers=h)
    html = home.get_data(as_text=True)
    assert home.status_code == 200 and "Berry" in html and "50 South 9 St" in html and "Betty and Medgar" not in html
    assert "Pat Lee" in html and "view only" in html
    assert pat.get("/directory", headers=h).status_code == 403 and pat.get("/users", headers=h).status_code == 403
    account = pat.get("/account", headers=h).get_data(as_text=True)
    assert "no password to change" in account and "Change your password" not in account
    pat.post("/account", data={"current": "", "new": "a-new-password-1", "again": "a-new-password-1"}, headers=h)
    import app.models as m
    session.expire_all()
    assert m.User.query.one().password_hash.startswith("!")
    # the password form can never open an email-only account
    other = app.test_client()
    for pw in ("", "!email-sign-in-only", "anything"):
        assert other.post("/login", data={"username": "pat.lee@pathsco.com", "password": pw}).status_code == 200
        assert other.get("/home").status_code == 302


def test_a_verified_stranger_gets_nothing_and_logout_really_logs_out(app, root, world):
    _link_all(root, world)
    root.post("/directory", data={"action": "approve", "email": ["pat.lee@pathsco.com"]})
    stranger = app.test_client()
    hs = {"Cf-Access-Jwt-Assertion": "ok:nobody@pathsco.com"}
    r = stranger.get("/home", headers=hs)
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    notice = stranger.get("/login", headers=hs).get_data(as_text=True)
    assert "nobody@pathsco.com" in notice and "Nothing in the fleet portal has been shared" in notice
    assert app.test_client().get("/home", headers={"Cf-Access-Jwt-Assertion": "forged"}).status_code == 302
    assert app.test_client().get("/home").status_code == 302                # no header at all
    pat, h = app.test_client(), {"Cf-Access-Jwt-Assertion": "ok:pat.lee@pathsco.com"}
    assert pat.get("/home", headers=h).status_code == 200
    assert pat.post("/logout", headers=h).status_code == 302
    assert pat.get("/home", headers=h).status_code == 302                   # not signed straight back in
    page = pat.get("/login", headers=h).get_data(as_text=True)
    assert "Continue as pat.lee@pathsco.com" in page
    assert pat.get("/login/email", headers=h).status_code == 302
    assert pat.get("/home", headers=h).status_code == 200
    # an account the administrator switches off stops working at once
    import app.models as m
    from app.extensions import db
    m.User.query.one().is_active = False
    db.session.commit()
    assert pat.get("/home", headers=h).status_code == 302


def test_email_sign_in_is_off_unless_configured(app, root, world):
    _link_all(root, world)
    root.post("/directory", data={"action": "approve", "email": ["pat.lee@pathsco.com"]})
    app.config["CF_ACCESS_AUD"] = ""
    h = {"Cf-Access-Jwt-Assertion": "ok:pat.lee@pathsco.com"}
    assert app.test_client().get("/home", headers=h).status_code == 302
    assert "Sign-in by company email is off" in root.get("/directory").get_data(as_text=True)


def test_when_the_directory_changes_removal_is_offered_never_done(app, root, world, session):
    import app.models as m
    _link_all(root, world)
    root.post("/directory", data={"action": "approve", "email": ["rae@pathsco.com", "pat.lee@pathsco.com"]})
    rae = m.User.query.filter_by(email="rae@pathsco.com").one()
    session.add(m.Permission(user_id=rae.id, scope_type="property", scope_id=world["dca"], role="viewer"))   # by hand
    session.commit()
    # Rae stops being regional manager of Betty; Pat leaves the directory altogether
    world["served"]["data"] = _directory(**{"102": {"rpm_email": None, "rpm_name": None},
                                            "101": {"pm_email": "next@pathsco.com", "pm_name": "Next Person"}})
    root.post("/directory", data={"action": "sync"})
    assert m.Permission.query.count() == 4                                  # nothing removed by the read
    html = root.get("/directory").get_data(as_text=True)
    section = html.split("3 · No longer in the directory")[1]
    assert "Rae Diaz" in section and "Betty and Medgar" in section and "pat.lee@pathsco.com" in section
    assert "DCA" not in section.split("</table>")[0]                       # the hand-made grant is never listed
    pat = m.User.query.filter_by(email="pat.lee@pathsco.com").one()
    root.post("/directory", data={"action": "drop", "drop": [f"{rae.id}:{world['bm']}", f"{pat.id}:{world['berry']}",
                                                             f"{rae.id}:{world['dca']}"]})
    left = {(p.user_id, p.scope_id, p.source) for p in m.Permission.query.all()}
    assert left == {(rae.id, world["berry"], "directory"), (rae.id, world["dca"], None)}


def test_a_failed_read_keeps_what_was_read_and_says_so(root, world, session):
    _link_all(root, world)
    world["served"]["data"] = RuntimeError("the violations portal refused the token (401) — check DIRECTORY_TOKEN")
    root.post("/directory", data={"action": "sync"})
    html = root.get("/directory").get_data(as_text=True)
    assert "Could not read the violations portal" in html and "refused the token" in html
    assert "Berry (NYCHA)" in html and len(_rows()) == 5


def test_not_connected_yet_explains_what_to_set(app, root, world):
    app.config["DIRECTORY_TOKEN"] = ""
    html = root.get("/directory").get_data(as_text=True)
    assert "Not connected yet" in html and "DIRECTORY_TOKEN" in html and world["served"]["calls"] == 0
