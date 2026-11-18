"""
tests/test_fan_control_db.py — a person the administrator lets run one
property's fans (Postgres tier).

A second tick beside a property on the person's permissions page opens that
property's 'Schedules & switching' page to them, and nothing else: not another
property, not the plan's hours, not the send-to-several page. Bloom is faked.
"""
import re

from tests.conftest import requires_postgres
from tests.test_fans_plan_db import EDITED, bloom_feed          # noqa: F401  (fixture)
from tests.test_viewer_access_db import pat, root, world        # noqa: F401  (fixtures)

pytestmark = requires_postgres


def _give(root, w, view, fans, offered=("berry", "dca", "other")):
    return root.post(f"/users/{w['viewer']}/properties",
                     data={"property_id": [w[k] for k in view], "fan_offered": [w[k] for k in offered],
                           "fan_property_id": [w[k] for k in fans]})


def test_a_tick_lets_one_person_run_one_propertys_fans(root, pat, world, bloom_feed):
    import app.models as m
    w, puts = world, bloom_feed["puts"]
    assert pat.get("/fans/property?name=Berry").status_code == 403
    page = root.get(f"/users/{w['viewer']}/permissions").get_data(as_text=True)
    assert page.count('name="fan_property_id"') == 3 and "may run the fans here" in page and "Save access" in page

    assert _give(root, w, ["berry"], ["berry"]).status_code == 302
    fc = m.FanControl.query.one()
    assert (fc.user_id, fc.property_id, fc.by) == (w["viewer"], w["berry"], "the administrator")
    page = root.get(f"/users/{w['viewer']}/permissions").get_data(as_text=True)
    assert "Pat Viewer can now run the fans at Berry." in page
    assert re.search(rf'name="fan_property_id" value="{w["berry"]}"[^>]* checked', page)
    assert "runs fans at Berry" in root.get("/users").get_data(as_text=True)

    # the person: their property's switching page, and nothing wider
    fans = pat.get("/fans").get_data(as_text=True)
    assert "Schedules &amp; switching" in fans and "view, and fan control" in fans
    for admin_only in ('class="strip edit"', "Save the plan", "Send to properties", "Push plan schedules", "Raw Bloom values"):
        assert admin_only not in fans, admin_only
    home = pat.get("/home").get_data(as_text=True)
    assert "Fan schedules and switching" in home
    assert "Fans you can run" in pat.get("/account").get_data(as_text=True)
    assert pat.get("/fans/property?name=Berry").status_code == 200
    for name in ("DCA", "Other", "No Such Place", ""):
        assert pat.get(f"/fans/property?name={name}").status_code == 403, name
    assert pat.post("/fans/plan", data=EDITED).status_code == 403
    assert pat.get("/fans/send").status_code == 403
    assert pat.post("/fans/send", data={"note": "x", "site": [f"{w['fan_mine']}:x"]}).status_code == 403
    assert pat.get(f"/fans/{w['fan_mine']}/push").status_code == 403
    assert pat.get(f"/fans/{w['fan_mine']}/raw").status_code == 403
    assert pat.get("/users").status_code == 403

    # automatic switching, on their property only, under their own login
    assert pat.post("/fans/property", data={"name": "DCA", "action": "auto_on"}).status_code == 403
    r = pat.post("/fans/property", data={"name": "Berry", "action": "auto_on"})
    assert r.status_code == 200 and "Automatic switching is now ON for Berry" in r.get_data(as_text=True)
    auto = m.BloomFanAuto.query.one()
    assert (auto.prop, auto.enabled, auto.by) == ("Berry", True, "pat")

    # the administrator changes the plan; the person sends the new hour to their property
    root.post("/fans/plan", data=EDITED)
    page = pat.get("/fans/property?name=Berry").get_data(as_text=True)
    digest = re.search(r'name="action" value="load">\s*<input type="hidden" name="digest" value="([0-9a-f]+)"', page).group(1)
    before = m.BloomFanWrite.query.count()
    r = pat.post("/fans/property", data={"name": "Berry", "action": "load", "digest": digest, "note": "one more evening hour"})
    assert r.status_code == 200 and "Schedules sent" in r.get_data(as_text=True)
    assert puts == [("440-Berry-St", [("3::20", 1)], "one more evening hour")]
    row = m.BloomFanWrite.query.order_by(m.BloomFanWrite.id.desc()).first()
    assert m.BloomFanWrite.query.count() == before + 1
    assert (row.site_id, row.by, row.ok, row.landed) == (w["fan_mine"], "pat", True, True)


def test_fan_control_goes_with_seeing_the_property(root, pat, world, bloom_feed):
    import app.models as m
    w = world
    # a property the person cannot see: refused, and said so
    _give(root, w, ["berry"], ["dca"])
    assert m.FanControl.query.count() == 0
    assert "Fan control was not given at DCA" in root.get(f"/users/{w['viewer']}/permissions").get_data(as_text=True)
    # ticking the property and its fans together works
    _give(root, w, ["berry", "dca"], ["dca"])
    assert [fc.property_id for fc in m.FanControl.query.all()] == [w["dca"]]
    assert pat.get("/fans/property?name=DCA").status_code == 200 and pat.get("/fans/property?name=Other").status_code == 403

    # a save that carries no fan ticks at all (an older form) leaves fan control alone
    root.post(f"/users/{w['viewer']}/properties", data={"property_id": [w["berry"], w["dca"]]})
    assert m.FanControl.query.count() == 1

    # taking the view away takes the fans with it, and giving the view back does not bring them back
    _give(root, w, ["berry"], ["dca"])
    assert m.FanControl.query.count() == 0
    page = root.get(f"/users/{w['viewer']}/permissions").get_data(as_text=True)
    assert "Fan control taken away at DCA" in page
    _give(root, w, ["berry", "dca"], [], offered=())
    assert m.FanControl.query.count() == 0

    # the same when the grant is revoked from the list below
    _give(root, w, ["berry"], ["berry"])
    assert m.FanControl.query.count() == 1
    grant = m.Permission.query.filter_by(user_id=w["viewer"], scope_type="property", scope_id=w["berry"]).one()
    root.post(f"/users/{w['viewer']}/permissions/{grant.id}/revoke")
    assert m.FanControl.query.count() == 0
    assert "Fan control at Berry went with it" in root.get(f"/users/{w['viewer']}/permissions").get_data(as_text=True)

    # unticking only the fans keeps the view
    _give(root, w, ["berry"], ["berry"])
    _give(root, w, ["berry"], [])
    assert m.FanControl.query.count() == 0
    assert m.Permission.query.filter_by(user_id=w["viewer"], scope_type="property", scope_id=w["berry"]).count() == 1
    assert pat.get("/fans/property?name=Berry").status_code == 403


def test_a_tick_alone_never_opens_a_property_the_person_cannot_see(pat, world, session, bloom_feed):
    import app.models as m
    w = world
    # a fan tick with no view behind it (put there by hand) counts for nothing
    session.add(m.FanControl(user_id=w["viewer"], property_id=w["dca"], by="x"))
    session.commit()
    assert pat.get("/fans/property?name=DCA").status_code == 403
    assert pat.post("/fans/property", data={"name": "DCA", "action": "auto_on"}).status_code == 403
    assert "Schedules &amp; switching" not in pat.get("/fans").get_data(as_text=True)
    assert m.BloomFanAuto.query.count() == 0 and not bloom_feed["puts"]


def test_seeing_as_a_fan_controller_changes_nothing(root, world, bloom_feed):
    import app.models as m
    w = world
    _give(root, w, ["berry"], ["berry"])
    root.post(f"/view-as/{w['viewer']}")
    assert root.get("/fans/property?name=Berry").status_code == 200          # the administrator sees what they see
    assert root.get("/fans/property?name=DCA").status_code == 403
    r = root.post("/fans/property", data={"name": "Berry", "action": "auto_on"})
    assert r.status_code == 303 and m.BloomFanAuto.query.count() == 0 and not bloom_feed["puts"]
