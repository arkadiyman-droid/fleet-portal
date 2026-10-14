"""
tests/test_enroll_db.py — a blank module calls home and an operator adopts it

Database tier (Postgres). The other half is EMS tests/test_fleet_enroll.py.

The /api/enroll/* endpoints are the only ones open without a login, so most
of these are about what an open door must not allow: storing the secret or
the code, a stranger taking over a module's enrollment, opening a module's
adopt page without having read its code off the screen, swapping the token
once handed over, a viewer adopting, and flooding.
"""
import re
import secrets
import uuid

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

CODE = "K7PQ4X"


def _module(**over):
    m = {"id": str(uuid.uuid4()), "secret": secrets.token_hex(32), "code": CODE, "hostname": "echotherm-new",
         "serial": "10000000abcdef", "model": "Raspberry Pi Compute Module 5", "emsVersion": "3.1.0",
         "lanIp": "10.0.0.9", "ageRecipient": "age1" + "q" * 58}
    m.update(over)
    return m


@pytest.fixture
def client(app):
    import app.enroll_api as api
    api._rate.clear()
    app.config["WTF_CSRF_ENABLED"] = False
    return app.test_client()


@pytest.fixture
def building(session):
    import app.models as m
    c = m.Client(name="Acme"); session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Main St"); session.add(p); session.flush()
    b = m.Building(property_id=p.id, name="12 Main St"); session.add(b); session.commit()
    return b


def _as_superuser(client):
    with client.session_transaction() as s:
        s["superuser"] = True


def _hello(client, mod):
    return client.post("/api/enroll/hello", json=mod)


def _auth(mod):
    return {"id": mod["id"], "secret": mod["secret"]}


class TheOpenDoor:

    def test_hello_stores_only_hashes(self, client):
        import app.models as m
        mod = _module()
        r = _hello(client, mod)
        assert r.status_code == 200 and r.get_json() == {"state": "waiting"}
        row = m.Enrollment.query.get(mod["id"])
        assert row.secret_hash != mod["secret"] and len(row.secret_hash) == 64
        assert CODE not in row.code_hash
        assert row.hostname == "echotherm-new" and row.age_recipient.startswith("age1")

    def test_a_stranger_cannot_take_over_a_known_id(self, client):
        mod = _module()
        _hello(client, mod)
        thief = dict(mod, secret=secrets.token_hex(32), hostname="evil")
        assert _hello(client, thief).status_code == 403
        assert client.post("/api/enroll/status", json=_auth(thief)).status_code == 403

    def test_malformed_input_is_refused(self, client):
        assert _hello(client, _module(id="not-a-uuid")).status_code == 400
        assert _hello(client, _module(code="12")).status_code == 400
        assert _hello(client, _module(ageRecipient="ssh-ed25519 AAAA")).status_code == 400
        assert client.post("/api/enroll/hello", data="nope", content_type="text/plain").status_code == 400

    def test_unknown_module_status_is_404(self, client):
        assert client.post("/api/enroll/status", json=_auth(_module())).status_code == 404

    def test_rate_limited(self, client):
        mod = _module()
        codes = [_hello(client, mod).status_code for _ in range(35)]
        assert 429 in codes and codes[0] == 200

    def test_no_login_needed_only_for_these(self, client):
        assert client.post("/api/enroll/status", json=_auth(_module())).status_code == 404   # not a login redirect
        assert client.get("/adopt").status_code == 302                                       # the UI still is


class Adopting:

    def test_whole_flow_new_controller(self, client, building):
        import app.models as m
        mod = _module()
        _hello(client, mod)
        _as_superuser(client)

        # 1. the code (any case, with the dash) finds it and opens its page
        r = client.post("/adopt", data={"code": "k7p-q4x"})
        assert r.status_code == 302 and r.headers["Location"].endswith(f"/adopt/{mod['id']}")
        page = client.get(f"/adopt/{mod['id']}")
        assert page.status_code == 200 and b"Where does it go?" in page.data

        # 2. assign it
        r = client.post(f"/adopt/{mod['id']}", data={"action": "assign", "building_id": building.id, "name": "boiler room"})
        assert r.status_code == 302
        row = m.Enrollment.query.get(mod["id"])
        assert row.state == "assigned" and row.controller.name == "boiler room"
        assert row.controller.fleet_api_token is None and row.controller.tailscale_ip == "10.0.0.9"

        # 3. the module learns it and hands over its token
        st = client.post("/api/enroll/status", json=_auth(mod)).get_json()
        assert st["state"] == "assigned"
        assert st["assignment"] == {"controllerName": "boiler room", "buildingName": "12 Main St"}
        tok = secrets.token_hex(32)
        r = client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": tok, "lanIp": "10.0.0.9"})
        assert r.status_code == 200 and r.get_json()["controllerId"] == row.controller_id
        m.db.session.expire_all()
        ctrl = m.Controller.query.get(row.controller_id)
        assert ctrl.fleet_api_token == tok                          # decrypts back to what the module minted
        assert m.Enrollment.query.get(mod["id"]).state == "complete"

        # a retry with the SAME token is fine (lost reply); a different one never
        assert client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": tok}).status_code == 200
        assert client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": secrets.token_hex(32)}).status_code == 409
        assert client.post("/api/enroll/status", json=_auth(mod)).get_json()["state"] == "complete"

    def test_complete_before_assignment_is_refused(self, client):
        mod = _module()
        _hello(client, mod)
        r = client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": secrets.token_hex(32)})
        assert r.status_code == 409

    def test_page_does_not_open_without_the_code(self, client):
        mod = _module()
        _hello(client, mod)
        _as_superuser(client)
        r = client.get(f"/adopt/{mod['id']}")
        assert r.status_code == 302 and r.headers["Location"].endswith("/adopt")

    def test_wrong_code_finds_nothing(self, client):
        _hello(client, _module())
        _as_superuser(client)
        r = client.post("/adopt", data={"code": "AAA-AAA"}, follow_redirects=True)
        assert b"No module waiting to be adopted shows that code" in r.data

    def test_the_code_never_appears_in_the_portal(self, client):
        mod = _module()
        _hello(client, mod)
        _as_superuser(client)
        listing = client.get("/adopt").data.decode()
        assert "echotherm-new" in listing
        assert not re.search(r"K7P-?Q4X", listing, re.I)

    def test_a_viewer_cannot_adopt(self, client, building, session):
        import app.models as m
        from werkzeug.security import generate_password_hash
        u = m.User(name="V", email="v@x", login="v", password_hash=generate_password_hash("x"))
        session.add(u); session.flush()
        session.add(m.Permission(user_id=u.id, scope_type="building", scope_id=building.id, role="viewer"))
        session.commit()
        with client.session_transaction() as s:
            s["user_id"] = u.id
        assert client.get("/adopt").status_code == 403

    def test_dismissed_module_leaves_the_list(self, client):
        import app.models as m
        mod = _module()
        _hello(client, mod)
        _as_superuser(client)
        client.post("/adopt", data={"code": CODE})
        client.post(f"/adopt/{mod['id']}", data={"action": "dismiss"})
        assert m.Enrollment.query.get(mod["id"]).state == "rejected"
        assert "echotherm-new" not in client.get("/adopt").data.decode()
