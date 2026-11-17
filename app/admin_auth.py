from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from sqlalchemy import func
from werkzeug.security import check_password_hash

from app.models import User
from app.permissions import load_identity

auth_bp = Blueprint("auth", __name__)

# The enroll.* endpoints are where a blank controller calls home; a machine
# calls them, so no login. Their own secret check stands in (enroll_api.py).
_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "auth.email_login", "static", "admin.stripe_webhook",
                     "enroll.hello", "enroll.status", "enroll.complete", "enroll.bundle"}

# A directory-made account has no password of its own: this never matches
# anything check_password_hash accepts, so the password form cannot open it.
NO_PASSWORD = "!email-sign-in-only"


def _safe_next(target: str) -> str:
    """Only allow same-origin relative redirects after login — guards against open redirects."""
    if not target:
        return url_for("admin.index")              # lands each person on their own start page
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("admin.index")
    return target


def cf_email():
    """The email Cloudflare Access has verified for this request, or None —
    also None when sign-in by company email is not configured (app/cf_access.py)."""
    cfg = current_app.config
    team, aud = cfg.get("CF_ACCESS_TEAM_DOMAIN"), cfg.get("CF_ACCESS_AUD")
    if not team or not aud:
        return None
    from app import cf_access
    return cf_access.identify(request.headers.get("Cf-Access-Jwt-Assertion"), team, aud)


def _user_for(email):
    if not email:
        return None
    return User.query.filter(func.lower(User.email) == email, User.is_active.is_(True)).first()


@auth_bp.before_app_request
def _require_login():
    if request.endpoint in _EXEMPT_ENDPOINTS:
        return None
    if session.get("superuser") or "user_id" in session:
        if load_identity():
            return None
        # session referenced a deleted/deactivated DB user — drop it and
        # fall through rather than leave it dangling.
        session.clear()
    # Sign-in by company email: Cloudflare Access has already verified who this
    # is; if an active account uses that address, that is who they are here.
    # Skipped after an explicit Log out, until they choose to come back in.
    if not session.get("email_signin_off"):
        user = _user_for(cf_email())
        if user is not None:
            session.clear()
            session["user_id"], session["via_email"] = user.id, True
            session.permanent = True
            if load_identity():
                return None
    return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == current_app.config["ADMIN_USERNAME"] and check_password_hash(
            current_app.config["ADMIN_PASSWORD_HASH"], password
        ):
            session.clear()
            session["superuser"] = True
            session.permanent = True
            return redirect(_safe_next(request.form.get("next", "")))
        user = User.query.filter_by(login=username, is_active=True).first()
        if user is not None and check_password_hash(user.password_hash, password):
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            return redirect(_safe_next(request.form.get("next", "")))
        flash("Invalid username or password.", "error")
    email = cf_email()
    return render_template("login.html", next=request.args.get("next", ""), cf_email=email,
                           cf_known=_user_for(email) is not None)


@auth_bp.route("/login/email")
def email_login():
    """Come back in by company email after a Log out."""
    session.pop("email_signin_off", None)
    return redirect(url_for("admin.index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    by_email = cf_email() is not None
    session.clear()
    if by_email:
        session["email_signin_off"] = True          # or the very next page would sign them straight back in
    return redirect(url_for("auth.login"))
