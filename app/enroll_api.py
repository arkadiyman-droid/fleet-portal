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

Behind Cloudflare Access (the beverly-pi pilot), /api/enroll/* must be given
a bypass policy, or modules can never reach it. See deploy/README.md.
"""
import hashlib
import hmac
import re
import threading
import time
from datetime import datetime

from flask import Blueprint, jsonify, request

from app.extensions import csrf, db
from app.models import Controller, Enrollment

enroll_bp = Blueprint("enroll", __name__)

RATE_PER_MINUTE = 30          # per calling address; a module makes ~4/min
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
    db.session.commit()
    out = {"state": row.state}
    if row.state in ("assigned", "complete"):
        out["assignment"] = _assignment(row)
        out["controllerId"] = row.controller_id
    return jsonify(out)


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
    tok = str(b.get("fleetToken") or "")
    if not _SECRET.match(tok):
        return _err("malformed fleetToken", 400)
    ctrl = row.controller
    if row.state == "complete":
        # A retry after a lost reply: fine if it is the same token, never a swap.
        if ctrl and ctrl.fleet_api_token and hmac.compare_digest(ctrl.fleet_api_token, tok):
            return jsonify({"ok": True, "controllerId": ctrl.id})
        return _err("already completed with a different token", 409)
    if row.state != "assigned" or ctrl is None:
        return _err("not assigned to a controller", 409)
    ctrl.fleet_api_token = tok
    lan = _clip(b.get("lanIp"), 50) or row.lan_ip
    if lan and not ctrl.tailscale_ip:
        ctrl.tailscale_ip = lan
    row.state = "complete"
    row.completed_at = datetime.utcnow()
    row.last_seen = row.completed_at
    db.session.commit()
    return jsonify({"ok": True, "controllerId": ctrl.id})
