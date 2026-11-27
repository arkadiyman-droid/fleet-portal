"""A 2010-box round that sees the worker exiting stops reading and saves
nothing (Postgres tier): the next worker reads the boxes again."""
import pytest

from app import shutdown
from tests.conftest import requires_postgres
from tests.test_legacy_db import boxes, fake_poll  # noqa: F401  (the fixture is used by name)

pytestmark = requires_postgres


@pytest.fixture
def flag():
    shutdown._set_for_tests(False)
    yield
    shutdown._set_for_tests(False)


def test_a_round_stops_when_the_worker_exits(app, session, boxes, flag):  # noqa: F811
    import app.models as m
    from app import legacy
    seen = []

    def poll(job, now):
        seen.append(job["id"])
        shutdown._set_for_tests(True)            # the worker starts exiting while the first box is read
        return fake_poll(job, now)

    assert legacy.poll_all(app, poll=poll, workers=1) == 0
    assert len(seen) == 1                         # the boxes still queued were skipped, not read
    assert session.query(m.LegacyStatus).count() == 0      # and the cut-short round saved nothing


def test_a_normal_round_is_unchanged(app, session, boxes, flag):  # noqa: F811
    from app import legacy
    assert legacy.poll_all(app, poll=fake_poll, workers=1) == 1
