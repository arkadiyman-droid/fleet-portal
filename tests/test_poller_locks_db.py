"""
tests/test_poller_locks_db.py — a poller that loses the race for its advisory
lock must not try to release it (Postgres tier).

Two gunicorn workers run every poller. The one that loses used to call
pg_advisory_unlock anyway; Postgres answers that with a WARNING ("you don't
own a lock of type ExclusiveLock"), which filled the database log with
~1,400 lines a day on beverly-pi (found 2026-11-25).
"""
import pytest
from sqlalchemy import event, text

from tests.conftest import requires_postgres

pytestmark = requires_postgres


def _pollers():
    from app import billing, bloom, fleet_poller, rc_dhw
    return [("fleet", fleet_poller.fleet_poller, fleet_poller._ADVISORY_LOCK_KEY),
            ("billing", billing.billing_scheduler, billing._ADVISORY_LOCK_KEY),
            ("bloom", bloom.bloom_poller, bloom._LOCK_KEY),
            ("rc_dhw", rc_dhw.rc_dhw_poller, rc_dhw._LOCK_KEY)]


@pytest.mark.parametrize("which", ["fleet", "billing", "bloom", "rc_dhw"])
def test_the_loser_never_releases_a_lock_it_does_not_hold(app, session, which):
    from app.extensions import db
    name, poller, key = next(p for p in _pollers() if p[0] == which)
    sent = []
    listen = lambda conn, cur, stmt, params, ctx, many: sent.append(stmt)   # noqa: E731
    event.listen(db.engine, "before_cursor_execute", listen)
    other = db.engine.connect()                       # "the other worker" holds the lock
    try:
        assert other.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar()
        poller._run_cycle(app)
        assert any("pg_try_advisory_lock" in s for s in sent)
        assert not any("pg_advisory_unlock" in s for s in sent), f"{name} released a lock it never got"
    finally:
        other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
        other.close()
        event.remove(db.engine, "before_cursor_execute", listen)

    sent.clear()                                      # and the winner does release it
    event.listen(db.engine, "before_cursor_execute", listen)
    try:
        poller._run_cycle(app)
        assert any("pg_advisory_unlock" in s for s in sent)
    finally:
        event.remove(db.engine, "before_cursor_execute", listen)
