"""
tests/test_fans_plan_db.py — the fan plan's hours, edited on the Fans page
(Postgres tier).

The administrator clicks hours of a temperature band on or off and saves. That
changes the plan in the portal — every reader of the plan gets the saved hours
— and writes nothing to Bloom; where the portal loaded a building's schedule
slots before, the page then says the new hours are waiting to be sent.
"""
import datetime as dt

import pytest

from app import bloom_fans as bf
from tests.conftest import requires_postgres
from tests.test_bloom_fans import LATE_COLD, LOADED, PLAN, _eq, _live
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

pytestmark = requires_postgres

HOURS = {b["key"]: b["bitstr"] for b in PLAN}
EDITED = {**HOURS, "cold": LATE_COLD}
WAITING = "New hours are waiting to be sent to Bloom"


def test_the_administrator_edits_the_plans_hours(root, world, app):
    import app.models as m
    page = root.get("/fans").get_data(as_text=True)
    assert 'class="strip edit"' in page and f'data-saved="{HOURS["cold"]}"' in page and "Save the plan" in page
    assert "Put the original hours back" not in page and m.BloomFanPlan.query.count() == 0

    r = root.post("/fans/plan", data=EDITED)
    assert r.status_code == 302 and r.headers["Location"].endswith("/fans")
    assert m.BloomFanPlan.query.one().hours == EDITED
    page = root.get("/fans").get_data(as_text=True)
    assert "Plan saved: 35° and below is now on 7am–10am, 6pm–9pm." in page and "Nothing has been sent to Bloom yet" in page
    assert f'data-saved="{LATE_COLD}"' in page and "on 7am–10am, 6pm–9pm · 6 h a day" in page
    assert "Put the original hours back" in page and "Hours last changed by" in page
    # every reader of the plan — the pages and the poller — now gets the saved hours
    assert [b["ranges"] for b in bf.current_plan(app.config)] == ["7am–10am, 6pm–9pm", PLAN[1]["ranges"], "all day"]

    root.post("/fans/plan", data=EDITED)                      # the same hours again: nothing new is recorded
    assert m.BloomFanPlan.query.count() == 1
    for broken in ({"cold": "1" * 24}, {**EDITED, "mild": "1" * 23}, {**EDITED, "free": "yes"}, {}):
        root.post("/fans/plan", data=broken)                  # hours that did not arrive whole: refused
    assert m.BloomFanPlan.query.count() == 1
    assert "The plan was not saved" in root.get("/fans").get_data(as_text=True)

    root.post("/fans/plan", data={"action": "reset"})         # the built-in hours back, as one more recorded change
    assert m.BloomFanPlan.query.count() == 2 and bf.current_plan(app.config) == PLAN
    assert "Put the original hours back" not in root.get("/fans").get_data(as_text=True)


def test_only_the_administrator_can_change_the_plan(pat, world):
    import app.models as m
    assert pat.post("/fans/plan", data=EDITED).status_code == 403
    assert pat.post("/fans/plan", data={"action": "reset"}).status_code == 403
    page = pat.get("/fans").get_data(as_text=True)
    assert page and 'class="strip edit"' not in page and "Save the plan" not in page
    assert m.BloomFanPlan.query.count() == 0


def test_new_hours_wait_to_be_sent_where_the_portal_loaded_the_slots(root, world, session):
    import app.models as m
    # Berry's fan building, reporting, with slots 3-5 holding the built-in hours and its group on slot 4
    raw = _live(LOADED, assign=4)
    raw["equipment"] = [{"type": "fan", "key": "exhaust-1", "name": "Exhaust Fan 1"},
                        {"type": "fanGroup", "key": "fg-1", "name": "Exhaust Fan Group 1", "fans": ["exhaust-1"]}]
    raw["sensors"].append({"type": "fan-rs", "pv": 1, "assoc": _eq("exhaust-1"),
                           "pollTs": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"})
    site = session.get(m.BloomFanSite, world["fan_mine"])
    site.data = bf.trim(raw)
    session.commit()

    root.post("/fans/plan", data=EDITED)
    # never loaded by the portal: its slots are not the portal's to call behind
    assert WAITING not in root.get("/fans").get_data(as_text=True)

    hours = [{"type": "gen-sched", "name": "3::7", "old": 0, "new": 1}]
    move = [{"type": "gen-sched-assign", "name": "fg-1", "old": 1, "new": 4}]
    session.add_all([m.BloomFanWrite(site_id=world["fan_mine"], by="x", changes=hours, ok=True),
                     m.BloomFanWrite(site_id=world["fan_dca"], by="x", changes=hours, ok=False),     # refused
                     m.BloomFanWrite(site_id=world["fan_other"], by="x", changes=move, ok=True)])    # a switch only
    session.commit()
    assert bf.loaded_site_ids() == {world["fan_mine"]}
    page = root.get("/fans").get_data(as_text=True)
    assert WAITING in page and "Berry (1 building)" in page and "NEW HOURS TO SEND" in page
    assert "loaded at 1 building." in page

    root.post("/fans/plan", data={"action": "reset"})         # the plan matches the slots again: nothing waits
    page = root.get("/fans").get_data(as_text=True)
    assert WAITING not in page and "NEW HOURS TO SEND" not in page


# ── /fans/send: the plan's hours to the properties ticked ──────────────────────

def _reporting(slot_bits, assign, name, key):
    """A full live building as /api/assets gives it, with one fan heard from just now."""
    raw = _live(slot_bits, assign=assign)
    raw["name"], raw["locid"] = name, key
    raw["equipment"] = [{"type": "fan", "key": "exhaust-1", "name": "Exhaust Fan 1"},
                        {"type": "fanGroup", "key": "fg-1", "name": "Exhaust Fan Group 1", "fans": ["exhaust-1"]}]
    raw["sensors"].append({"type": "fan-rs", "pv": 1, "assoc": _eq("exhaust-1"),
                           "pollTs": dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"})
    return raw


@pytest.fixture
def bloom_feed(monkeypatch, world, session):
    """Bloom, faked: a live feed holding Berry's and DCA's fan buildings (loaded
    with the built-in hours, groups on slot 4) and a put_points that changes
    it. Returns {"feed", "puts"}; set feed["refuse"] to a locid to have Bloom
    refuse that building."""
    import time
    import app.models as m
    from app import bloom, routes
    acct = session.get(m.BloomFanSite, world["fan_mine"]).account
    feed = {"by": {"440-Berry-St": _reporting(LOADED, 4, "440 Berry St", "440-Berry-St"),
                   "18-Monroe-St": _reporting(LOADED, 4, "18 Monroe St", "18-Monroe-St")},
            "refuse": None}
    puts = []

    def live_accounts(sites):
        return {acct.id: {"by": feed["by"], "sess": None, "tok": "t"}} if sites else {}

    def put_points(account, sess, token, locid, points, note, timeout=20):
        puts.append((locid, [(p["name"], p["pv"]) for p in points], note))
        if feed["refuse"] == locid:
            return {"success": False, "message": "controller offline"}, token
        by_id = {s.get("_id"): s for s in feed["by"][locid]["sensors"]}
        for p in points:
            by_id[p["_id"]]["pv"] = p["pv"]
        return {"success": True, "data": {"result": "The change has been sent to the controller."}}, token

    monkeypatch.setattr(routes, "_fans_live_accounts", live_accounts)
    monkeypatch.setattr(bloom, "put_points", put_points)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session.add_all([m.BloomFanWrite(site_id=world[k], by="x", ok=True,                 # the portal loaded both before
                                     changes=[{"type": "gen-sched", "name": "3::7", "old": 0, "new": 1}])
                     for k in ("fan_mine", "fan_dca")])
    session.commit()
    return {"feed": feed, "puts": puts}


def _digest(page, site_id):
    import re
    return re.search(rf'data-site="{site_id}" data-digest="([0-9a-f]+)"', page).group(1)


def test_the_plans_hours_are_sent_to_the_properties_ticked(root, world, bloom_feed, session):
    import app.models as m
    w, puts = world, bloom_feed["puts"]
    page = root.get("/fans/send").get_data(as_text=True)
    assert "nothing to send" in page and 'data-site="' not in page          # the slots hold the plan: nothing to tick

    root.post("/fans/plan", data=EDITED)                                    # one more evening hour in the cold band
    page = root.get("/fans/send").get_data(as_text=True)
    assert "7am–10am, 6pm–9pm · 6 h a day" in page                          # the hours being sent
    for prop in ("Berry", "DCA", "Other"):
        assert f'data-prop="{prop}"' in page
    assert "77 Nowhere Rd" not in page                                      # not filed under a property
    assert "<b>1 change</b> to send at <b>1</b> of 1 building" in page and "not in Bloom" in page   # Other: not in the feed
    assert "This changes how fans run" not in page                          # groups are on slot 4; slot 3 is what changes
    mine, dca = _digest(page, w["fan_mine"]), _digest(page, w["fan_dca"])

    # Berry only: one building, the one hour that changed, with the reason; DCA is not touched
    before = m.BloomFanWrite.query.count()
    r = root.post("/fans/send", data={"note": "one more evening hour", "site": [f"{w['fan_mine']}:{mine}"]})
    assert r.status_code == 200
    assert r.get_json()["results"] == {str(w["fan_mine"]): {"state": "sent", "n": 1, "landed": True,
                                                            "detail": "The change has been sent to the controller."}}
    assert puts == [("440-Berry-St", [("3::20", 1)], "one more evening hour")]
    row = m.BloomFanWrite.query.order_by(m.BloomFanWrite.id.desc()).first()
    assert m.BloomFanWrite.query.count() == before + 1 and row.site_id == w["fan_mine"] and row.ok and row.landed
    assert row.note == "one more evening hour" and row.changes == [{"type": "gen-sched", "name": "3::20", "old": 0, "new": 1}]
    page = root.get("/fans/send").get_data(as_text=True)
    assert f'data-site="{w["fan_mine"]}"' not in page and f'data-site="{w["fan_dca"]}"' in page

    # sending the same building again finds it in place and sends nothing
    again = root.post("/fans/send", data={"note": "again", "site": [f"{w['fan_mine']}:{mine}"]}).get_json()["results"]
    assert again[str(w["fan_mine"])]["state"] == "inplace" and len(puts) == 1


def test_a_send_goes_only_if_it_is_still_the_change_that_was_shown(root, world, bloom_feed, session):
    import app.models as m
    w, puts = world, bloom_feed["puts"]
    root.post("/fans/plan", data=EDITED)
    page = root.get("/fans/send").get_data(as_text=True)
    mine, dca = _digest(page, w["fan_mine"]), _digest(page, w["fan_dca"])
    before = m.BloomFanWrite.query.count()

    assert root.post("/fans/send", data={"note": "", "site": [f"{w['fan_mine']}:{mine}"]}).status_code == 400   # no reason
    assert root.post("/fans/send", data={"note": "x"}).status_code == 400                                         # nothing ticked
    many = [f"{i}:x" for i in range(1, 7)]
    assert root.post("/fans/send", data={"note": "x", "site": many}).status_code == 400                           # too many at once
    assert not puts and m.BloomFanWrite.query.count() == before

    # Bloom's values moved after the page was read: not sent. A building Bloom refuses is logged as refused.
    bloom_feed["feed"]["refuse"] = "18-Monroe-St"
    res = root.post("/fans/send", data={"note": "x", "site": [f"{w['fan_mine']}:stale-digest", f"{w['fan_dca']}:{dca}",
                                                              f"{w['fan_other']}:x", "999999:x"]}).get_json()["results"]
    assert res[str(w["fan_mine"])] == {"state": "changed"}
    assert res[str(w["fan_dca"])]["state"] == "refused" and "controller offline" in res[str(w["fan_dca"])]["detail"]
    assert res[str(w["fan_other"])]["state"] == "gone" and res["999999"]["state"] == "gone"
    assert [p[0] for p in puts] == ["18-Monroe-St"]
    row = m.BloomFanWrite.query.order_by(m.BloomFanWrite.id.desc()).first()
    assert m.BloomFanWrite.query.count() == before + 1 and row.site_id == w["fan_dca"] and not row.ok and row.landed is None


def test_a_send_says_when_it_changes_how_fans_run(root, world, bloom_feed):
    bloom_feed["feed"]["by"]["440-Berry-St"] = _reporting(LOADED, 3, "440 Berry St", "440-Berry-St")   # groups ON the cold slot
    root.post("/fans/plan", data=EDITED)
    page = root.get("/fans/send").get_data(as_text=True)
    assert "This changes how fans run" in page and "440 Berry St slot 3 (7am–10am, 6pm–8pm → <b>7am–10am, 6pm–9pm</b>)" in page
    assert f'data-site="{world["fan_mine"]}"' in page and 'data-live="1"' in page


def test_only_the_administrator_can_send(pat, world, bloom_feed):
    assert pat.get("/fans/send").status_code == 403
    assert pat.post("/fans/send", data={"note": "x", "site": [f"{world['fan_mine']}:x"]}).status_code == 403
    assert not bloom_feed["puts"]
