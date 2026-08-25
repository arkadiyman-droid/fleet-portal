from urllib.parse import urlparse

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

auth_bp = Blueprint("auth", __name__)

_EXEMPT_ENDPOINTS = {"auth.login", "auth.logout", "static"}


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
    if session.get("authenticated"):
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
            session["authenticated"] = True
            session.permanent = True
            return redirect(_safe_next(request.form.get("next", "")))
        flash("Invalid username or password.", "error")
    return render_template("login.html", next=request.args.get("next", ""))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
