import getpass

import click
from dotenv import load_dotenv
from flask import Flask
from werkzeug.security import generate_password_hash

from app.extensions import csrf, db, migrate


def create_app():
    load_dotenv()

    from config import Config

    app = Flask(__name__)
    app.config.from_object(Config())

    db.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    from app.admin_auth import auth_bp
    from app.routes import admin_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

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
