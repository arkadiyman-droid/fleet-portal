"""The overnight verdicts are cached (the Live page asks every minute and the
read-back is its slowest part): a finished night is worked out once, a night
still in progress at most every OPEN_TTL_S, and a new night starts fresh."""
import datetime as dt

from app import heat_overnight as ho

DAY = dt.datetime(2026, 11, 26, 17, 0)        # 12:00 EST: last night is over
NIGHT = dt.datetime(2026, 11, 27, 3, 0)       # 22:00 EST: tonight so far


def _counter():
    calls = []

    def build(start, end):
        calls.append((start, end))
        return {1: {"state": "ran", "n": len(calls)}}
    return calls, build


def test_a_finished_night_is_worked_out_once():
    calls, build = _counter()
    a = ho._cached("legacy", [1], DAY, build)
    b = ho._cached("legacy", [1], DAY + dt.timedelta(hours=3), build)
    assert len(calls) == 1 and a == b
    b[2] = "changed by a caller"                                   # callers get their own dict
    assert 2 not in ho._cached("legacy", [1], DAY, build)


def test_a_night_in_progress_is_reworked_after_the_ttl(monkeypatch):
    calls, build = _counter()
    clock = [1000.0]
    monkeypatch.setattr(ho.time, "monotonic", lambda: clock[0])
    ho._cached("legacy", [1], NIGHT, build)
    clock[0] += ho.OPEN_TTL_S - 1
    ho._cached("legacy", [1], NIGHT, build)
    assert len(calls) == 1
    clock[0] += 2
    ho._cached("legacy", [1], NIGHT, build)
    assert len(calls) == 2


def test_the_first_minutes_after_9am_are_not_frozen(monkeypatch):
    calls, build = _counter()
    clock = [1000.0]
    monkeypatch.setattr(ho.time, "monotonic", lambda: clock[0])
    nine_oh_five = dt.datetime(2026, 11, 26, 14, 5)              # 09:05 EST: late reads may still land
    ho._cached("legacy", [1], nine_oh_five, build)
    clock[0] += ho.OPEN_TTL_S + 1
    ho._cached("legacy", [1], nine_oh_five, build)
    assert len(calls) == 2


def test_each_source_box_set_and_night_has_its_own_entry():
    calls, build = _counter()
    ho._cached("legacy", [1, 2], DAY, build)
    ho._cached("legacy", [2, 1], DAY, build)                      # same set, other order: same entry
    ho._cached("bloom", [1, 2], DAY, build)
    ho._cached("legacy", [1], DAY, build)
    assert len(calls) == 3
    ho._cached("legacy", [1, 2], NIGHT, build)                    # the next night
    assert len(calls) == 4
    assert all(k[1] == ho.window(NIGHT)[0] for k in ho._cache)    # yesterday's entries are gone
