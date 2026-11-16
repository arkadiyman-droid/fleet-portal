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


# ── property grouping and the page ─────────────────────────────────────────────

INDEX = bf.building_index([("590-600 Gates Ave", "Betty"), ("735 Gates", "Betty"), ("392 Marion St.", "DCA"),
                           ("372 Franklyn Ave", "DCA"), ("Wil-1 325 Roebling N", "Williams")])


def test_place_by_name_key_and_address_range():
    assert bf.place(INDEX, "392 Marion St.") == "DCA"
    assert bf.place(INDEX, "392 marion st") == "DCA"                       # punctuation and case do not matter
    assert bf.place(INDEX, "372 Franklin Ave", "372-Franklyn-Ave") == "DCA"   # Bloom's name differs, its key matches
    assert bf.place(INDEX, "Wil-1 325 Roebling N", "Wil-1-325-Roebling-N") == "Williams"
    # a fan-only entry lands on the building whose address range holds its number
    assert bf.place(INDEX, "590 Gates Ave Exhaust Fans", "590-gates-exhaust") == "Betty"
    assert bf.place(INDEX, "600 Gates Ave Exhaust Fans") == "Betty"
    assert bf.place(INDEX, "610 Gates Ave Exhaust Fans") is None           # outside every range
    assert bf.place(INDEX, "590 Other St Exhaust Fans") is None            # right number, wrong street
    assert bf.place(INDEX, "1 Nowhere Rd") is None


def test_until_names_the_next_change():
    bits = [1 if h in (8, 9, 18, 19) else 0 for h in range(24)]
    assert bf.until(bits, 11) == (False, "6pm")
    assert bf.until(bits, 8) == (True, "10am")
    assert bf.until(bits, 22) == (False, "8am")                            # wraps past midnight
    assert bf.until([0] * 24, 5) == (False, None)


def test_banner_says_where_the_cold_schedule_stands():
    assert "on standby" in _sum()["banner"] and _sum()["banner_tone"] == "standby"
    cold = _sum(outdoor=20, rs=(1, 1, 1), hour=11)
    assert cold["banner_tone"] == "active" and "fans scheduled OFF until 6pm" in cold["banner"]
    assert _sum(outdoor=None, rs=(1, 1, 1))["banner_tone"] == "blind"
    s = _sum()
    assert s["schedules"][0]["used_by"] == ["G1"] and s["schedules"][1]["used_by"] == []
    assert [f["state"] for f in s["groups"][0]["fans"]] == ["down", "on", "on"]
    assert [f["num"] for f in s["groups"][0]["fans"]] == ["1", "2", "3"]


def _row(name, prop, **kw):
    r = _sum(**kw)
    r.update(name=name, prop=prop, client=None)
    return r


def test_page_groups_by_property_with_totals():
    rows = [_row("10 B St", "Zeta"), _row("2 A St", "Zeta", rs=(1, 1, 1)), _row("9 C St", "Alpha", rs=(1, 1, 1)),
            _row("5 D St", None, fan_ts="2026-09-12T10:00:00.000Z")]
    pg = bf.page(rows)
    assert [g["name"] for g in pg["groups"]] == ["Alpha", "Zeta", bf.UNFILED]      # unfiled last
    zeta = pg["groups"][1]
    assert [r["name"] for r in zeta["rows"]] == ["2 A St", "10 B St"]                # natural order: 2 before 10
    assert (zeta["on"], zeta["fans"], zeta["down"], zeta["off"]) == (5, 6, 1, 0)
    assert pg["groups"][2]["dark"] == 1 and pg["groups"][2]["dark_fans"] == 3 and pg["groups"][2]["fans"] == 0
    assert pg["summary"]["buildings"] == 4 and pg["summary"]["on"] == 8 and pg["summary"]["all_fans"] == 12


def test_page_filters_searches_and_sorts_by_status():
    rows = [_row("10 B St", "Zeta"), _row("2 A St", "Zeta", rs=(1, 1, 1)), _row("9 C St", "Alpha", rs=(1, 1, 1))]
    fault = bf.page(rows, show="fault")
    assert fault["show"] == "fault" and [r["name"] for g in fault["groups"] for r in g["rows"]] == ["10 B St"]
    assert fault["summary"]["buildings"] == 3                       # the tiles keep the whole-fleet totals
    assert bf.page(rows, show="nonsense")["show"] == "all"
    assert [g["name"] for g in bf.page(rows, q="alpha")["groups"]] == ["Alpha"]      # search matches the property too
    st = bf.page(rows, sort="status")
    assert len(st["groups"]) == 1 and st["groups"][0]["name"] is None
    assert st["groups"][0]["rows"][0]["name"] == "10 B St"          # the faulted building first


# ── the plan: how the fans should run ──────────────────────────────────────────

PLAN = bf.plan(36, 56)


def test_plan_bands_and_hours():
    assert [b["key"] for b in PLAN] == ["cold", "mild", "free"]
    cold, mild, free = PLAN
    assert cold["label"] == "35° and below" and cold["ranges"] == "8am–10am, 6pm–8pm" and cold["hours_on"] == 4
    assert mild["label"] == "36° to 55°" and mild["ranges"] == "8am–12pm, 5pm–10pm" and mild["hours_on"] == 9
    assert free["label"] == "56° and above" and free["hours_on"] == 24
    # 35 is in the cold band, 36 is not; 55 is in the middle band, 56 is not
    assert [bf.band_of(PLAN, v)["key"] for v in (20, 35, 35.9, 36, 55, 55.9, 56, 80)] == \
        ["cold", "cold", "cold", "mild", "mild", "mild", "free", "free"]


def test_hysteresis_drops_at_the_line_and_climbs_two_degrees_above_it():
    def step(prev, temp):
        return bf.step_band(PLAN, prev, temp, 2)["key"]
    # coming up out of the cold schedule: held until 38
    assert [step("cold", v) for v in (35, 36, 37, 37.9, 38, 40)] == ["cold", "cold", "cold", "cold", "mild", "mild"]
    # going down into it: at once
    assert [step("mild", v) for v in (37, 36, 35.9, 30)] == ["mild", "mild", "cold", "cold"]
    # the upper line the same way: free only from 58, back to the middle below 56
    assert [step("mild", v) for v in (55, 56, 57.9, 58)] == ["mild", "mild", "mild", "free"]
    assert [step("free", v) for v in (57, 56, 55.9)] == ["free", "free", "mild"]
    # a big jump is taken one line at a time, each with its margin
    assert step("cold", 57) == "mild" and step("cold", 58) == "free" and step("free", 20) == "cold"
    # no memory, or a band the plan no longer has: just the temperature
    assert step(None, 37) == "mild" and step("gap", 37) == "mild"
    assert bf.step_band(PLAN, "cold", None, 2) is None
    # sitting on the line does not flip the schedule back and forth
    band, seen = "mild", []
    for v in (36.2, 35.8, 36.3, 35.9, 36.4, 37.5, 38.1, 37.0):
        band = step(band, v)
        seen.append(band)
    assert seen == ["mild", "cold", "cold", "cold", "cold", "cold", "mild", "mild"]


def test_expect_says_what_the_plan_asks_for_now():
    e = bf.expect(PLAN, 44, 14, 30)                       # 2:30pm at 44°
    assert e["band"]["key"] == "mild" and e["on"] is False and e["until"] == "5pm" and not e["settling"]
    assert bf.expect(PLAN, 44, 9, 30)["on"] is True and bf.expect(PLAN, 44, 9, 30)["until"] == "12pm"
    assert bf.expect(PLAN, 20, 9, 30)["until"] == "10am"
    assert bf.expect(PLAN, 52, 3, 0)["band"]["key"] == "mild"       # 50-55 is the middle schedule now
    free = bf.expect(PLAN, 70, 3, 0)
    assert free["on"] is True and free["until"] is None and not free["held"]
    # the first ten minutes after the plan flips are grace
    assert bf.expect(PLAN, 44, 12, 5)["settling"] and not bf.expect(PLAN, 44, 12, 15)["settling"]
    assert not bf.expect(PLAN, 44, 13, 5)["settling"]     # no change at 1pm
    # held on the colder schedule by hysteresis, and told when it lets go
    h = bf.expect(PLAN, 37, 14, 30, prev_key="cold", hyst=2)
    assert h["band"]["key"] == "cold" and h["held"] and h["release"] == 38 and h["until"] == "6pm"
    assert not bf.expect(PLAN, 37, 14, 30, prev_key="mild", hyst=2)["held"]


def _plan(**kw):
    hour, minute, band = kw.pop("hour", 14), kw.pop("minute", 30), kw.pop("band", None)
    raw = bf.trim(_building(**kw))
    if band:
        raw["portal"] = {"band": band}                    # what the poller remembered
    return bf.summary(raw, now=NOW, hour=hour, minute=minute, pl=PLAN, hyst=2)


def test_fans_running_when_the_plan_says_off_is_the_headline():
    s = _plan(outdoor=44, rs=(1, 1, 1))                   # 2:30pm, 44°: off until 5pm
    assert s["over"] == 3 and s["under"] == 0 and s["tone"] == "over"
    assert "3 fans running — plan says off until 5pm (44° outside)" == s["status"]
    night = _plan(outdoor=20, rs=(1, 1, 0), hour=23)      # a cold night, two still on (the third is "down")
    assert night["over"] == 2 and night["tone"] == "over" and "until 8am" in night["status"]
    assert _plan(outdoor=52, rs=(1, 1, 1), hour=3)["over"] == 3             # 50-55 is judged now


def test_following_the_plan_is_not_flagged():
    assert _plan(outdoor=44, rs=(1, 1, 1), hour=9)["tone"] == "ok"          # on in an on-hour
    off = _plan(outdoor=44, rs=(0, 0, 0), cmd=0)                            # off in an off-hour
    assert off["over"] == 0 and off["under"] == 0 and off["tone"] == "off"
    assert _plan(outdoor=70, rs=(1, 1, 1), hour=3)["tone"] == "ok"          # warm: on around the clock
    assert _plan(outdoor=44, rs=(1, 1, 1), hour=12, minute=5)["over"] == 0  # just flipped: grace
    assert _plan(outdoor=44, rs=(1, 1, 1), hour=12, minute=15)["over"] == 3


def test_the_remembered_band_decides_near_a_line():
    # 11am at 37°: the middle schedule says ON, the cold one says OFF.
    fresh = _plan(outdoor=37, rs=(1, 1, 1), hour=11)                        # no memory: middle
    assert fresh["want"]["band"]["key"] == "mild" and fresh["over"] == 0
    held = _plan(outdoor=37, rs=(1, 1, 1), hour=11, band="cold")            # came up from the cold: still cold
    assert held["want"]["band"]["key"] == "cold" and held["want"]["held"] and held["over"] == 3
    out = _plan(outdoor=38, rs=(1, 1, 1), hour=11, band="cold")             # two degrees clear: released
    assert out["want"]["band"]["key"] == "mild" and not out["want"]["held"] and out["over"] == 0
    # 3am at 57° after a mild evening: still the middle schedule (off), not free-running
    assert _plan(outdoor=57, rs=(1, 1, 1), hour=3, band="mild")["over"] == 3
    assert _plan(outdoor=58, rs=(1, 1, 1), hour=3, band="mild")["over"] == 0


def test_held_off_when_the_plan_says_on():
    s = _plan(outdoor=70, rs=(0, 0, 0), cmd=0, hour=3)
    assert s["under"] == 3 and s["tone"] == "under" and "held off — plan says on" in s["status"]
    # a fan that is simply down in a group commanded on is a fault, not "held off"
    down = _plan(outdoor=70, rs=(0, 1, 1), hour=3)
    assert down["under"] == 0 and down["fail"] == 1 and down["tone"] == "fault"


def test_no_outdoor_or_no_report_means_no_judgement():
    assert _plan(outdoor=None, rs=(1, 1, 1))["want"] is None
    stale = _plan(outdoor=20, rs=(1, 1, 1), fan_ts="2026-09-12T10:00:00.000Z")
    assert stale["over"] == 0 and stale["tone"] == "dark"


def test_plan_gaps_name_what_bloom_is_missing():
    g = _plan(outdoor=44, rs=(1, 1, 1))["gaps"]           # fixture: one schedule, below 35°, 8-10 + 6-8
    assert g == ["No schedule in Bloom for 36° to 55° — the plan is 8am–12pm, 5pm–10pm."]
    b = _building(rs=(1, 1, 1))
    for x in b["sensors"]:                                # stretch the cold schedule to 8-11
        if x["type"] == "gen-sched" and x["name"] == "1::10":
            x["pv"] = 1
    g = bf.summary(bf.trim(b), now=NOW, hour=14, pl=PLAN)["gaps"]
    assert g[0] == "35° and below: Bloom runs the fans 8am–11am, 6pm–8pm; the plan is 8am–10am, 6pm–8pm."
    for x in b["sensors"]:                                # a setpoint the plan does not have
        if x["type"] == "oat-sp-gen" and x["name"] == "1":
            x["pv"] = 40
    g = bf.summary(bf.trim(b), now=NOW, hour=14, pl=PLAN)["gaps"]
    assert any("below 40°" in s and "not in the plan" in s for s in g)
    assert any(s.startswith("No schedule in Bloom for 35° and below") for s in g)


def test_page_counts_plan_deviations_and_bands():
    rows = [_plan(outdoor=44, rs=(1, 1, 1)), _plan(outdoor=70, rs=(1, 1, 1)),
            _plan(outdoor=37, rs=(1, 1, 1), band="cold")]
    for i, r in enumerate(rows):
        r.update(name=f"{i} St", prop="P", client=None)
    pg = bf.page(rows)
    assert pg["summary"]["over"] == 2 and pg["summary"]["over_fans"] == 6 and pg["summary"]["gaps"] == 3
    assert pg["summary"]["bands"] == {"cold": 1, "mild": 1, "free": 1} and pg["summary"]["held"] == 1
    assert [r["name"] for g in bf.page(rows, show="over")["groups"] for r in g["rows"]] == ["0 St", "2 St"]


# ── the audit view: raw Bloom values beside what the page shows ────────────────

def test_raw_view_lays_out_every_point_untouched():
    b = _building()                                       # fan 1 off, 2 and 3 on; group commanded on
    v = bf.raw_view(bf.trim(b), now=NOW)
    f1, f2 = v["fans"][0], v["fans"][1]
    assert (f1["key"], f1["rs"]["pv"], f1["fail"]["pv"], f1["rt"]["pv"], f1["shown"]) == ("exhaust-1", 0, 1, 0, "DOWN")
    assert (f2["rs"]["pv"], f2["rt"]["pv"], f2["shown"], f2["shown_rt"]) == (1, 42000, "ON", 11.67)
    assert f1["manual"] is None                           # the fixture has no fan-run-manual: "not in feed"
    assert f2["rs"]["age"] == "1 min" and not f2["rs"]["stale"]
    g = v["groups"][0]
    assert (g["key"], g["cmd"]["pv"], g["assign"]["pv"], g["shown"], g["members"]) == \
        ("fg-1", 1, 1, "START", ["exhaust-1", "exhaust-2", "exhaust-3"])
    s1, s2 = v["schedules"]
    assert s1["bits"] == "000000001100000000110000" and s1["sp"]["pv"] == 35 and s1["enabled"]["pv"] == 1
    assert s1["used_by"] == ["G1"] and s2["bits"] == "0" * 24 and s2["enabled"]["pv"] == 0
    assert v["other"] == []                               # every stored point landed in a table
    assert v["n_sensors"] == len(bf.trim(b)["sensors"])


def test_raw_view_marks_which_outdoor_reading_is_used():
    v = bf.raw_view(bf.trim(_building(outdoor=51)), now=NOW)
    live = next(o for o in v["outdoor"] if o["pv"] == 51)
    dead = next(o for o in v["outdoor"] if o["pv"] == 44.6)
    assert live["used"] and live["freshest"] and not dead["used"] and dead["stale"]
    assert "2024" in dead["at"]                           # an old reading names its year
    # only the 2024 reading left: it is the freshest, but too old to use
    v = bf.raw_view(bf.trim(_building(outdoor=None)), now=NOW)
    assert [(o["freshest"], o["used"]) for o in v["outdoor"]] == [(True, False)]


def test_raw_view_of_a_silent_building_shows_no_data():
    v = bf.raw_view(bf.trim(_building(fan_ts="2026-09-12T10:00:00.000Z", rs=(1, 1, 1))), now=NOW)
    assert all(f["shown"] == "NO DATA" and f["rs"]["stale"] and f["rs"]["pv"] == 1 for f in v["fans"])


def test_raw_view_times_are_local_when_a_zone_is_given():
    tz = dt.timezone(dt.timedelta(hours=-5))
    v = bf.raw_view(bf.trim(_building()), now=NOW, tz=tz)
    assert v["fans"][1]["rs"]["at"] == "Nov 16, 11:49:00 AM"      # 16:49Z in New York winter time
