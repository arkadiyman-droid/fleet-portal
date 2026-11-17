"""
app/cf_access.py — who Cloudflare Access says is at the keyboard.

The portal sits behind Cloudflare Access: before a request reaches it, the
person has already proved their email address to Cloudflare (a one-time code
to their company mailbox). Cloudflare passes that on as a signed token in the
`Cf-Access-Jwt-Assertion` header. This module checks the signature and the
claims and returns the email — so a manager needs no second password here
(app/admin_auth.py signs them in as the account with that email).

The token is verified, not trusted: an RS256 signature against the team's
published keys, the audience tag of THIS application, the issuer, and the
expiry. A plain `Cf-Access-Authenticated-User-Email` header is never used —
anything on the box could set it.

No JWT library: the checks are few and `cryptography` is already a
dependency (app/crypto_types.py).
"""
import base64
import json
import logging
import time

import requests
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

log = logging.getLogger(__name__)

KEY_MAX_AGE = 3600            # re-read the team's keys hourly; they rotate every few weeks
KEY_RETRY = 60                # after a failed read, or an unknown key id, do not hammer
_cache = {"team": None, "at": 0.0, "keys": {}}


def _b64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _int(s: str) -> int:
    return int.from_bytes(_b64(s), "big")


def parse_keys(doc) -> dict:
    """{key id: RSA public key} from the team's /cdn-cgi/access/certs document."""
    out = {}
    for k in (doc or {}).get("keys") or []:
        try:
            if k.get("kty") == "RSA" and k.get("kid"):
                out[k["kid"]] = rsa.RSAPublicNumbers(_int(k["e"]), _int(k["n"])).public_key()
        except Exception:
            continue
    return out


def fetch_keys(team: str, timeout=10) -> dict:
    r = requests.get(f"https://{team}/cdn-cgi/access/certs", timeout=timeout)
    r.raise_for_status()
    return parse_keys(r.json())


def keys_for(team: str, kid=None, now=None, fetch=fetch_keys) -> dict:
    """The team's keys, cached. Re-read when stale, or (at most once a minute)
    when a token names a key we have not seen — that is how a rotation looks."""
    now = time.time() if now is None else now
    age = now - _cache["at"]
    stale = _cache["team"] != team or age > KEY_MAX_AGE
    unknown = kid is not None and kid not in _cache["keys"] and age > KEY_RETRY
    if stale or unknown:
        try:
            _cache.update(team=team, at=now, keys=fetch(team))
        except Exception as e:
            log.warning("Cloudflare Access keys could not be read: %s", e)
            if _cache["team"] != team:
                _cache.update(team=team, at=now, keys={})
            else:
                _cache["at"] = now - KEY_MAX_AGE + KEY_RETRY      # keep the old keys, try again in a minute
    return _cache["keys"]


def verify(token, team: str, aud: str, keys: dict, now=None):
    """The email in a Cloudflare Access token, lower-cased — or None when the
    token is missing, malformed, signed by someone else, for another
    application, from another team, expired, or carries no email (a service
    token). Never raises."""
    try:
        h64, p64, s64 = str(token or "").split(".")
        header, claims = json.loads(_b64(h64)), json.loads(_b64(p64))
        if header.get("alg") != "RS256":
            return None
        key = keys.get(header.get("kid"))
        if key is None:
            return None
        key.verify(_b64(s64), f"{h64}.{p64}".encode(), padding.PKCS1v15(), hashes.SHA256())
        now = time.time() if now is None else now
        audience = claims.get("aud")
        audience = audience if isinstance(audience, list) else [audience]
        if aud not in audience:
            return None
        if claims.get("iss") != f"https://{team}":
            return None
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now:
            return None
        if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + 60:
            return None
        email = str(claims.get("email") or "").strip().lower()
        return email if "@" in email else None
    except (ValueError, KeyError, TypeError, InvalidSignature):
        return None
    except Exception:
        return None


def identify(token, team: str, aud: str):
    """verify() with the team's cached keys; None when sign-in by email is not
    configured (no team or audience) or the token does not check out."""
    if not token or not team or not aud:
        return None
    try:
        kid = json.loads(_b64(str(token).split(".")[0])).get("kid")
    except Exception:
        return None
    return verify(token, team, aud, keys_for(team, kid))
