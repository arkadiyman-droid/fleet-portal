"""
tests/test_bloom_settings_db.py — the "Change settings" page of a Bloom heating
building (Postgres tier)

The administrator changes a building's setpoints from its page. The page reads
the building live, shows the change old -> new, and sends it only on a second,
confirming submit — and only if it is still the change that was shown. Bloom
is faked here: a live feed for one building and a put_points that changes it.
"""
import datetime as dt
import re

import pytest

from tests.conftest import requires_postgres
from tests.test_bloom_settings import FORM, building
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

pytestmark = requires_postgres

REASON = "Tenants cold at night"


@pytest.fixture
def feed(monkeypatch, world, session):
    """Bloom, faked, for the world's Berry asset. Returns {"b": the live
    building, "puts": every write, "mode": "ok" | "refuse" | "down" | "slow"}."""
    import time
    import requests
    import app.models as m
    from app import bloom, routes
    state = {"b": building(), "puts": [], "mode": "ok", "reads": 0}
    state["b"]["name"], state["b"]["locid"] = "440 Berry St", "440-Berry-St"

    def live(asset):
        state["reads"] += 1
        return (state["b"] if asset.id == world["asset_mine"] else None), None, "t"

    def put_points(account, sess, token, locid, points, note, timeout=20, bacnet=True):
        state["puts"].append({"locid": locid, "points": [(p["type"], p["pv"], p["originalPv"]) for p in points],
                              "note": note, "bacnet": bacnet, "timeout": timeout})
        if state["mode"] == "down":
            raise requests.HTTPError("502 Server Error: Bad Gateway for url: https://bloom.test/api/points/" + locid)
        if state["mode"] == "refuse":
            return {"success": False, "message": "not allowed for this login"}, token
        if state["mode"] != "slow":
            by_id = {s.get("_id"): s for s in state["b"]["sensors"]}
            for p in points:
                by_id[p["_id"]]["pv"] = p["pv"]
        return {"success": True, "data": {"result": "The change has been sent to the controller."}}, token

    monkeypatch.setattr(routes, "_bloom_live_asset", live)
    monkeypatch.setattr(bloom, "put_points", put_points)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    session.add(m.BloomStatus(asset_id=world["asset_mine"], polled_at=dt.datetime.utcnow(), ok=True,
                              last_ok_at=dt.datetime.utcnow(), data={"asset": building(), "alarms": []}))
    session.commit()
    return state


def _url(world):
    return f"/skyline/{world['asset_mine']}/settings"


def _digest(page):
    return re.search(r'name="digest" value="([0-9a-f]*)"', page).group(1)


def _confirm(page, **over):
    """The confirmation form of a preview page, as a browser would post it."""
    card = page[page.index("What will change at"):page.index('name="step" value="preview"')]
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', card))
    data["note"] = REASON
    data.update(over)
    return data


def test_the_page_shows_what_bloom_holds_and_sends_nothing(root, world, feed):
    r = root.get(_url(world))
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Change settings" in page and "440 Berry St" in page
    for label in ("Indoor setpoint, day", "Outdoor cutoff, night", "Heating season starts", "Secondary call for heat"):
        assert label in page
    assert 'name="ind-sp-day" value="72" min="50" max="80"' in page and "allowed 50° to 80°" in page
    assert '<option value="05:00" selected>5:00 AM</option>' in page and '<option value="22:00" selected>10:00 PM</option>' in page
    assert '<option value="10" selected>October</option>' in page and "in Bloom now: <b>May 31</b>" in page
    assert "Review changes" in page and "What will change" not in page and "None yet." in page
    assert feed["puts"] == []


def test_only_the_administrator_gets_the_page_or_its_link(root, pat, world, feed):
    assert pat.get(_url(world)).status_code == 403
    assert pat.post(_url(world), data={**FORM, "step": "send", "digest": "x", "note": REASON}).status_code == 403
    assert feed["puts"] == [] and feed["reads"] == 0
    detail = f"/skyline/{world['asset_mine']}"
    assert "Change settings…" in root.get(detail).get_data(as_text=True)
    seen = pat.get(detail)
    assert seen.status_code == 200 and "Change settings" not in seen.get_data(as_text=True)


def test_a_change_is_shown_before_it_can_be_sent(root, world, feed):
    import app.models as m
    page = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "70", "stop-tm-day": "22:30"}
                     ).get_data(as_text=True)
    assert "What will change at 440 Berry St" in page and "Send 2 changes to 440 Berry St" in page
    assert re.search(r"<td>Indoor setpoint, day</td><td>72°</td><td>→</td><td class=\"new\">70°</td>", page)
    assert re.search(r"<td>Night period starts</td><td>10:00 PM</td><td>→</td><td class=\"new\">10:30 PM</td>", page)
    assert page.count('<td class="new">') == 2 and len(_digest(page)) == 16
    assert feed["puts"] == [] and m.BloomSettingWrite.query.count() == 0      # a preview writes nothing, anywhere

    same = root.post(_url(world), data={**FORM, "step": "preview"}).get_data(as_text=True)
    assert "Nothing to send" in same and "What will change" not in same


def test_a_confirmed_change_goes_to_bloom_is_read_back_and_logged(root, world, feed):
    import app.models as m
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "70", "stop-tm-day": "22:30"}
                        ).get_data(as_text=True)
    page = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert len(feed["puts"]) == 1
    put = feed["puts"][0]
    assert put["locid"] == "440-Berry-St" and put["note"] == REASON
    assert put["bacnet"] is None                              # as Bloom's own Schedule screen: no bacnet flag
    assert sorted(put["points"]) == [("ind-sp-day", 70, 72), ("stop-tm-day", 2250, 2200)]
    assert "Sent, and confirmed." in page and "Indoor setpoint, day 72° → 70°" in page
    assert 'name="ind-sp-day" value="70"' in page and "in Bloom now: <b>70°</b>" in page      # the form shows the new truth
    assert "What will change" not in page
    w = m.BloomSettingWrite.query.one()
    assert w.asset_id == world["asset_mine"] and w.by == "the administrator" and w.ok and w.landed and w.note == REASON
    assert [(c["type"], c["old"], c["new"]) for c in w.changes] == [("stop-tm-day", 2200, 2250), ("ind-sp-day", 72, 70)]
    assert "sent · confirmed" in page and REASON in page

    again = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)     # the same confirmation, twice
    assert len(feed["puts"]) == 1 and m.BloomSettingWrite.query.count() == 1


def test_a_send_goes_only_if_it_is_still_the_change_that_was_shown(root, world, feed):
    import app.models as m
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "70", "ind-sp-night": "68"}
                        ).get_data(as_text=True)
    next(s for s in feed["b"]["sensors"] if s["type"] == "ind-sp-night")["pv"] = 68      # someone changed Bloom meanwhile
    page = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert "Nothing was sent." in page and "changed between the preview" in page
    assert "Send 1 change to 440 Berry St" in page            # the fresh preview, to confirm again
    assert feed["puts"] == [] and m.BloomSettingWrite.query.count() == 0

    page = root.post(_url(world), data=_confirm(page, note="  ")).get_data(as_text=True)
    assert "A reason is required" in page and feed["puts"] == []


def test_a_value_outside_blooms_limits_is_never_sent(root, world, feed):
    import app.models as m
    for step in ("preview", "send"):
        page = root.post(_url(world), data={**FORM, "step": step, "ind-sp-day": "85", "ind-sp-night": "68",
                                            "digest": "x", "note": REASON}).get_data(as_text=True)
        assert "Nothing can be sent yet." in page and "Bloom allows 50° to 80°" in page
        assert "What will change" not in page and 'name="ind-sp-day" value="85"' in page
    assert feed["puts"] == [] and m.BloomSettingWrite.query.count() == 0


def test_when_bloom_cannot_reach_the_building_it_says_so_and_logs_it(root, world, feed):
    import app.models as m
    preview = root.post(_url(world), data={**FORM, "step": "preview", "oat-sp-day": "60"}).get_data(as_text=True)
    feed["mode"] = "down"
    page = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert "Not changed." in page and "502 Server Error: Bad Gateway" in page
    w = m.BloomSettingWrite.query.one()
    assert not w.ok and w.landed is None and "502" in w.detail and "not taken" in page
    assert 'name="oat-sp-day" value="60"' in page and "in Bloom now: <b>55°</b>" in page      # what was asked is kept

    feed["mode"] = "refuse"
    page = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert "Not changed." in page and "not allowed for this login" in page
    assert m.BloomSettingWrite.query.count() == 2 and not any(w.ok for w in m.BloomSettingWrite.query.all())


def test_accepted_but_not_seen_yet_is_said_plainly(root, world, feed):
    import app.models as m
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "70"}).get_data(as_text=True)
    feed["mode"] = "slow"                                     # Bloom says yes, the value does not show yet
    page = root.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert "Sent, not yet confirmed." in page and "sent · unconfirmed" in page
    w = m.BloomSettingWrite.query.one()
    assert w.ok and w.landed is False


def test_a_building_bloom_does_not_list_can_change_nothing(root, world, feed):
    r = root.get(f"/skyline/{world['asset_other']}/settings")
    page = r.get_data(as_text=True)
    assert r.status_code == 200 and "Could not read this building from Bloom" in page and "Review changes" not in page
    r = root.post(f"/skyline/{world['asset_other']}/settings", data={**FORM, "step": "send", "digest": "x", "note": REASON})
    assert r.status_code == 200 and feed["puts"] == []
