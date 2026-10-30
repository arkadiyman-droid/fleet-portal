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

    from app.legacy import legacy_poller
    legacy_poller.start(app)

    from app.bloom import bloom_poller
    bloom_poller.start(app)

    @app.cli.command("hash-password")
    def hash_password():
        """Prompt for a password and print its werkzeug hash, for pasting into .env as ADMIN_PASSWORD_HASH."""
        pw = getpass.getpass("Password: ")
        confirm = getpass.getpass("Confirm: ")
        if pw != confirm:
            click.echo("Passwords did not match.")
            return
        click.echo(generate_password_hash(pw))

    @app.cli.command("bloom-add-account")
    @click.argument("label")
    @click.option("--base-url", default="https://my.empoweredbuildings.com")
    def bloom_add_account(label, base_url):
        """Create (or update) a Bloom account and verify the login. Prompts for
        the Bloom username/password; they are encrypted at rest. See app/bloom.py."""
        import requests
        from app import bloom
        from app.extensions import db
        from app.models import BloomAccount
        username = input("Bloom username/email: ").strip()
        password = getpass.getpass("Bloom password: ")
        acct = BloomAccount.query.filter_by(label=label).first() or BloomAccount(label=label)
        acct.base_url, acct.username, acct.password, acct.is_active = base_url, username, password, True
        db.session.add(acct)
        db.session.commit()
        try:
            tok = bloom.login(acct, requests.Session())
            acct.token = tok
            from datetime import datetime as _d
            acct.token_at = _d.utcnow()
            db.session.commit()
            click.echo(f"OK — logged in, token cached (account id {acct.id}).")
        except Exception as e:
            click.echo(f"Saved account id {acct.id}, but login FAILED: {e}")

    @app.cli.command("bloom-dump")
    @click.argument("label")
    @click.option("--n", default=1, help="how many assets to show")
    def bloom_dump(label, n):
        """Print the real /api/assets field names for one account — use this to
        finalise FIELD/_points() in app/bloom.py against live data."""
        import json
        import requests
        from app import bloom
        from app.models import BloomAccount
        acct = BloomAccount.query.filter_by(label=label).first()
        if not acct:
            click.echo(f"No Bloom account labelled {label!r}.")
            return
        s = requests.Session()
        tok = acct.token or bloom.login(acct, s)
        data, _ = bloom.fetch_json(acct, s, tok, "/api/assets")
        blds = bloom.buildings(data)
        sky = [b for b in blds if bloom.is_skyline(b)]
        click.echo(f"{len(blds)} buildings total, {len(sky)} skyline-terrace.")
        for b in sky[:n]:
            click.echo(f"{b.get('name')} [{b.get('locid')}] keys={sorted(b.keys())}")
            click.echo(json.dumps(b, indent=2, default=str)[:3000])

    @app.cli.command("bloom-poll-once")
    def bloom_poll_once():
        """Run one Bloom poll cycle now and report, for testing. See app/bloom.py."""
        from app.bloom import bloom_poller
        bloom_poller._run_cycle(app)
        click.echo("Bloom poll cycle done (see log for per-account detail).")

    return app
