import getpass
import os

import click
from dotenv import load_dotenv
from flask import Flask, render_template
from werkzeug.security import generate_password_hash

from app.extensions import csrf, db, migrate


def load_env(path=None) -> None:
    """Read .env into the environment.

    Normally a value already in the environment wins (tests and the shell
    set their own). With FLEET_ENV_FILE_WINS=1 -- set in .env on the server
    -- the FILE wins: systemd hands the service .env as it was at the last
    full restart, and a reload (kill -HUP) re-runs this but cannot change
    that environment, so without this an edited .env was silently ignored
    until the next restart (found 2026-10-14: a changed alert topic kept
    the old value after a reload)."""
    load_dotenv(path)
    if os.environ.get("FLEET_ENV_FILE_WINS") == "1":
        load_dotenv(path, override=True)


def create_app():
    load_env()

    from config import Config

    app = Flask(__name__)
    app.config.from_object(Config())

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    from app.admin_auth import auth_bp
    from app.enroll_api import enroll_bp
    from app.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(enroll_bp)

    @app.errorhandler(403)
    def _forbidden(e):
        return render_template("403.html"), 403

    from flask_wtf.csrf import CSRFError

    @app.errorhandler(CSRFError)
    def _stale_form(e):
        # A page left open longer than the form token lives (an hour): say
        # so in plain words and put the person back on the page, rather than
        # a bare "400 Bad Request". The action was NOT done.
        from flask import flash, redirect, request
        from urllib.parse import urlparse
        ref = urlparse(request.referrer or "")
        back = ref.path if ref.netloc == request.host and ref.path.startswith("/") else "/"
        flash("That page was open too long, so its buttons expired and nothing was done. "
              "The page is fresh now: press the button again.", "error")
        return redirect(back + (("?" + ref.query) if ref.query and back != "/" else ""))

    from app.fleet_poller import fleet_poller
    fleet_poller.start(app)

    from app.billing import billing_scheduler
    billing_scheduler.start(app)

    @app.cli.command("hash-password")
    def hash_password():
        """Prompt for a password and print its werkzeug hash, for pasting into .env as ADMIN_PASSWORD_HASH."""
        pw = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if pw != confirm:
            click.echo("Passwords did not match.")
            return
        click.echo(generate_password_hash(pw))

    return app
