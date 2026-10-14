"""
tests/test_tailscale_api.py — the portal's calls to Tailscale (no database)

Tailscale's HTTP API is stubbed at requests.post; what is checked is what
the portal asks for (a single-use, pre-approved, tagged key) and that every
failure comes back as one plain sentence for the adopt page.
"""
from unittest import mock

import pytest
import flask
import requests

import app.tailscale_api as ts


def Resp(status, body=None):
    """A stand-in for a requests.Response (a function: pytest collects classes)."""
    def _json():
        if body is None:
            raise ValueError("no json")
        return body
    return mock.Mock(status_code=status, json=_json)


@pytest.fixture
def ctx():
    a = flask.Flask("t")
    a.config.update(TAILSCALE_OAUTH_CLIENT_ID="kCLIENT", TAILSCALE_OAUTH_CLIENT_SECRET="tskey-client-secret",
                    TAILSCALE_TAGS="tag:controller", TAILSCALE_KEY_TTL_SECONDS=3600)
    ts.forget_token()
    with a.app_context():
        yield a
    ts.forget_token()


def _api(*responses):
    calls = []
    it = iter(responses)

    def post(url, **kw):
        calls.append((url, kw))
        r = next(it)
        if isinstance(r, Exception):
            raise r
        return r
    return calls, mock.patch.object(ts.requests, "post", side_effect=post)


TOKEN = Resp(200, {"access_token": "tskey-api-AT", "expires_in": 3600})


def test_asks_for_a_single_use_preapproved_tagged_key(ctx):
    calls, p = _api(TOKEN, Resp(200, {"key": "tskey-auth-kABC-123", "id": "kABC"}))
    with p:
        assert ts.mint_auth_key("EchoTherm adopt Boiler #2 (rear)") == "tskey-auth-kABC-123"
    (u1, k1), (u2, k2) = calls
    assert u1.endswith("/api/v2/oauth/token") and k1["data"]["grant_type"] == "client_credentials"
    assert u2.endswith("/api/v2/tailnet/-/keys")
    assert k2["headers"]["Authorization"] == "Bearer tskey-api-AT"
    create = k2["json"]["capabilities"]["devices"]["create"]
    assert create == {"reusable": False, "ephemeral": False, "preauthorized": True, "tags": ["tag:controller"]}
    assert k2["json"]["expirySeconds"] == 3600
    assert k2["json"]["description"] == "EchoTherm adopt Boiler 2 rear"      # only what Tailscale accepts


def test_signs_in_once_and_reuses_it(ctx):
    calls, p = _api(TOKEN, Resp(200, {"key": "tskey-auth-k1"}), Resp(200, {"key": "tskey-auth-k2"}))
    with p:
        ts.mint_auth_key("a"); ts.mint_auth_key("b")
    assert [u.rsplit("/", 1)[-1] for u, _ in calls] == ["token", "keys", "keys"]


def test_wrong_client_secret_is_one_plain_sentence(ctx):
    _, p = _api(Resp(401, {"message": "invalid client"}))
    with p, pytest.raises(ts.TailscaleError, match="check its id and secret"):
        ts.mint_auth_key("x")


def test_tag_not_allowed_says_what_to_fix(ctx):
    _, p = _api(TOKEN, Resp(400, {"message": "requested tags [tag:controller] are invalid or not permitted"}))
    with p, pytest.raises(ts.TailscaleError, match="tagOwners"):
        ts.mint_auth_key("x")


def test_unreachable_tailscale(ctx):
    _, p = _api(requests.ConnectionError("down"))
    with p, pytest.raises(ts.TailscaleError, match="could not reach Tailscale"):
        ts.mint_auth_key("x")


def test_an_expired_sign_in_is_dropped_and_redone_next_time(ctx):
    calls, p = _api(TOKEN, Resp(401, {}), TOKEN, Resp(200, {"key": "tskey-auth-k3"}))
    with p:
        with pytest.raises(ts.TailscaleError):
            ts.mint_auth_key("x")
        assert ts.mint_auth_key("x") == "tskey-auth-k3"
    assert [u.rsplit("/", 1)[-1] for u, _ in calls] == ["token", "keys", "token", "keys"]


def test_configured_only_with_both_halves(ctx):
    assert ts.configured()
    ctx.config["TAILSCALE_OAUTH_CLIENT_SECRET"] = ""
    assert not ts.configured()


def test_node_names(ctx):
    assert ts.node_name("Boiler Room #2") == "boiler-room-2"
    assert ts.node_name("  ---  ", "echotherm-ab12") == "echotherm-ab12"
    assert len(ts.node_name("x" * 100)) == 63
