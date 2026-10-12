"""
tests/conftest.py — shared fixtures.

Two tiers of test live here, and the split is deliberate.

PURE tests need no database at all. They import the billing/permissions
modules directly and exercise logic that is just arithmetic, mapping, or
policy. They run anywhere, including a laptop with no Postgres, which is
what makes them worth having: a test suite that only runs on a configured
machine is a suite nobody runs.

DATABASE tests need **Postgres specifically**, and skip cleanly without
it. SQLite is not a substitute here and must not be substituted in later:

  * ControllerStatus.raw_json is `JSONB`, which does not compile on SQLite.
  * Contract's "at most one current contract per building" rule is a
    PARTIAL unique index (`postgresql_where=end_date IS NULL`). On any
    other dialect SQLAlchemy silently emits a PLAIN unique index, which
    would forbid a building from ever having a second contract at all —
    the test would then pass against behaviour the product does not have.
  * The billing and poller schedulers coordinate with
    `pg_try_advisory_lock`.

Point FLEET_TEST_DATABASE_URL at a throwaway database to run them:

    FLEET_TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/fleet_test

It is dropped and recreated per test. Never aim it at anything real.
"""

import os

import pytest

# Config() calls _require_env() for several variables at construction time,
# so anything that builds an app needs them present. These are throwaway
# values for tests only — the encryption key is a valid Fernet key so the
# EncryptedString round-trip is genuinely exercised rather than skipped.
os.environ.setdefault("SECRET_KEY", "test-secret-not-used-anywhere-real")
os.environ.setdefault("ADMIN_PASSWORD_HASH", "pbkdf2:sha256:1000$test$0" * 1)
os.environ.setdefault("FLEET_TOKEN_ENCRYPTION_KEY",
                      "zHq1nQ8kqk1Yx1yv0h2m9Qp3rJvKwX5tZ6aBcDeFgHI=")
# The schedulers are daemon threads started by create_app(). A test process
# has no business running them.
os.environ.setdefault("FLEET_POLLER_ENABLED", "0")
os.environ.setdefault("BILLING_SCHEDULER_ENABLED", "0")

TEST_DB_URL = os.environ.get("FLEET_TEST_DATABASE_URL")

requires_postgres = pytest.mark.skipif(
    not TEST_DB_URL,
    reason="set FLEET_TEST_DATABASE_URL to a throwaway Postgres database to run "
           "database tests (SQLite cannot stand in — see tests/conftest.py)",
)


@pytest.fixture
def app():
    """A real app bound to the throwaway Postgres database, schema created
    from the models and dropped afterwards."""
    if not TEST_DB_URL:
        pytest.skip("FLEET_TEST_DATABASE_URL not set")

    os.environ["DATABASE_URL"] = TEST_DB_URL
    from app import create_app
    from app.extensions import db

    application = create_app()
    with application.app_context():
        db.drop_all()
        db.create_all()
        yield application
        db.session.remove()
        db.drop_all()


@pytest.fixture
def session(app):
    from app.extensions import db
    return db.session
