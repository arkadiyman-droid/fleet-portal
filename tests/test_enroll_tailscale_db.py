"""
tests/test_enroll_tailscale_db.py — an adopted new module joins the fleet's tailnet

Database tier (Postgres). With Tailscale settings the portal mints a one-time
key for each module it adopts as a new site, hands it over through the
module's authenticated check-in, and polls the module at the Tailscale
address it reports. Tailscale itself is stubbed here (app/tailscale_api.py
has its own tests); the EMS side is tests/test_fleet_enroll.py.
"""
import secrets
import datetime as dt

import pytest
from sqlalchemy import text

from tests.conftest import requires_postgres
from tests.test_enroll_db import _as_superuser, _auth, _hello, _module, building, client  # noqa: F401 (fixtures)

pytestmark = requires_postgres

KEY = "tskey-auth-kTEST1234CNTRL-abcdefghijklmnop"


@pytest.fixture
def minted(app, monkeypatch):
    """Tailscale settings on, and a stand-in for Tailscale's key endpoint."""
    import app.tailscale_api as ts
    app.config.update(TAILSCALE_OAUTH_CLIENT_ID="kCLIENT", TAILSCALE_OAUTH_CLIENT_SECRET="tskey-client-x")
    calls = {"n": 0, "fail": None, "desc": []}

    def fake_mint(description):
        calls["n"] += 1
        calls["desc"].append(description)
        if calls["fail"]:
            raise ts.TailscaleError(calls["fail"])
        return KEY
    monkeypatch.setattr(ts, "mint_auth_key", fake_mint)
    return calls


def _assign(client, building, mod, name="Boiler Room #2"):
    _hello(client, mod)
    _as_superuser(client)
    client.post("/adopt", data={"code": mod["code"]})
    r = client.post(f"/adopt/{mod['id']}", data={"action": "assign", "building_id": building.id, "name": name})
    assert r.status_code == 302


def _status(client, mod):
    return client.post("/api/enroll/status", json=_auth(mod)).get_json()


class JoiningTheTailnet:

    def test_whole_flow_key_then_tailscale_address(self, client, building, minted):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        st = _status(client, mod)
        assert st["tailscale"] == {"key": KEY, "hostname": "boiler-room-2"}
        assert minted["n"] == 1 and "Boiler Room" in minted["desc"][0]
        assert _status(client, mod)["tailscale"]["key"] == KEY
        assert minted["n"] == 1                                   # one key per module, not per check-in

        # at rest the key is encrypted, like the polling tokens
        raw = m.db.session.execute(text("select ts_key from enrollments where id = :i"), {"i": mod["id"]}).scalar()
        assert raw and "tskey" not in raw

        tok = secrets.token_hex(32)
        r = client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": tok, "lanIp": "10.0.0.9",
                                                      "tailscaleIp": "100.88.1.2"})
        assert r.status_code == 200
        m.db.session.expire_all()
        row = m.Enrollment.query.get(mod["id"])
        assert row.state == "complete" and row.ts_key is None
        assert row.controller.tailscale_ip == "100.88.1.2"         # polled there from now on
        assert row.controller.tailscale_hostname == "boiler-room-2"
        page = client.get(f"/adopt/{mod['id']}")
        assert b"joined Tailscale as 100.88.1.2" in page.data

    def test_a_key_that_cannot_be_minted_keeps_the_module_waiting(self, client, building, minted):
        import app.models as m
        minted["fail"] = "Tailscale refused the portal's OAuth client: check its id and secret"
        mod = _module()
        _assign(client, building, mod)
        assert _status(client, mod)["tailscale"] == {"pending": True}
        assert _status(client, mod)["tailscale"] == {"pending": True}
        assert minted["n"] == 1                                   # not hammered every 15 seconds
        page = client.get(f"/adopt/{mod['id']}")
        assert b"could not get a Tailscale key" in page.data and b"check its id and secret" in page.data

        # fixed, and a minute later the next check-in gets a key
        minted["fail"] = None
        row = m.Enrollment.query.get(mod["id"])
        row.ts_key_made = dt.datetime.utcnow() - dt.timedelta(seconds=120)
        m.db.session.commit()
        assert _status(client, mod)["tailscale"]["key"] == KEY
        assert m.Enrollment.query.get(mod["id"]).ts_error is None

    def test_an_unused_key_is_replaced_before_it_expires(self, client, building, minted):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        _status(client, mod)
        row = m.Enrollment.query.get(mod["id"])
        row.ts_key_made = dt.datetime.utcnow() - dt.timedelta(seconds=3300)
        m.db.session.commit()
        _status(client, mod)
        assert minted["n"] == 2

    def test_without_tailscale_settings_nothing_changes(self, client, building):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        assert "tailscale" not in _status(client, mod)
        client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": secrets.token_hex(32), "lanIp": "10.0.0.9"})
        m.db.session.expire_all()
        assert m.Enrollment.query.get(mod["id"]).controller.tailscale_ip == "10.0.0.9"

    def test_a_restore_brings_its_own_tailscale(self, client, building, minted):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        row = m.Enrollment.query.get(mod["id"])
        row.mode = "restore"
        m.db.session.commit()
        assert "tailscale" not in _status(client, mod)
        assert minted["n"] == 0

    def test_a_bad_tailscale_address_is_ignored(self, client, building, minted):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        _status(client, mod)
        client.post("/api/enroll/complete", json={**_auth(mod), "fleetToken": secrets.token_hex(32),
                                                  "lanIp": "10.0.0.9", "tailscaleIp": "http://evil/"})
        m.db.session.expire_all()
        assert m.Enrollment.query.get(mod["id"]).controller.tailscale_ip == "10.0.0.9"

    def test_cancel_forgets_the_key(self, client, building, minted):
        import app.models as m
        mod = _module()
        _assign(client, building, mod)
        _status(client, mod)
        client.post(f"/adopt/{mod['id']}", data={"action": "cancel"})
        m.db.session.expire_all()
        row = m.Enrollment.query.get(mod["id"])
        assert row.state == "waiting" and row.ts_key is None and row.ts_key_made is None
