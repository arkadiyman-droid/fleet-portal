"""
tests/test_bloom_fans.py — roof exhaust fans from the Bloom feed (no database)

The fixture mirrors the real /api/assets shape confirmed against the live
"omni" account 2026-11-16: fan + fanGroup equipment, per-fan fan-rs /
fan-run-fail / fan-rt-day (seconds), per-group fan-cmd, gen-sched "S::H" hourly
bits, oat-sp-gen / gen-sp-enabled per schedule, and several oat-act readings of
which only the freshest is real. Values are synthetic.
"""
import datetime as dt

from app import bloom_fans as bf

NOW = dt.datetime(2026, 11, 16, 16, 50)
FRESH = "2026-11-16T16:49:00.000Z"
OLD = "2024-12-27T15:55:57.346Z"


def _eq(key):
    return {"type": "equipment", "key": key}


def _building(outdoor=51, outdoor_ts=FRESH, fan_ts=FRESH, rs=(0, 1, 1), cmd=1, sp_on=1, rt=(0, 42000, 42000)):
    sensors = [
        {"type": "oat-act", "pv": 44.6, "pollTs": OLD},                       # a stale duplicate
        {"type": "ht-supply-t", "pv": 150, "pollTs": FRESH},                  # heating: not ours
        {"type": "fan-cmd", "pv": cmd, "assoc": _eq("fg-1"), "pollTs": fan_ts},
        {"type": "gen-sched-assign", "name": "fg-1", "pv": 1, "pollTs": fan_ts},
        {"type": "oat-sp-gen", "name": "1", "pv": 35, "pollTs": fan_ts},
        {"type": "oat-sp-gen", "name": "2", "pv": 36, "pollTs": fan_ts},
        {"type": "gen-sp-enabled", "name": "oatSp::1", "pv": sp_on, "pollTs": fan_ts},
        {"type": "gen-sp-enabled", "name": "oatSp::2", "pv": 0, "pollTs": fan_ts},
    ]
    if outdoor is not None:
        sensors.append({"type": "oat-act", "pv": outdoor, "pollTs": outdoor_ts,
                        "assoc": {"type": "zone", "key": "bldg"}})
    for h in range(24):
        sensors.append({"type": "gen-sched", "name": f"1::{h}", "pv": 1 if h in (8, 9, 18, 19) else 0})
        sensors.append({"type": "gen-sched", "name": f"2::{h}", "pv": 0})
    for i, on in enumerate(rs, start=1):
        k = f"exhaust-{i}"
        sensors += [{"type": "fan-rs", "pv": on, "assoc": _eq(k), "pollTs": fan_ts},
                    {"type": "fan-run-fail", "pv": 0 if on else 1, "assoc": _eq(k), "pollTs": fan_ts},
                    {"type": "fan-rt-day", "pv": rt[i - 1], "assoc": _eq(k), "pollTs": fan_ts}]
    return {
        "name": "1 Test St", "locid": "1-Test-St",
        "equipment": [{"type": "fan", "key": f"exhaust-{i}", "name": f"Exhaust Fan {i}"} for i in (1, 2, 3)]
                     + [{"type": "fanGroup", "key": "fg-1", "name": "Exhaust Fan Group 1",
                         "fans": ["exhaust-1", "exhaust-2", "exhaust-3"]},
                        {"type": "boiler", "key": "b-1", "name": "Boiler"}],
        "sensors": sensors,
    }


def _sum(**kw):
    hour = kw.pop("hour", 11)
    return bf.summary(bf.trim(_building(**kw)), now=NOW, cold_f=35, hour=hour)


def test_trim_keeps_only_the_fan_slice():
    t = bf.trim(_building())
    assert {e["type"] for e in t["equipment"]} == {"fan", "fanGroup"}
    assert not any(s["type"] == "ht-supply-t" for s in t["sensors"])
    assert bf.has_fans(_building()) and not bf.has_fans({"sensors": [{"type": "pmp-rs", "pv": 1}]})


def test_ranges_words():
    bits = [1 if h in (8, 9, 10, 18, 19, 20) else 0 for h in range(24)]
    assert bf.ranges(bits) == "8am–11am, 6pm–9pm"
    assert bf.ranges([0] * 24) == "off all day" and bf.ranges([1] * 24) == "all day"
    assert bf.ranges([1] + [0] * 22 + [1]) == "12am–1am, 11pm–12am"


def test_counts_groups_and_schedule():
    s = _sum()
    assert (s["total"], s["on"], s["fail"]) == (3, 2, 1)       # fan 1 is off while commanded on
    g = s["groups"][0]
    assert g["cmd"] is True and g["sched"] == 1 and [f["on"] for f in g["fans"]] == [False, True, True]
    assert s["cut"]["sp"] == 35 and s["cut"]["hours_on"] == 4 and s["cut"]["ranges"] == "8am–10am, 6pm–8pm"
    assert s["outdoor"] == 51                                   # the fresh reading, not the 2024 one
    assert s["cut_active"] is False and s["tone"] == "fault"
    assert round(s["rt_max_h"], 1) == 11.7                      # fan-rt-day is seconds


def test_healthy_building_is_ok():
    s = _sum(rs=(1, 1, 1), rt=(42000, 42000, 42000))
    assert s["tone"] == "ok" and s["status"] == "Running" and s["fail"] == 0 and s["flags"] == []


def test_running_in_the_cold_is_the_headline():
    s = _sum(outdoor=20, rs=(1, 1, 1), hour=11)
    assert s["tone"] == "cold" and s["cut_active"] is True
    assert "3 fans running at 20° outside" in s["status"] and "schedule says off" in s["status"]
    assert "scheduled on this hour" in _sum(outdoor=20, rs=(1, 1, 1), hour=8)["status"]


def test_cold_but_fans_off_is_not_flagged():
    s = _sum(outdoor=20, rs=(0, 0, 0), cmd=0)
    assert s["tone"] == "off" and s["fail"] == 0


def test_no_outdoor_reading_means_the_cutback_cannot_engage():
    s = _sum(outdoor=None, rs=(1, 1, 1))                         # only the 2024 duplicate is left
    assert s["tone"] == "blind" and "cannot engage" in s["status"] and s["outdoor"] is None
    b = _building(outdoor=None, rs=(1, 1, 1))
    b["sensors"] = [x for x in b["sensors"] if x["type"] != "oat-act"]
    assert "no outdoor reading" in bf.summary(bf.trim(b), now=NOW, hour=11)["status"]
    stale = _sum(outdoor=67, outdoor_ts="2026-09-12T10:00:00.000Z", rs=(1, 1, 1))
    assert stale["tone"] == "blind" and stale["outdoor"] is None and stale["outdoor_last"] == 67
    # with no outdoor-gated schedule in use there is nothing to be blind about
    assert _sum(outdoor=None, rs=(1, 1, 1), sp_on=0)["tone"] == "ok"


def test_a_building_not_heard_from_is_dark_however_good_it_looks():
    s = _sum(fan_ts="2026-09-12T10:00:00.000Z", rs=(1, 1, 1))
    assert s["stale"] and s["tone"] == "dark" and "Not reporting" in s["status"]


def test_runaway_runtime_counter_is_not_shown_as_hours():
    s = _sum(rs=(1, 1, 1), rt=(37_644_540, 37_644_540, 37_644_540))    # 10,456 h "today"
    assert s["rt_bad"] and s["rt_max_h"] is None


def test_latched_fail_flag_on_a_running_fan_is_flagged_not_down():
    b = _building(rs=(1, 1, 1))
    for x in b["sensors"]:
        if x["type"] == "fan-run-fail" and x["assoc"]["key"] == "exhaust-2":
            x["pv"] = 1
    s = bf.summary(bf.trim(b), now=NOW, hour=11)
    assert s["fail"] == 0 and s["groups"][0]["fans"][1]["flagged"] is True


FANS = [{"name": "Exhaust Fan 1"}, {"name": "Exhaust Fan 2"}, {"name": "Exhaust Fan 10"}]


def _samp(minutes_ago, rs, out):
    return (NOW - dt.timedelta(minutes=minutes_ago),
            {"on": rs.count("1"), "total": len(rs), "fail": 0, "out": out, "cut": False, "rs": rs})


def test_series_raw_has_one_row_per_fan():
    d = bf.series_from([_samp(10, "011", 40), _samp(5, "111", 39), _samp(0, "11", None)], FANS, 35, 1, NOW)
    assert d["res"] == "every read" and len(d["t"]) == 3 and d["t"] == sorted(d["t"])
    assert d["out"] == [40, 39, None] and d["on"] == [2, 3, 2] and d["sp"] == 35
    assert [f["label"] for f in d["fans"]] == ["1", "2", "10"]
    assert d["fans"][0]["data"] == [0, 1, 1]
    assert d["fans"][2]["data"] == [1, 1, None]          # a short rs (fan added later) is unknown, not off
    assert d["range"][1] - d["range"][0] == 86400 * 1000 and d["note"] is None


def test_series_over_a_week_is_hourly_with_run_fractions():
    hour = NOW.replace(minute=0)                           # NOW is 16:50; these four land in the 16:00 bucket
    samples = [(hour + dt.timedelta(minutes=m), {"on": on, "out": o, "fail": 0, "rs": rs})
               for m, rs, on, o in ((0, "100", 1, 30), (15, "100", 1, 32), (30, "110", 2, None), (45, "110", 2, 34))]
    d = bf.series_from(samples, FANS, 35, 30, NOW)
    assert d["res"] == "hourly averages" and len(d["t"]) == 1
    assert d["out"] == [32.0] and d["on"] == [1.5]
    assert [f["data"] for f in d["fans"]] == [[1.0], [0.5], [0.0]]


def test_series_with_no_samples_says_so():
    d = bf.series_from([], FANS, None, 1, NOW)
    assert d["t"] == [] and d["note"] and all(f["data"] == [] for f in d["fans"])


def test_history_sample():
    assert bf.history_sample(_sum()) == {"on": 2, "total": 3, "fail": 1, "out": 51, "cut": False, "rs": "011"}
