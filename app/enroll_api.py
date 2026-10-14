"""
app/enroll_api.py — where a blank controller calls home (no login, JSON)

The other half of EMS use_controller/fleet/enroll.py. These three endpoints
are the only ones in the portal reachable without a login (admin_auth.py's
_EXEMPT_ENDPOINTS) and without a CSRF token, because a machine calls them.
What stands in for a login:

  * the module's own secret, sent on every call; only its sha256 is kept,
    and a different secret for a known id is refused -- nobody can take
    over another module's enrollment by knowing its id;
  * the pairing code is never returned by anything here; an operator types
    it from the module's own screen (routes.py /adopt);
  * nothing a module says is trusted for more than display until an
    operator adopts it; the token it finally hands over is stored only for
    the Controller row an operator created;
  * a per-address rate limit and a cap on waiting enrollments, so the open
    door cannot be used to fill the database.

With Tailscale settings (app/tailscale_api.py), /status also hands an
assigned new module a one-time auth key for the fleet's tailnet, and
/complete takes the Tailscale address it joined with; the poller uses that.

Behind Cloudflare Access (the beverly-pi pilot), /api/enroll/* must be given
a bypass policy, or modules can never reach it. See deploy/README.md.
"""
import hashlib
import hmac
import ipaddress
import re
import threading
import time
from datetime import datetime, timedelta

from flask import Blueprint, Response, jsonify, request

from app import tailscale_api
from app.extensions import csrf, db
from app.models import Controller, Enrollment

enroll_bp = Blueprint("enroll", __name__)

RATE_PER_MINUTE = 30          # per calling address; a module makes ~4/min
TS_RETRY_SECS = 60            # a failed key request is retried at most this often
TS_REFRESH_MARGIN = 600       # re-mint an unused key this long before it expires
MAX_WAITING = 500             # open door, bounded
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SECRET = re.compile(r"^[0-9a-f]{64}$")
_AGE = re.compile(r"^age1[a-z0-9]{58}$")

_rate_lock = threading.Lock()
_rate = {}                    # addr -> [window_start, count]


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def normalize_code(code: str) -> str:
    """What the operator typed, as the module generated it: case and the
    dash do not matter."""
    return re.sub(r"[^A-Z0-9]", "", (code or "").upper())


def code_hash(code: str) -> str:
    return sha256("echotherm-pair:" + normalize_code(code))


def _err(msg, status):
    return jsonify({"error": msg}), status


def _addr():
    # behind the tunnel the client address is in CF-Connecting-IP
    return request.headers.get("CF-Connecting-IP") or request.remote_addr or "?"


def _rate_ok(now=None) -> bool:
    now = time.monotonic() if now is None else now
    with _rate_lock:
        w = _rate.get(_addr())
        if not w or now - w[0] >= 60:
            _rate[_addr()] = [now, 1]
            return True
        w[1] += 1
        return w[1] <= RATE_PER_MINUTE


def _body():
    b = request.get_json(silent=True)
    return b if isinstance(b, dict) else None


def _auth(b):
    """The enrollment this call is about, if the secret matches. (row, error)"""
    eid, secret = str(b.get("id") or ""), str(b.get("secret") or "")
    if not _UUID.match(eid) or not _SECRET.match(secret):
        return None, _err("malformed id or secret", 400)
    row = db.session.get(Enrollment, eid)
    if row is None:
        return None, _err("unknown module: say hello first", 404)
    if not hmac.compare_digest(row.secret_hash, sha256(secret)):
        return None, _err("secret does not match", 403)
    return row, None


def _clip(v, n):
    return None if v is None else str(v)[:n]


def _ip(v):
    """A syntactically valid IP address, or None."""
    try:
        return str(ipaddress.ip_address(str(v or "").strip()))
    except ValueError:
        return None


def _tailscale_for(row):
    """What an assigned new module is told about joining the tailnet:
    {key, hostname} or {pending: true}. Mints a key when there is none, or
    the unused one is about to expire; a failure is kept for the adopt page
    and retried at most every TS_RETRY_SECS."""
    ctrl = row.controller
    now = datetime.utcnow()
    ttl = int(tailscale_api._cfg("TAILSCALE_KEY_TTL_SECONDS", 3600))
    made = row.ts_key_made
    fresh = row.ts_key and made and now - made < timedelta(seconds=max(60, ttl - TS_REFRESH_MARGIN))
    tried_lately = not row.ts_key and made and now - made < timedelta(seconds=TS_RETRY_SECS)
    if not fresh and not tried_lately and ctrl is not None:
        row.ts_key_made = now
        try:
            row.ts_key = tailscale_api.mint_auth_key(f"EchoTherm adopt {ctrl.name}")
            row.ts_error = None
        except tailscale_api.TailscaleError as e:
            row.ts_key, row.ts_error = None, str(e)[:300]
    if row.ts_key and ctrl is not None:
        return {"key": row.ts_key, "hostname": tailscale_api.node_name(ctrl.name, "echotherm-" + row.id[:8])}
    return {"pending": True}


def _assignment(row):
    c = row.controller
    if not c:
        return None
    b = c.building
    return {"controllerName": c.name, "buildingName": b.name if b else None}


@enroll_bp.route("/api/enroll/hello", methods=["POST"])
@csrf.exempt
def hello():
    if not _rate_ok():
        return _err("too many requests", 429)
    b = _body()
    if b is None:
        return _err("expected a JSON object", 400)
    eid, secret, code = str(b.get("id") or ""), str(b.get("secret") or ""), normalize_code(b.get("code"))
    if not _UUID.match(eid) or not _SECRET.match(secret) or len(code) != 6:
        return _err("malformed id, secret or code", 400)
    age = b.get("ageRecipient")
    if age is not None and not _AGE.match(str(age)):
        return _err("ageRecipient is not an age public key", 400)

    row = db.session.get(Enrollment, eid)
    if row is None:
        if Enrollment.query.filter_by(state="waiting").count() >= MAX_WAITING:
            return _err("too many modules waiting", 503)
        row = Enrollment(id=eid, secret_hash=sha256(secret), code_hash=code_hash(code), state="waiting")
        db.session.add(row)
    elif not hmac.compare_digest(row.secret_hash, sha256(secret)):
        return _err("secret does not match", 403)
    row.hostname = _clip(b.get("hostname"), 100)
    row.serial = _clip(b.get("serial"), 64)
    row.model = _clip(b.get("model"), 120)
    row.ems_version = _clip(b.get("emsVersion"), 60)
    row.lan_ip = _clip(b.get("lanIp"), 50)
    row.remote_ip = _clip(_addr(), 50)
    if age:
        row.age_recipient = age
    row.last_seen = datetime.utcnow()
    db.session.commit()
    return jsonify({"state": row.state})


@enroll_bp.route("/api/enroll/status", methods=["POST"])
@csrf.exempt
def status():
    if not _rate_ok():
        return _err("too many requests", 429)
    b = _body()
    if b is None:
        return _err("expected a JSON object", 400)
    row, err = _auth(b)
    if err:
        return err
    row.last_seen = datetime.utcnow()
    if "lastError" in b:                       # the module's own view, for the adopt page
        row.module_error = _clip(b.get("lastError"), 300)
    ts = None
    if row.state == "assigned" and row.mode != "restore" and tailscale_api.configured():
        ts = _tailscale_for(row)
    db.session.commit()
    out = {"state": row.state, "mode": row.mode}
    if ts is not None:
        out["tailscale"] = ts
    if row.state in ("assigned", "restoring", "complete"):
        out["assignment"] = _assignment(row)
        out["controllerId"] = row.controller_id
    if row.state == "assigned" and row.mode == "restore":
        out["bundleReady"] = row.bundle_ct is not None
    return jsonify(out)


@enroll_bp.route("/api/enroll/bundle", methods=["POST"])
@csrf.exempt
def bundle():
    """Restore mode: the site bundle, encrypted in the operator's browser to
    this module's own key. Handed over as long as the module has not yet
    said it is restoring (so a dropped download can be retried), then
    deleted by /complete."""
    if not _rate_ok():
        return _err("too many requests", 429)
    b = _body()
    if b is None:
        return _err("expected a JSON object", 400)
    row, err = _auth(b)
    if err:
        return err
    if row.state != "assigned" or row.mode != "restore" or row.bundle_ct is None:
        return _err("no bundle waiting for this module", 409)
    row.fetched_at = datetime.utcnow()
    row.last_seen = row.fetched_at
    db.session.commit()
    return Response(bytes(row.bundle_ct), mimetype="application/octet-stream")


@enroll_bp.route("/api/enroll/complete", methods=["POST"])
@csrf.exempt
def complete():
    if not _rate_ok():
        return _err("too many requests", 429)
    b = _body()
    if b is None:
        return _err("expected a JSON object", 400)
    row, err = _auth(b)
    if err:
        return err
    ctrl = row.controller
    if row.mode == "restore":
        # "I have the bundle and am rebooting into the site." No token: the
        # restored site brings its own, which the portal already holds for
        # this Controller. The portal marks it complete on the first good
        # poll (fleet_poller).
        if row.state == "restoring":
            return jsonify({"ok": True, "controllerId": row.controller_id})
        if row.state != "assigned" or ctrl is None or row.fetched_at is None:
            return _err("not assigned, or the bundle was never fetched", 409)
        row.state = "restoring"
        row.restoring_at = datetime.utcnow()
        row.bundle_ct = None                    # the module has it; the portal keeps no copy
        row.last_seen = row.restoring_at
        db.session.commit()
        return jsonify({"ok": True, "controllerId": ctrl.id})
    tok = str(b.get("fleetToken") or "")
    if not _SECRET.match(tok):
        return _err("malformed fleetToken", 400)
    if row.state == "complete":
        # A retry after a lost reply: fine if it is the same token, never a swap.
        if ctrl and ctrl.fleet_api_token and hmac.compare_digest(ctrl.fleet_api_token, tok):
            return jsonify({"ok": True, "controllerId": ctrl.id})
        return _err("already completed with a different token", 409)
    if row.state != "assigned" or ctrl is None:
        return _err("not assigned to a controller", 409)
    ctrl.fleet_api_token = tok
    ts_ip = _ip(b.get("tailscaleIp"))
    if ts_ip:
        # it joined the fleet's tailnet: poll it there, wherever it is
        ctrl.tailscale_ip = ts_ip
        ctrl.tailscale_hostname = tailscale_api.node_name(ctrl.name, "echotherm-" + row.id[:8])
    else:
        lan = _clip(b.get("lanIp"), 50) or row.lan_ip
        if lan and not ctrl.tailscale_ip:
            ctrl.tailscale_ip = lan
    row.ts_key = None                           # single use, and used (or not needed)
    row.state = "complete"
    row.completed_at = datetime.utcnow()
    row.last_seen = row.completed_at
    db.session.commit()
    return jsonify({"ok": True, "controllerId": ctrl.id})
