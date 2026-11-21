"""
tests/test_bloom_settings.py — a Bloom heating building's settings, changed from
the portal (no database)

The fixture mirrors the setting points of a real Skyline building as
GET /api/assets returned them 2026-11-20 (928 Alan Page Dr): thirteen points,
each listed once, each with an _id. The encodings are Bloom's own, read from
its Schedule screen's code: times are the hour x 100 plus hundredths of an
hour, dates are the day of a leap year.
"""
import copy
import datetime as dt

from app import bloom, bloom_settings as bs

Z = {"type": "zone", "key": "bldg"}
NOW = dt.datetime(2026, 11, 20, 15, 0, 0)
VALUES = {"start-tm-day": 500, "stop-tm-day": 2200, "ind-sp-day": 72, "ind-sp-night": 72, "oat-sp-day": 55,
          "oat-sp-night": 55, "wint-start-day": 275, "wint-end-day": 152, "space-min-sp-enabled": 1,
          "space-min-threshold-type": 1, "space-min-threshold": 20, "space-min-sp-day": 68, "space-min-sp-night": 68}


def building(**change):
    """928 Alan Page Dr with its thirteen setting points (and two readings)."""
    vals = {**VALUES, **change}
    sensors = [{"_id": f"id-{t}", "type": t, "pv": v, "unit": 64, "assoc": Z, "pollTs": "2026-11-20T14:58:00.000Z"}
               for t, v in vals.items()]
    sensors += [{"_id": "id-oat", "type": "oat-act", "pv": 51.1, "pollTs": "2026-11-20T14:59:00.000Z"},
                {"_id": "id-avg", "type": "avg-space-t", "pv": 73.9, "assoc": Z, "pollTs": "2026-11-20T14:59:30.000Z"}]
    return {"_id": "68cd", "name": "928 Alan Page Dr", "locid": "928-alan-page-dr", "sensors": sensors}


def row(p, key):
    return next(r for r in p["rows"] if r["key"] == key)


# ── Bloom's encodings ────────────────────────────────────────────────────────

def test_a_time_is_the_hour_and_hundredths_of_an_hour_not_hhmm():
    assert bs.clock_decode(500) == (5, 0) and bs.clock_decode(2200) == (22, 0)
    assert bs.clock_decode(550) == (5, 30)                    # NOT ten to six
    assert bs.clock_decode(2250) == (22, 30) and bs.clock_decode(0) == (0, 0)
    assert bs.clock_encode(5, 30) == 550 and bs.clock_encode(22, 0) == 2200 and bs.clock_encode(0, 30) == 50
    assert bs.clock_text(500) == "5:00 AM" and bs.clock_text(2250) == "10:30 PM" and bs.clock_text(1200) == "12:00 PM"
    assert bs.clock_text(None) == "—" and bs.clock_decode("x") is None
    # the building page reads the same numbers the same way
    assert bloom._clock(550) == "5:30" and bloom._clock(2200) == "22:00" and bloom._clock(None) is None


def test_every_half_hour_survives_the_round_trip():
    for h in range(24):
        for m in (0, 30):
            assert bs.clock_decode(bs.clock_encode(h, m)) == (h, m)
    opts = bs.clock_options(500)
    assert len(opts) == 48 and opts[0] == ("00:00", "12:00 AM") and ("05:00", "5:00 AM") in opts
    odd = bs.clock_options(525)                               # a stored quarter hour is still offered, so it can stay
    assert len(odd) == 49 and ("05:15", "5:15 AM") in odd


def test_a_date_is_the_day_of_a_leap_year():
    assert bs.date_decode(275) == (10, 1) and bs.date_decode(152) == (5, 31) and bs.date_decode(60) == (2, 29)
    assert bs.date_encode(10, 1) == 275 and bs.date_encode(5, 31) == 152
    assert bs.date_text(275) == "October 1" and bs.date_text(152) == "May 31"
    assert bs.date_decode(0) is None and bs.date_decode(367) is None and bs.date_text("x") == "—"


def test_values_read_the_way_a_person_says_them():
    assert bs.text("ind-sp-day", 72) == "72°" and bs.text("oat-sp-day", 55.0) == "55°"
    assert bs.text("space-min-sp-enabled", 1) == "on" and bs.text("space-min-sp-enabled", 0) == "off"
    assert bs.text("space-min-threshold-type", 1) == "a percentage of the apartments"
    assert bs.text("space-min-threshold", 20, 1) == "20%" and bs.text("space-min-threshold", 3, 0) == "3 apartments"
    assert bs.text("space-min-threshold", 1, 0) == "1 apartment" and bs.text("ind-sp-day", None) == "—"


# ── which settings a building offers ─────────────────────────────────────────

def test_a_skyline_building_offers_all_thirteen():
    b = building()
    assert bs.offered(b) == 13
    p = bs.plan(b)
    assert [r["key"] for r in p["rows"]] == [s["key"] for s in bs.SETTINGS]
    assert all(r["found"] and not r["change"] and not r["error"] for r in p["rows"])
    assert p["n_changes"] == 0 and p["points"] == [] and p["digest"] == "" and not p["errors"]
    assert row(p, "start-tm-day")["old_text"] == "5:00 AM" and row(p, "wint-end-day")["old_text"] == "May 31"
    assert row(p, "space-min-threshold")["old_text"] == "20%"


def test_a_setting_bloom_lists_twice_is_not_offered():
    b = building()
    b["sensors"].append({"_id": "dup", "type": "space-min-sp-day", "pv": 68})        # 905 Alan Page Dr has this
    assert bs.offered(b) == 12
    p = bs.plan(b, {"space-min-sp-day": 66, "ind-sp-day": 70})
    r = row(p, "space-min-sp-day")
    assert not r["found"] and "2 times" in r["why"] and "cannot be changed here" in r["error"]
    assert p["errors"] and p["points"] == []                  # all or nothing: the day setpoint does not go alone


def test_a_heat_pump_plant_offers_nothing():
    b = {"name": "354 Nepperhan Ave", "sensors": [{"_id": "x", "type": "gen-t", "pv": 96, "name": "PDT BLDG A SUPPLY"}]}
    assert bs.offered(b) == 0 and bs.plan(b)["n_offered"] == 0
    assert bs.offered({}) == 0 and bs.offered(None) == 0


def test_a_point_without_an_id_cannot_be_written():
    b = building()
    next(s for s in b["sensors"] if s["type"] == "ind-sp-day").pop("_id")
    assert bs.offered(b) == 12 and "no id" in row(bs.plan(b), "ind-sp-day")["why"]


def test_how_long_since_bloom_heard_from_the_building():
    assert round(bs.heard_age(building(), now=NOW), 1) == 0.5     # the freshest point decides
    assert bs.heard_age({"sensors": [{"type": "x", "pv": 1}]}, now=NOW) is None
    old = building()
    for s in old["sensors"]:
        s["pollTs"] = "2026-11-19T21:24:00.000Z"
    assert bs.heard_age(old, now=NOW) > bs.STALE_MIN


# ── a change ─────────────────────────────────────────────────────────────────

def test_only_the_values_that_differ_are_sent_as_bloom_wants_them():
    b = building()
    p = bs.plan(b, {**VALUES, "ind-sp-day": 70, "stop-tm-day": 2250})
    assert p["n_changes"] == 2 and not p["errors"] and len(p["digest"]) == 16
    sent = {x["type"]: x for x in p["points"]}
    assert set(sent) == {"ind-sp-day", "stop-tm-day"}
    day = sent["ind-sp-day"]                                  # Bloom's own object, pv new, originalPv old
    assert day["_id"] == "id-ind-sp-day" and day["pv"] == 70 and day["originalPv"] == 72 and day["assoc"] == Z
    assert sent["stop-tm-day"]["pv"] == 2250 and sent["stop-tm-day"]["originalPv"] == 2200
    r = row(p, "ind-sp-day")
    assert r["change"] and r["old_text"] == "72°" and r["new_text"] == "70°"
    assert row(p, "stop-tm-day")["new_text"] == "10:30 PM" and not row(p, "ind-sp-night")["change"]
    assert p["log"] == [{"type": "stop-tm-day", "name": None, "old": 2200, "new": 2250, "label": "Night period starts"},
                        {"type": "ind-sp-day", "name": None, "old": 72, "new": 70, "label": "Indoor setpoint, day"}]
    assert next(s for s in b["sensors"] if s["type"] == "ind-sp-day")["pv"] == 72     # the live object is untouched
    assert bs.write_summary(p["log"]) == "Night period starts 10:00 PM → 10:30 PM; Indoor setpoint, day 72° → 70°"


def test_a_value_bloom_already_holds_is_not_a_change_even_as_a_float():
    b = building(**{"ind-sp-day": 72.0})
    assert bs.plan(b, {"ind-sp-day": 72})["n_changes"] == 0
    odd = building(**{"ind-sp-night": 71.5, "start-tm-day": 501})      # values off Bloom's own grid, left alone
    assert bs.clock_encode(*bs.clock_decode(501)) == 502      # 5:01 does not come back as the same number...
    p = bs.plan(odd, {"ind-sp-night": 71.5, "start-tm-day": 502, "ind-sp-day": 70})       # ...and is still no change
    assert not p["errors"] and [x["type"] for x in p["points"]] == ["ind-sp-day"]


def test_blooms_own_limits_are_enforced_on_what_changes():
    for key, bad in (("ind-sp-day", 81), ("ind-sp-night", 49), ("oat-sp-day", 71), ("oat-sp-night", -1),
                     ("space-min-sp-day", 91), ("space-min-threshold", 0), ("space-min-threshold", 101),
                     ("space-min-threshold-type", 2), ("space-min-sp-enabled", 5), ("ind-sp-day", 70.5),
                     ("wint-start-day", 400), ("start-tm-day", 2500)):
        p = bs.plan(building(), {key: bad, "ind-sp-night": 70 if key != "ind-sp-night" else 49})
        assert p["errors"] and row(p, key)["error"], key
        assert p["points"] == [] and p["digest"] == "" and p["n_changes"] == 0        # nothing goes, not even the good half
    assert "50° to 80°" in row(bs.plan(building(), {"ind-sp-day": 81}), "ind-sp-day")["error"]
    for key, good in (("ind-sp-day", 50), ("ind-sp-day", 80), ("oat-sp-day", 0), ("oat-sp-day", 70),
                      ("space-min-sp-night", 90), ("space-min-threshold", 100)):
        assert bs.plan(building(), {key: good})["n_changes"] == 1, key
    # a value already out of range in Bloom is not our error until someone changes it
    assert not bs.plan(building(**{"ind-sp-night": 45}), {"ind-sp-night": 45, "ind-sp-day": 70})["errors"]


def test_the_day_period_has_to_start_before_the_night_period():
    p = bs.plan(building(), {"start-tm-day": 2300})
    assert p["errors"] and "start before" in row(p, "start-tm-day")["error"] and not row(p, "stop-tm-day")["error"]
    assert bs.plan(building(), {"stop-tm-day": 500})["errors"]
    assert not bs.plan(building(), {"start-tm-day": 600, "stop-tm-day": 2300})["errors"]
    same = bs.plan(building(), {"wint-end-day": 275})
    assert same["errors"] and "same day" in row(same, "wint-end-day")["error"]
    assert not bs.plan(building(), {"wint-start-day": bs.date_encode(9, 15)})["errors"]


def test_changing_how_the_threshold_is_counted_says_what_the_number_now_means():
    p = bs.plan(building(), {"space-min-threshold-type": 0})
    assert p["n_changes"] == 1 and row(p, "space-min-threshold")["note"] == "stays 20, now read as 20 apartments"
    both = bs.plan(building(), {"space-min-threshold-type": 0, "space-min-threshold": 3})
    assert row(both, "space-min-threshold")["new_text"] == "3 apartments" and not row(both, "space-min-threshold")["note"]


def test_the_digest_follows_the_change_not_the_order():
    a = bs.plan(building(), {"ind-sp-day": 70, "ind-sp-night": 68})["digest"]
    assert a == bs.plan(building(), {"ind-sp-night": 68, "ind-sp-day": 70})["digest"]
    assert a != bs.plan(building(), {"ind-sp-day": 70, "ind-sp-night": 67})["digest"]
    moved = bs.plan(building(**{"ind-sp-night": 68}), {"ind-sp-day": 70, "ind-sp-night": 68})["digest"]
    assert moved != a                                         # someone changed Bloom in between: a different change


def test_landed_means_bloom_now_holds_what_was_asked():
    want = {"ind-sp-day": 70, "stop-tm-day": 2250}
    assert not bs.landed(building(), want)
    assert not bs.landed(building(**{"ind-sp-day": 70}), want)
    assert bs.landed(building(**{"ind-sp-day": 70, "stop-tm-day": 2250}), want)
    assert not bs.landed(building(), {"ind-sp-day": 99})      # a refused change never counts as landed


# ── the form ─────────────────────────────────────────────────────────────────

FORM = {"start-tm-day": "05:00", "stop-tm-day": "22:00", "ind-sp-day": "72", "ind-sp-night": "72", "oat-sp-day": "55",
        "oat-sp-night": "55", "wint-start-day.m": "10", "wint-start-day.d": "1", "wint-end-day.m": "5",
        "wint-end-day.d": "31", "space-min-sp-enabled.sent": "1", "space-min-sp-enabled": "1",
        "space-min-threshold-type": "1", "space-min-threshold": "20", "space-min-sp-day": "68",
        "space-min-sp-night": "68"}


def test_the_form_as_it_was_shown_changes_nothing():
    wanted, errors = bs.parse(FORM)
    assert errors == {} and wanted == VALUES
    assert bs.plan(building(), wanted)["n_changes"] == 0


def test_the_form_reads_times_dates_ticks_and_numbers():
    form = {**FORM, "stop-tm-day": "22:30", "ind-sp-day": "70", "wint-start-day.m": "9", "wint-start-day.d": "15"}
    del form["space-min-sp-enabled"]                          # the tick taken off
    wanted, errors = bs.parse(form)
    assert errors == {} and wanted["stop-tm-day"] == 2250 and wanted["ind-sp-day"] == 70
    assert wanted["wint-start-day"] == 259 and wanted["space-min-sp-enabled"] == 0
    p = bs.plan(building(), wanted)
    assert sorted(x["type"] for x in p["points"]) == ["ind-sp-day", "space-min-sp-enabled", "stop-tm-day", "wint-start-day"]
    assert bs.parse({}) == ({}, {})                           # nothing posted: nothing asked
    assert bs.parse({"ind-sp-day": "", "stop-tm-day": " "}) == ({}, {})


def test_what_cannot_be_read_is_an_error_on_its_own_row():
    wanted, errors = bs.parse({**FORM, "ind-sp-day": "warm", "stop-tm-day": "late", "wint-end-day.m": "2",
                               "wint-end-day.d": "31"})
    assert set(errors) == {"ind-sp-day", "stop-tm-day", "wint-end-day"} and "ind-sp-day" not in wanted
    p = bs.plan(building(), wanted, errors)
    assert p["errors"] and p["points"] == []
    assert row(p, "ind-sp-day")["error"] == "not a number" and row(p, "wint-end-day")["error"] == "not a date"


def test_a_previewed_change_reaches_its_confirmation_whole():
    wanted, _ = bs.parse({**FORM, "stop-tm-day": "22:30", "ind-sp-day": "70", "space-min-sp-enabled": ""})
    again, errors = bs.parse({k: str(v) for k, v in bs.fields(wanted)})       # the hidden inputs, posted back
    assert errors == {} and again == wanted
    assert bs.plan(building(), again)["digest"] == bs.plan(building(), wanted)["digest"]
    assert copy.deepcopy(wanted) == wanted
