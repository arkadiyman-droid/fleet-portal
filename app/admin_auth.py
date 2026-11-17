from urllib.parse import urlparse

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from sqlalchemy import func
from werkzeug.security import check_password_hash

from app.models import User
from app.permissions import load_identity

auth_bp = Blueprint("auth", __name__)

# The enroll.* endpoints are where a blank controller calls home; a machine
# calls them, so no login. Their own secret check stands in (enroll_api.py).
_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "auth.email_login", "static", "admin.stripe_webhook",
                     "enroll.hello", "enroll.status", "enroll.complete", "enroll.bundle"}

# The two requests that may be POSTed while the administrator is looking at the
# portal as someone else: stop, and switch to another person.
_VIEW_AS_ENDPOINTS = {"auth.view_as", "auth.view_as_stop"}

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
            if (getattr(g, "viewing_as", False) and request.method not in ("GET", "HEAD", "OPTIONS")
                    and request.endpoint not in _VIEW_AS_ENDPOINTS):
                # Looking as someone else is looking only: nothing is saved or sent under their name.
                flash(f"You are seeing the portal as {g.user.name}. Nothing can be changed from here — "
                      "go back to the administrator first.", "error")
                return redirect(request.referrer or url_for("admin.index"), code=303)
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


# ─── "See as": the administrator looks at the portal as one of the users ───
#
# The surest way to know what a person has been given is to look at it. The
# administrator's own session stays the administrator's (session["superuser"]);
# session["view_as"] names the account being looked through, and
# permissions.load_identity turns each request into that person's. No password
# or email code is involved, nothing is recorded against the other account, and
# _require_login refuses every request that would change anything.

@auth_bp.route("/view-as/<int:user_id>", methods=["POST"])
def view_as(user_id):
    if not session.get("superuser"):
        abort(403)
    user = User.query.get_or_404(user_id)
    if not user.is_active:
        flash(f"{user.name} is archived, so there is nothing to see as them.", "error")
        return redirect(url_for("admin.list_users"))
    session["view_as"] = user.id
    return redirect(url_for("admin.index"))


@auth_bp.route("/view-as/stop", methods=["POST"])
def view_as_stop():
    seen = session.pop("view_as", None)
    if session.get("superuser") and seen is not None:
        return redirect(url_for("admin.user_permissions", user_id=seen))
    return redirect(url_for("admin.index"))
