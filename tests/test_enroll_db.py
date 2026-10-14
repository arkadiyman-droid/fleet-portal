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
        assert page.status_code == 200 and b"What is this module?" in page.data

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


CT = b"age-encryption.org/v1\n-> X25519 c2hhcmU\nYm9keQ\n--- bWFj\n" + bytes(80)   # shape only; the portal never decrypts


@pytest.fixture
def site(building, session):
    """An existing controller -- the site being replaced -- with its token."""
    import app.models as m
    c = m.Controller(building_id=building.id, name="dead box", tailscale_ip="100.64.0.7", fleet_api_token="t" * 64)
    session.add(c); session.commit()
    return c


def _open(client, mod):
    _hello(client, mod)
    _as_superuser(client)
    client.post("/adopt", data={"code": CODE})


def _send(client, mod, site, data=CT):
    from io import BytesIO
    return client.post(f"/adopt/{mod['id']}/restore",
                       data={"controller_id": site.id, "site": "usecontroller", "made": "2026-10-13T15:37:33Z",
                             "bundle": (BytesIO(data), "bundle.age")}, content_type="multipart/form-data")


class Restoring:

    def test_whole_restore_flow(self, app, client, site):
        import app.models as m
        from unittest import mock
        from app.fleet_poller import poll_controller
        mod = _module()
        _open(client, mod)

        r = _send(client, mod, site)
        assert r.status_code == 200 and r.get_json()["ok"]
        row = m.Enrollment.query.get(mod["id"])
        assert row.mode == "restore" and row.state == "assigned" and row.controller_id == site.id
        assert bytes(row.bundle_ct) == CT and row.bundle_site == "usecontroller"

        # the site reads "being restored", not red, while it is dark
        from app import fleet_health
        import datetime as dt, types
        st = types.SimpleNamespace(ok=False, error="timed out", http_status=None, version=None, building_name=None,
                                   polled_at=dt.datetime.utcnow(), last_ok_at=dt.datetime.utcnow() - dt.timedelta(hours=5), raw_json=None)
        ev = fleet_health.evaluate(m.Controller.query.get(site.id), st)
        assert ev["level"] == "info" and "Being restored" in ev["issues"][0][1]

        s = client.post("/api/enroll/status", json=_auth(mod)).get_json()
        assert s["state"] == "assigned" and s["mode"] == "restore" and s["bundleReady"] is True

        # "restoring" before the module even fetched it is refused
        assert client.post("/api/enroll/complete", json={**_auth(mod), "mode": "restore"}).status_code == 409
        b = client.post("/api/enroll/bundle", json=_auth(mod))
        assert b.status_code == 200 and b.data == CT
        assert client.post("/api/enroll/bundle", json=_auth(mod)).data == CT      # a dropped download can retry
        assert client.post("/api/enroll/complete", json={**_auth(mod), "mode": "restore"}).status_code == 200
        m.db.session.expire_all()
        row = m.Enrollment.query.get(mod["id"])
        assert row.state == "restoring" and row.bundle_ct is None                  # the portal keeps no copy
        assert client.post("/api/enroll/bundle", json=_auth(mod)).status_code == 409

        # the restored site answers its first poll with its own (old) token -> complete
        ok = types.SimpleNamespace(status_code=200, raise_for_status=lambda: None,
                                   json=lambda: {"buildingName": "12 Main St", "version": "3.1.0"})
        with mock.patch("app.fleet_poller.requests.get", return_value=ok) as g:
            poll_controller(app, m.Controller.query.get(site.id))
        assert g.call_args.kwargs["headers"]["Authorization"] == "Bearer " + "t" * 64
        m.db.session.expire_all()
        assert m.Enrollment.query.get(mod["id"]).state == "complete"

    def test_refuses_anything_but_one_recipient_age_ciphertext(self, client, site):
        mod = _module()
        _open(client, mod)
        assert _send(client, mod, site, b"PK\x03\x04 not age").status_code == 400
        two = b"age-encryption.org/v1\n-> X25519 a\nb\n-> X25519 c\nd\n--- m\n" + bytes(40)
        assert _send(client, mod, site, two).status_code == 400
        assert _send(client, mod, site, CT + bytes(2 * 1024 * 1024)).status_code == 400

    def test_not_without_the_code(self, client, site):
        mod = _module()
        _hello(client, mod)
        _as_superuser(client)
        assert _send(client, mod, site).status_code == 403

    def test_a_module_without_a_key_cannot_be_restored_onto(self, client, site):
        mod = _module(ageRecipient=None)
        _open(client, mod)
        r = _send(client, mod, site)
        assert r.status_code == 409 and "no public key" in r.get_json()["error"]

    def test_older_adoption_of_the_same_site_lets_go(self, client, site, session):
        import app.models as m
        old = _module(code="ACDEFG")
        _hello(client, old)
        session.query(m.Enrollment).filter_by(id=old["id"]).update({"state": "complete", "controller_id": site.id})
        session.commit()
        mod = _module()
        _open(client, mod)
        assert _send(client, mod, site).status_code == 200
        m.db.session.expire_all()
        assert m.Enrollment.query.get(old["id"]).controller_id is None
        assert m.Controller.query.get(site.id).enrollment.id == mod["id"]


class Cancelling:

    def test_cancel_a_restore_before_the_module_acts(self, client, site):
        import app.models as m
        mod = _module()
        _open(client, mod)
        _send(client, mod, site)
        client.post(f"/adopt/{mod['id']}", data={"action": "cancel"})
        m.db.session.expire_all()
        row = m.Enrollment.query.get(mod["id"])
        assert row.state == "waiting" and row.mode == "new" and row.bundle_ct is None and row.controller_id is None
        assert m.Controller.query.get(site.id) is not None            # the real site is untouched

    def test_cancel_a_new_adoption_removes_the_empty_record(self, client, building):
        import app.models as m
        mod = _module()
        _open(client, mod)
        client.post(f"/adopt/{mod['id']}", data={"action": "assign", "building_id": building.id, "name": "oops"})
        cid = m.Enrollment.query.get(mod["id"]).controller_id
        client.post(f"/adopt/{mod['id']}", data={"action": "cancel"})
        m.db.session.expire_all()
        assert m.Controller.query.get(cid) is None
        assert m.Enrollment.query.get(mod["id"]).state == "waiting"

    def test_too_late_once_the_module_has_acted(self, client, site):
        import app.models as m
        mod = _module()
        _open(client, mod)
        _send(client, mod, site)
        client.post("/api/enroll/bundle", json=_auth(mod))
        client.post("/api/enroll/complete", json={**_auth(mod), "mode": "restore"})
        r = client.post(f"/adopt/{mod['id']}", data={"action": "cancel"}, follow_redirects=True)
        assert b"Too late to cancel" in r.data
        assert m.Enrollment.query.get(mod["id"]).state == "restoring"
