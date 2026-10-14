"""
app/tailscale_api.py — one-time Tailscale keys for modules being adopted

When an operator adopts a blank module as a NEW site, the portal asks
Tailscale for an auth key made for that module alone, and hands it to the
module through its authenticated check-in (app/enroll_api.py). The module
joins the fleet's tailnet with it (EMS tools/tailscale_join.sh) and reports
its Tailscale address, which is what the poller uses from then on --
wherever the building is. Nobody signs a module in through a browser, and
nobody turns key expiry off by hand: the keys are

  * single use and pre-approved (the device joins without a click),
  * tagged (TAILSCALE_TAGS, default tag:controller) -- a tagged device is
    owned by the tag, not by a person, and its node key does not expire,
  * short-lived (TAILSCALE_KEY_TTL_SECONDS, default one hour) if unused.

The portal authenticates to Tailscale with an OAuth client (Tailscale admin
console -> Settings -> OAuth clients) that may only write auth keys for
those tags. Its id and secret live in .env, set with
deploy/set-tailscale-oauth.sh. Without them the feature is off and adoption
works as before (the portal polls the module's LAN address).
"""
import re
import threading
import time

import requests
from flask import current_app

TIMEOUT = 10
_lock = threading.Lock()
_token = {"value": None, "until": 0.0, "client": None}


class TailscaleError(RuntimeError):
    """A plain-language reason the portal could not get a key."""


def _cfg(name, default=None):
    return current_app.config.get(name, default)


def configured() -> bool:
    return bool(_cfg("TAILSCALE_OAUTH_CLIENT_ID") and _cfg("TAILSCALE_OAUTH_CLIENT_SECRET"))


def tags() -> list:
    return [t.strip() for t in (_cfg("TAILSCALE_TAGS") or "tag:controller").split(",") if t.strip()]


def node_name(controller_name: str, fallback: str = "echotherm") -> str:
    """A Tailscale machine name from the controller's name: lowercase
    letters, digits and dashes, at most 63 characters."""
    s = re.sub(r"[^a-z0-9]+", "-", (controller_name or "").lower()).strip("-")[:63].strip("-")
    return s or fallback


def _api() -> str:
    return (_cfg("TAILSCALE_API_URL") or "https://api.tailscale.com").rstrip("/")


def _access_token() -> str:
    cid = _cfg("TAILSCALE_OAUTH_CLIENT_ID")
    with _lock:
        if _token["value"] and _token["client"] == cid and time.monotonic() < _token["until"]:
            return _token["value"]
    try:
        r = requests.post(_api() + "/api/v2/oauth/token", timeout=TIMEOUT, data={
            "client_id": cid, "client_secret": _cfg("TAILSCALE_OAUTH_CLIENT_SECRET"),
            "grant_type": "client_credentials"})
    except requests.RequestException as e:
        raise TailscaleError(f"could not reach Tailscale ({type(e).__name__})")
    if r.status_code in (400, 401, 403):
        raise TailscaleError("Tailscale refused the portal's OAuth client: check its id and secret")
    if r.status_code != 200:
        raise TailscaleError(f"Tailscale answered {r.status_code} when the portal signed in")
    body = r.json()
    with _lock:
        _token.update(value=body["access_token"], client=cid,
                      until=time.monotonic() + max(60, int(body.get("expires_in", 3600)) - 120))
    return body["access_token"]


def forget_token() -> None:
    with _lock:
        _token.update(value=None, until=0.0, client=None)


def mint_auth_key(description: str) -> str:
    """A single-use, pre-approved, tagged auth key. Raises TailscaleError."""
    desc = re.sub(r"[^A-Za-z0-9 _-]", "", description or "")[:50] or "EchoTherm adoption"
    body = {"capabilities": {"devices": {"create": {
                "reusable": False, "ephemeral": False, "preauthorized": True, "tags": tags()}}},
            "expirySeconds": int(_cfg("TAILSCALE_KEY_TTL_SECONDS", 3600)),
            "description": desc}
    try:
        r = requests.post(_api() + "/api/v2/tailnet/-/keys", json=body, timeout=TIMEOUT,
                          headers={"Authorization": "Bearer " + _access_token()})
    except requests.RequestException as e:
        raise TailscaleError(f"could not reach Tailscale ({type(e).__name__})")
    if r.status_code == 401:
        forget_token()                        # signed out under us: sign in again next time
        raise TailscaleError("Tailscale no longer accepts the portal's sign-in; it retries")
    if r.status_code in (400, 403):
        try:
            msg = (r.json() or {}).get("message") or ""
        except ValueError:
            msg = ""
        if "tag" in msg.lower():
            raise TailscaleError(f"Tailscale will not issue keys for {', '.join(tags())}: "
                                 "the OAuth client must be allowed that tag, and the tailnet policy "
                                 "must list it under tagOwners")
        raise TailscaleError("Tailscale refused to make a key" + (f": {msg[:150]}" if msg else ""))
    if r.status_code != 200:
        raise TailscaleError(f"Tailscale answered {r.status_code} when asked for a key")
    key = (r.json() or {}).get("key") or ""
    if not key.startswith("tskey-auth-"):
        raise TailscaleError("Tailscale's answer had no auth key in it")
    return key
