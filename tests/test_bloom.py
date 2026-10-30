"""
tests/test_bloom.py — the Bloom (Empowered Buildings) read pipeline (no database)

Fixtures mirror the real /api/assets building object confirmed against the live
account 2026-10-29: a `sensors` list of {type, pv, assoc, name} where building
aggregates have name==null and per-apartment space sensors carry a name, pumps
are assoc'd to equipment, and `accounts` tags the owner (skyline-terrace).
"""
from app import bloom


def _s(type, pv, assoc=None, name=None, **extra):
    return {"type": type, "pv": pv, "assoc": assoc, "name": name, **extra}


Z = {"type": "zone", "key": "bldg"}

def _eq(key):
    return {"type": "equipment", "key": key}


# 928 Alan Page Dr, values from the live dump; loop is hot (169.6/133.7).
BLD = {
    "_id": "68cd", "name": "928 Alan Page Dr", "locid": "928-alan-page-dr",
    "accounts": ["skyline-terrace"],
    "sensors": [
        _s("oat-act", 51.7), _s("avg-space-t", 80.7, Z), _s("min-space-t", 71.3, Z),
        _s("max-space-t", 94, Z), _s("ind-sp-act", 72, Z), _s("cur-mode", 1, Z),
        _s("ht-supply-t", 169.6, Z), _s("ht-ret-t", 133.7, Z), _s("hw-supply-sp", 124.4, Z),
        _s("blr-pct", 0, _eq("bg-1/2")), _s("dhw-supply-t", 120.1, Z),
        _s("cfh", 0, Z), _s("system-status", 6, Z),
        _s("pmp-rs", 1, _eq("pump-1")), _s("pmp-cmd", 1, _eq("pump-1")),
        _s("pmp-rt-day", 34051, _eq("pump-1")),
        _s("pmp-rs", 0, _eq("pump-2")), _s("pmp-rt-day", 0, _eq("pump-2")),
        _s("space-t", 77, Z, name="135-9", stats={"belowSetpoint": 0}),
        _s("space-t", 66, Z, name="135-8", stats={"belowSetpoint": 1}),
    ],
}


def test_points_extracts_the_right_sensors():
    p = bloom._points(BLD)
    assert p["outside_f"] == 51.7
    assert p["space_avg_f"] == 80.7 and p["space_min_f"] == 71.3 and p["space_max_f"] == 94
    assert p["setpoint_f"] == 72 and p["mode"] == "DAY"
    assert p["supply_f"] == 169.6 and p["return_f"] == 133.7 and p["supply_sp_f"] == 124.4
    assert p["dhw_supply_f"] == 120.1 and p["boiler_pct"] == 0
    assert p["spaces"] == 2 and p["spaces_below"] == 1       # named space-t only
    # pump-1 running (34051s -> 567 min), pump-2 off; heat_rt is the sum
    assert p["pumps"][0]["on"] is True and p["pumps"][0]["run_min"] == 567
    assert p["pumps"][1]["on"] is False
    assert p["heat_rt"] == 567


def test_card_hot_loop_reads_as_heating():
    c = bloom.card({"asset": BLD, "alarms": []})
    assert c["loop_hot"] is True and c["heating"] is True
    assert c["tone"] == "heat" and c["status"] == "Heating"
    assert c["n_alarms"] == 0


def test_calling_for_heat_with_a_cold_loop_is_a_crit_problem():
    cold = dict(BLD, sensors=[
        _s("cfh", 1, Z), _s("ht-supply-t", 72, Z), _s("ht-ret-t", 68, Z),
        _s("pmp-rs", 0, _eq("pump-1")),
    ])
    c = bloom.card({"asset": cold, "alarms": []})
    assert c["cold_call"] is True and c["fault"] is True
    assert c["tone"] == "crit" and c["status"] == "Call for heat — not responding"


def test_a_fault_alarm_makes_the_card_crit_and_is_listed():
    alarms = [{"type": "pmp-run-fail", "txt": "Heating Pump 1 did not start when commanded", "end": None}]
    c = bloom.card({"asset": BLD, "alarms": alarms})
    assert c["fault"] is True and c["tone"] == "crit"
    assert c["n_alarms"] == 1 and "did not start" in c["alarms"][0]


def test_a_nonfault_alarm_is_warn_not_crit():
    alarms = [{"type": "pmp-run-manual", "txt": "Pump left in manual", "end": None}]
    c = bloom.card({"asset": BLD, "alarms": alarms})   # BLD loop is hot -> heating
    assert c["fault"] is False
    assert c["tone"] == "heat"      # heating wins; alarm still listed
    assert c["n_alarms"] == 1


def test_loop_hot_rule():
    assert bloom.loop_hot(150, 145) is True
    assert bloom.loop_hot(96, 70) is True
    assert bloom.loop_hot(92, 70) is True
    assert bloom.loop_hot(85, 80) is False
    assert bloom.loop_hot(0, 0) is False
    assert bloom.loop_hot(None, None) is False


def test_buildings_unwraps_the_account_object():
    assert bloom.buildings({"buildings": [1, 2]}) == [1, 2]
    assert bloom.buildings([3, 4]) == [3, 4]
    assert bloom.buildings({"nope": 1}) == []


def test_asset_key_is_the_locid():
    assert bloom.asset_key({"locid": "928-alan-page-dr", "_id": "x"}) == "928-alan-page-dr"
    assert bloom.asset_key({"_id": "abc"}) == "abc"
    assert bloom.asset_key({}) is None


def test_is_skyline_by_account_tag():
    assert bloom.is_skyline(BLD) is True
    assert bloom.is_skyline({"accounts": ["progressiveManagement"]}) is False
    assert bloom.is_skyline({}) is False


def test_alarms_group_by_locid_open_only():
    payload = [
        {"locId": "928-alan-page-dr", "type": "pmp-run-fail", "end": None},
        {"locId": "928-alan-page-dr", "type": "blr-alm", "end": None},
        {"locId": "721-alan-page-dr", "type": "pmp-run-manual", "end": None},
        {"locId": "928-alan-page-dr", "type": "old", "end": "2026-01-01T00:00:00Z"},  # closed -> skipped
    ]
    g = bloom.alarms_by_locid(payload)
    assert len(g["928-alan-page-dr"]) == 2
    assert len(g["721-alan-page-dr"]) == 1


def test_extract_token_unwraps_common_shapes():
    assert bloom._extract_token({"token": "abc"}) == "abc"
    assert bloom._extract_token({"data": {"access_token": "deep"}}) == "deep"
    assert bloom._extract_token("bare.jwt") == "bare.jwt"
    assert bloom._extract_token({"nope": 1}) is None


# A single Bloom feed covering TWO towers/buildings (Whitney Young: 354 + 358).
_TWO_TOWER = {
    "asset": {
        "name": "354-358 Nepperhan Ave", "locid": "354-358-nepperhan-ave",
        "sensors": [
            _s("space-t", 70, name="354 - 1A"),
            _s("space-t", 33, name="354 - 5E"),              # dead/offline sensor
            _s("space-t", 74, name="358 - 3E"),
            _s("space-h", 55, name="354 - 1A"),
            _s("space-h", 50, name="358 - 3E"),
            _s("gen-t", 94.4, name="PDT BLDG A SUPPLY"),
            _s("gen-t", 94.3, name="PDT BLDG A RETURN"),
            _s("gen-t", 52.1, assoc={"type": "zone", "key": "zone-a"}, name="PDT BLDG A SUPPLY"),  # cooling copy
            _s("gen-t", 97.4, name="PDT BLDG B SUPPLY"),
            _s("gen-t", 93.8, name="PDT BLDG B RETURN"),
            _s("diff-pr", 30.1, name="PDT DIFFERENTIAL PRESSURE"),   # shared, no tower
            _s("binary", 1, name="PDT WATER AVAILABLE"),             # shared, no tower
        ],
    },
    "alarms": [
        {"type": "pmp-run-manual", "end": None,
         "txt": "Tower A - Glycol Dual Temperature Pump 2 did not start when commanded"},
    ],
}


def test_view_scopes_shared_feed_to_each_building():
    a = bloom.card(_TWO_TOWER, view="354", plant_tag="A")
    b = bloom.card(_TWO_TOWER, view="358", plant_tag="B")
    # supply/return from each tower's UN-zoned PDT BLDG loop (not the cooling copy)
    assert (a["supply_f"], a["return_f"]) == (94.4, 94.3)
    assert (b["supply_f"], b["return_f"]) == (97.4, 93.8)
    assert a["spaces"] == 2 and b["spaces"] == 1
    # space avg/min ignore the dead (<40F) 354 sensor
    assert a["space_avg_f"] == 70.0 and a["space_min_f"] == 70
    # a Tower A alarm is 354's, not 358's
    assert a["n_alarms"] == 1 and b["n_alarms"] == 0
    # a whole-feed read (no view) sees everything
    assert bloom.card(_TWO_TOWER)["spaces"] == 3


def test_detail_view_filters_apartments_and_plant():
    da = bloom.detail(_TWO_TOWER, view="354", plant_tag="A")
    db_ = bloom.detail(_TWO_TOWER, view="358", plant_tag="B")
    assert {ap["name"] for ap in da["apartments"]} == {"354 - 1A", "354 - 5E"}
    assert {ap["name"] for ap in db_["apartments"]} == {"358 - 3E"}
    assert any(ap["rh"] == 55 for ap in da["apartments"])          # humidity joined
    names_a = {p["name"] for p in da["plant"]}
    names_b = {p["name"] for p in db_["plant"]}
    assert "PDT BLDG A SUPPLY" in names_a and "PDT BLDG B SUPPLY" not in names_a
    assert "PDT BLDG B SUPPLY" in names_b and "PDT BLDG A SUPPLY" not in names_b
    assert "PDT DIFFERENTIAL PRESSURE" in names_a and "PDT DIFFERENTIAL PRESSURE" in names_b  # shared
    assert da["alarms"] and not db_["alarms"]
