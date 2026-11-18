"""
tests/test_fans_plan_db.py — the fan plan's hours, edited on the Fans page
(Postgres tier).

The administrator clicks hours of a temperature band on or off and saves. That
changes the plan in the portal — every reader of the plan gets the saved hours
— and writes nothing to Bloom; where the portal loaded a building's schedule
slots before, the page then says the new hours are waiting to be sent.
"""
import datetime as dt

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
