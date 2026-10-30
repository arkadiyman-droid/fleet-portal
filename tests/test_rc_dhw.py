"""Unit tests for the Reliable Controls DHW scrape value parsing (app/rc_dhw.py).

Only the pure parsing is covered here (no headless browser / DB); the gel maps
below are real captures from the Whitney Young DHW System A / B graphics.
"""
from app import rc_dhw


# Real gel captures (Tower A / Tower B share the graphic template).
_TOWER_A = {
    "gel_208": "123.8 °F", "gel_152": "115.7 °F", "gel_310": "Yes",
    "gel_320": "Normal", "gel_324": "Normal", "gel_328": "Normal", "gel_332": "Normal",
    "gel_220": "0.0 GPM-D", "gel_356": "13:00:00", "gel_284": "2.6 Hours",
}
_TOWER_B = {
    "gel_208": "124.0 °F", "gel_152": "115.6 °F", "gel_310": "Yes",
    "gel_320": "Normal", "gel_324": "Normal", "gel_328": "Normal", "gel_332": "Normal",
}


def test_num_extracts_leading_number_with_units():
    assert rc_dhw._num("124.0 °F") == 124.0
    assert rc_dhw._num("100.0 %") == 100.0
    assert rc_dhw._num("8.5 Hours") == 8.5
    assert rc_dhw._num("0.0 GPM-D") == 0.0
    assert rc_dhw._num("-3.2 °F") == -3.2
    assert rc_dhw._num("") is None
    assert rc_dhw._num(None) is None


def test_is_on_and_is_alarm():
    assert rc_dhw._is_on("Yes") is True
    assert rc_dhw._is_on("On") is True
    assert rc_dhw._is_on("No") is False
    assert rc_dhw._is_on("Off") is False
    assert rc_dhw._is_on("whatever") is None
    assert rc_dhw._is_alarm("Normal") is False
    assert rc_dhw._is_alarm("Fault") is True
    assert rc_dhw._is_alarm("") is False


def test_parse_gels_healthy_tower():
    for gels in (_TOWER_A, _TOWER_B):
        p = rc_dhw.parse_gels(gels)
        assert 100 < p["supply_f"] < 160
        assert 100 < p["return_f"] < 160
        assert p["supply_f"] > p["return_f"]          # supply hotter than return
        assert p["pump_running"] is True
        assert p["alarm"] is False


def test_parse_gels_flags_a_pump_alarm():
    gels = dict(_TOWER_A, gel_332="Fault")
    assert rc_dhw.parse_gels(gels)["alarm"] is True


def test_parse_gels_alarm_none_when_no_cells_present():
    # a partial/failed read with no alarm cells -> alarm unknown, not False
    assert rc_dhw.parse_gels({"gel_208": "120.0 °F"})["alarm"] is None
