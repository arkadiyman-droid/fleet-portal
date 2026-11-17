"""tests/test_home.py — the "My properties" page's grouping and totals (pure)."""
from app import home


def _b(prop_id, prop, name, status, client="Owner A", client_id=1):
    return {"client": client, "client_id": client_id, "prop": prop, "prop_id": prop_id,
            "entry": {"name": name, "source": "2010", "status": status, "out": 44, "sup": 150, "ret": 130,
                      "rt": None, "dhw": None, "spt": 70, "mode": None, "alarms": [], "url": "/live/1"}}


def _f(prop_id, name, on, total, fail=0, stale=False):
    return {"prop_id": prop_id, "name": name, "on": on, "total": total, "fail": fail, "stale": stale,
            "tone": "ok", "status": "running"}


def test_buildings_group_under_their_property_worst_first():
    out = home.assemble([_b(1, "Berry", "88 South 10 St", "ok"), _b(1, "Berry", "50 South 9 St", "crit"),
                         _b(1, "Berry", "440 Berry St", "dark"), _b(1, "Berry", "56 South 10 St", "heat"),
                         _b(2, "DCA", "18 Monroe St", "warn")], [])
    assert [o["name"] for o in out["owners"]] == ["Owner A"]
    berry, dca = out["owners"][0]["props"]
    assert (berry["name"], dca["name"]) == ("Berry", "DCA")
    assert [e["name"] for e in berry["buildings"]] == ["50 South 9 St", "56 South 10 St", "88 South 10 St", "440 Berry St"]
    assert berry["tally"] == {"buildings": 4, "crit": 1, "warn": 0, "heat": 1, "ok": 1, "dark": 1,
                              "fan_sites": 0, "fan_dark": 0, "fans": 0, "fans_on": 0, "fans_down": 0}
    assert berry["buildings"][0]["tone_label"] == "Problem" and berry["buildings"][0]["source_label"] == "2010 controller"
    s = out["summary"]
    assert (s["properties"], s["buildings"], s["crit"], s["warn"], s["heat"], s["dark"]) == (2, 5, 1, 1, 1, 1)


def test_fans_count_only_buildings_that_are_reporting():
    out = home.assemble([_b(1, "Berry", "50 South 9 St", "ok")],
                        [_f(1, "50 South 9 St", 2, 3, fail=1), _f(1, "440 Berry St", 9, 9, stale=True)])
    t = out["owners"][0]["props"][0]["tally"]
    assert (t["fan_sites"], t["fan_dark"], t["fans"], t["fans_on"], t["fans_down"]) == (2, 1, 3, 2, 1)
    assert [f["name"] for f in out["owners"][0]["props"][0]["fans"]] == ["50 South 9 St", "440 Berry St"]


def test_a_fan_site_is_never_shown_under_a_property_that_is_not_on_the_page():
    out = home.assemble([_b(1, "Berry", "50 South 9 St", "ok")], [_f(2, "18 Monroe St", 2, 2), _f(None, "Loose", 1, 1)])
    assert out["owners"][0]["props"][0]["fans"] == [] and out["summary"]["fan_sites"] == 0


def test_owners_are_separate_and_nothing_shared_is_empty():
    out = home.assemble([_b(1, "Berry", "A", "ok"), _b(9, "Other", "B", "ok", client="Owner B", client_id=2)], [])
    assert [o["name"] for o in out["owners"]] == ["Owner A", "Owner B"]
    empty = home.assemble([], [_f(1, "x", 1, 1)])
    assert empty["owners"] == [] and empty["summary"]["buildings"] == 0 and empty["summary"]["properties"] == 0


# ── "How to read this page": the guide for someone seeing it for the first time ──

def test_the_guide_explains_every_status_the_page_can_show():
    # every heating tone has a row, in the page's own worst-first order, under the label the pill carries
    assert [r["key"] for r in home.HEAT_GUIDE] == sorted(home.TONE_LABEL, key=home.TONE_ORDER.get)
    assert all(r["label"] == home.TONE_LABEL[r["key"]] for r in home.HEAT_GUIDE)
    for r in home.HEAT_GUIDE + home.FAN_GUIDE:
        assert r["means"].endswith(".") and r["do"].endswith(".") and r["css"] in ("crit", "warn", "heat", "ok", "dark")
    # nobody is named in the page: who to tell is "the person who gave you this access"
    assert all("@" not in r["do"] for r in home.HEAT_GUIDE + home.FAN_GUIDE)


def test_a_fan_building_gets_the_worst_thing_true_of_it():
    assert home.fan_key({"stale": True, "fail": 2, "on": 3}) == "stale"      # not reporting: nothing else is known
    assert home.fan_key({"fail": 1, "hand": 1, "on": 3}) == "fail"
    assert home.fan_key({"hand": 1, "on": 3}) == "hand"
    assert home.fan_key({"on": 3}) == "on" and home.fan_key({"on": 0}) == "off"
    assert {home.fan_key(f) for f in ({"stale": 1}, {"fail": 1}, {"hand": 1}, {"on": 1}, {})} == {r["key"] for r in home.FAN_GUIDE}


def test_every_row_carries_its_pill_and_what_the_pill_means():
    out = home.assemble([_b(1, "Berry", "50 South 9 St", "heat"), _b(1, "Berry", "440 Berry St", "dark")],
                        [_f(1, "50 South 9 St", 2, 3, fail=1), _f(1, "440 Berry St", 0, 4)])
    p = out["owners"][0]["props"][0]
    heat, dark = p["buildings"]
    assert heat["tip"].startswith("Normal.") and "cannot reach" in dark["tip"]
    down, off = p["fans"]
    assert (down["pill"], down["pill_css"]) == ("Fan down", "crit") and "not running" in down["tip"]
    assert (off["pill"], off["pill_css"]) == ("Off", "dark")
