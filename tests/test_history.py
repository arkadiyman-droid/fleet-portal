"""
tests/test_history.py — the 2010 boxes' history arithmetic (app/history.py, pure)

Run minutes are counters since each box's own midnight (some clocks an hour
off), so usage comes from differences with reset and gap rules; the hourly
row must survive missing sensors and apply the heat-season minimums.
"""
import datetime as dt
import types

from app import history, legacy
from tests.test_legacy import ALARM_XML, NAMES_XML, STATUS_XML

H = dt.datetime(2026, 1, 15, 12, 0)          # 7 am in New York, January


def r(minute, **kw):
    base = {f"heat{i}": None for i in range(1, 5)} | {f"hw{i}": None for i in range(1, 5)}
    base.update(ts=H + dt.timedelta(minutes=minute), code="Outsd Sat", out=None, inside=None, dit=None, aqua=None,
                coil=None, mix=None, supp=None, retn=None, stack=None, hi_stack=None, fuel_count=None,
                water_count=None, apt=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


def test_reading_from_a_real_status():
    data = {"status": legacy.values(STATUS_XML), "names": legacy.values(NAMES_XML), "alarm": legacy.parse_alarm(ALARM_XML)}
    row = history.reading(7, H, data)
    assert row["box_id"] == 7 and row["code"] == "Outsd Sat"
    assert (row["out"], row["inside"], row["dit"], row["aqua"]) == (67, 75, 69, 83)
    assert (row["heat1"], row["hw1"], row["stack"]) == (2, 179, 90)
    assert row["apt"][0] == 75 and len(row["sys"]) <= 16


def test_run_minutes_reset_and_gaps():
    seq = [r(-5, heat1=50), r(0, heat1=53), r(5, heat1=58), r(10, heat1=2), r(15, heat1=7)]
    assert history.used(seq, "heat1") == 3 + 5 + 2 + 5           # the drop is the box's midnight
    assert history.used([r(0, heat1=10), r(5, heat1=40)], "heat1") == 6   # never more than the minutes passed
    assert history.used([r(0, heat1=10), r(120, heat1=60)], "heat1") == 0  # across a long gap: unknown
    assert history.used([r(0, water_count=100), r(5, water_count=90), r(10, water_count=95)], "water_count",
                        counter=True) == 5


def test_heat_season_minimums():
    assert history.law_minimum(H, 40) == 68                      # 7 am, cold
    assert history.law_minimum(H, 58) is None                    # 7 am, mild: no daytime rule
    assert history.law_minimum(dt.datetime(2026, 1, 15, 4, 0), 58) == 62   # 11 pm: night rule always
    assert history.law_minimum(dt.datetime(2026, 7, 15, 4, 0), 40) is None  # summer


def test_hour_row():
    prev = r(-5, heat1=100, hw1=10, apt=[70, 0, 66])
    cur = [r(m, heat1=100 + m, hw1=10, inside=68, dit=68, stack=300 + m, out=None,
             apt=[70, 0, 60 + m // 10], code="DTime Rem" if m < 30 else "Safety") for m in range(0, 60, 5)]
    row = history.hour_row(3, H, prev, cur, fleet_out=30.0, poll_min=5)
    assert row["n"] == 12 and row["out_avg"] == 30.0 and row["out_fleet"]
    assert row["heat_min"] == 55 and row["hw_min"] == 0             # 11 five-minute steps (prev to first read: 0)
    assert row["calling_min"] == 30 and row["safety_min"] == 30
    assert row["apt_avg"] == [70, None, 62] and row["apt_low"] == 60
    assert row["law_req"] == 68 and row["law_low"] == 1           # sensor 3 averaged 62 F
    assert abs(row["hdd"] - (65 - 30) / 24) < 1e-3
    assert row["stack_max"] == 355


def _cycle(prev, ok, data=None, error=None):
    now = history.remember(prev, ok, data)
    return now, [e["kind"] for e in history.changes(1, H, prev, ok, now, error)]


def test_events():
    st = lambda code, alarm=None: {"status": {"callForHeat": code}, "alarm": alarm}
    first, ev = _cycle({}, True, st("Outsd Sat"))
    assert ev == []                                                           # first read: nothing to compare
    now = history.remember(first, True, st("Safety"))
    ev = history.changes(1, H, first, True, now)
    assert [(e["kind"], e["code"]) for e in ev] == [("status", "Safety")]
    assert "Outside Temp. Satisfied -> Safety" in ev[0]["text"]

    miss1, ev = _cycle(now, False, error="no answer")
    assert ev == []                                                           # one miss: a reset connection
    miss2, ev = _cycle(miss1, False, error="no answer")
    assert ev == ["dark"]
    _s, ev = _cycle(miss2, False)
    assert ev == []                                                           # logged once
    back, ev = _cycle(miss2, True, st("Safety"))
    assert ev == ["back"]

    trip = {"name": "safetyActive", "at": "2026-01-15T07:01:00-05:00"}
    a1, ev = _cycle(back, True, st("Safety", trip))
    assert ev == ["alarm"]
    a2, ev = _cycle(a1, True, st("Safety"))                                   # this box then answers an empty list
    assert ev == []
    _a3, ev = _cycle(a2, True, st("Safety", trip))                            # ...and the same alarm again: not news
    assert ev == []


def test_day_bounds_follow_new_york():
    assert history.day_bounds(dt.date(2026, 1, 15)) == (dt.datetime(2026, 1, 15, 5), dt.datetime(2026, 1, 16, 5))
    s, e = history.day_bounds(dt.date(2026, 3, 8))                # spring forward: 23 hours
    assert e - s == dt.timedelta(hours=23)
