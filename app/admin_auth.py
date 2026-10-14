from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

from app.models import User
from app.permissions import load_identity

auth_bp = Blueprint("auth", __name__)

# The enroll.* endpoints are where a blank controller calls home; a machine
# calls them, so no login. Their own secret check stands in (enroll_api.py).
_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "static", "admin.stripe_webhook",
                     "enroll.hello", "enroll.status", "enroll.complete"}


def _safe_next(target: str) -> str:
    """Only allow same-origin relative redirects after login — guards against open redirects."""
    if not target:
        return url_for("admin.list_clients")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return url_for("admin.list_clients")
    return target


@auth_bp.before_app_request
def _require_login():
    if request.endpoint in _EXEMPT_ENDPOINTS:
        return None
    if session.get("superuser") or "user_id" in session:
        if load_identity():
            return None
        # session referenced a deleted/deactivated DB user — drop it and
        # fall through to the redirect below rather than leave it dangling.
        session.clear()
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
    return render_template("login.html", next=request.args.get("next", ""))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
