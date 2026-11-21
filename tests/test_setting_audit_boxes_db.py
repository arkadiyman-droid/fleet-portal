"""
tests/test_setting_audit_boxes_db.py — the settings ledger for the 2010 oBIX
boxes, the snapshot every kind of box can use, and the per-person settings
tick (Postgres tier; the pure tests first)

A 2010 box keeps its settings in eight oBIX objects the status poll does not
read. Each box has them read in its turn, about once an hour, and compared
with the snapshot stored for it. A person ticked for a property may change
that property's settings under their own name.
"""
import datetime as dt

from app import setting_audit as sa_
from tests.conftest import requires_postgres
from tests.test_bloom_settings import FORM
from tests.test_bloom_settings_db import REASON, _confirm, _url, feed      # noqa: F401  (fixture)
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

NOW = dt.datetime(2026, 11, 21, 0, 30, 0)


def box_settings(**change):
    """What one read of a 2010 box's settings objects brings back (cut down)."""
    s = {"emsSettings": {"serverTime": "2026-11-21T00:11:26Z", "dayStartHour": "6", "nightStartHour": "22",
                         "insideDaySetpoint": "72", "insideNightSetpoint": "71", "outsideDaySetpoint": "55",
                         "outsideNightSetpoint": "50", "mixHighTempSetpoint": "140", "wrlsSensorTimeout": "240",
                         "extOutsideTemp": "56", "leadBoiler": "0"},
         "emsConfig": {"serverTime": "2026-11-21T00:11:29Z", "heatControlType": "13", "numBoilers": "1",
                       "emsControlsBoiler1": "true", "relayAssignment2": "2"},
         "DITs": {"serverTime": "x", "weekdayDITHour5": "71", "weekdayDITHour22": "70"},
         "hotWaterHeat": {"serverTime": "x", "daySetpointforT5x0": "180", "valveMaxOpen": "80"}}
    for key, v in change.items():
        obj, field = key.split("__")
        s[obj][field] = v
    return s


# ── pure ─────────────────────────────────────────────────────────────────────

def test_a_boxs_clock_stamps_and_self_moving_values_are_not_settings():
    snap = sa_.legacy_snapshot(box_settings())
    assert snap["emsSettings.insideDaySetpoint"] == "72" and snap["DITs.weekdayDITHour5"] == "71"
    assert not any(k.endswith((".serverTime", ".extOutsideTemp", ".leadBoiler")) for k in snap)
    later = sa_.legacy_snapshot(box_settings(emsSettings__extOutsideTemp="31", emsSettings__serverTime="later",
                                             emsSettings__leadBoiler="1"))
    assert sa_.diff(snap, later) == []                       # the weather feed and the clock changed; no setting did
    assert sa_.legacy_snapshot(None) == {} and sa_.legacy_snapshot({"emsSettings": None}) == {}


def test_box_settings_read_in_plain_words():
    d = sa_.legacy_describe
    assert d("emsSettings.insideNightSetpoint", "71") == ("Indoor setpoint, night", "71°")
    assert d("emsSettings.dayStartHour", "6") == ("Day period starts", "6 AM")
    assert d("emsSettings.nightStartHour", "22") == ("Night period starts", "10 PM")
    assert d("DITs.weekdayDITHour5", "71") == ("Indoor target, weekdays, 5 AM", "71°")
    assert d("aquastat.daqHour13", "180") == ("Aquastat target, 1 PM", "180°")
    assert d("hotWaterHeat.daySetpointforT5x0", "180") == ("Supply water table, day, step 1", "180°")
    assert d("emsConfig.emsControlsBoiler1", "true") == ("Ems Controls Boiler 1 (configuration)", "on")
    assert d("emsConfig.heatControlType", "13") == ("Heating system type", "13")
    assert d("somethingNew.futureField", "7") == ("Future Field (somethingNew)", "7")      # an unknown field still reads


def test_a_cycles_share_brings_every_box_round_in_time():
    assert sa_.share(186, 60, 300) == 17                      # 186 boxes hourly, a cycle every 5 min: 15.5 and a bit
    assert sa_.share(186, 60, 300) * 12 >= 186
    assert sa_.share(1, 5, 60) >= 1 and sa_.share(186, 0, 300) == 0 and sa_.share(0, 60, 300) == 0


@requires_postgres
def test_the_boxes_read_longest_ago_go_first_and_no_cycle_reads_them_all(world, session):
    import app.models as m
    ids = list(range(1, 41))
    t0 = NOW
    assert sa_.due("legacy", ids, 0, t0) == set()             # switched off
    first = sa_.due("legacy", ids, 60, t0, limit=6)
    assert first == {1, 2, 3, 4, 5, 6}                        # never read: in order, a share at a time
    for i in first:
        sa_.track("legacy", i, None, f"box {i}", {"emsSettings.x": "1"}, sa_.legacy_describe, t0)
    sa_.touch("legacy", 7, t0)                                # asked, nothing came back: it has had its turn
    session.commit()
    nxt = sa_.due("legacy", ids, 60, t0 + dt.timedelta(minutes=5), limit=6)
    assert nxt == {8, 9, 10, 11, 12, 13}                      # the read ones and the failed one wait their hour
    assert m.SettingSnapshot.query.filter_by(source="legacy", source_id=7).one().data == {}
    hour = t0 + dt.timedelta(minutes=61)
    assert sa_.due("legacy", list(range(1, 8)), 60, hour) == {1, 2, 3, 4, 5, 6, 7}      # an hour on: due again
    assert sa_.due("echotherm", [1], 5, hour) == {1}          # another kind of box has its own queue
    # a box that only ever failed takes its first good read as the baseline, not as changes
    assert sa_.track("legacy", 7, None, "box 7", {"emsSettings.x": "1"}, sa_.legacy_describe, hour) == 0
    session.commit()
    assert m.SettingChange.query.count() == 0


# ── any kind of box: the stored snapshot ─────────────────────────────────────

def _rows():
    import app.models as m
    return m.SettingChange.query.order_by(m.SettingChange.id).all()


@requires_postgres
def test_the_first_read_is_a_baseline_and_a_later_difference_is_a_change(world, session):
    import app.models as m
    args = ("legacy", world["box_mine"], None, "50 South 9 St")
    assert sa_.track(*args, sa_.legacy_snapshot(box_settings()), sa_.legacy_describe, NOW) == 0
    session.commit()
    assert _rows() == [] and m.SettingSnapshot.query.count() == 1

    hour = NOW + dt.timedelta(hours=1)
    assert sa_.track(*args, sa_.legacy_snapshot(box_settings()), sa_.legacy_describe, hour) == 0       # nobody touched it
    changed = box_settings(emsSettings__insideNightSetpoint="68", DITs__weekdayDITHour5="69",
                           emsSettings__extOutsideTemp="20")
    assert sa_.track(*args, sa_.legacy_snapshot(changed), sa_.legacy_describe, hour + dt.timedelta(hours=1)) == 2
    session.commit()
    got = sorted((r.label, r.old_text, r.new_text, r.origin, r.source) for r in _rows())
    assert got == [("Indoor setpoint, night", "71°", "68°", "outside", "legacy"),
                   ("Indoor target, weekdays, 5 AM", "71°", "69°", "outside", "legacy")]
    assert "the vendor's app, or at the box" in sa_.who(_rows()[0])
    assert sa_.track(*args, sa_.legacy_snapshot(changed), sa_.legacy_describe, hour + dt.timedelta(hours=2)) == 0
    session.commit()
    assert len(_rows()) == 2 and m.SettingSnapshot.query.count() == 1


@requires_postgres
def test_a_read_that_brought_back_only_part_never_looks_like_a_change(world, session):
    args = ("legacy", world["box_mine"], None, "50 South 9 St")
    sa_.track(*args, sa_.legacy_snapshot(box_settings()), sa_.legacy_describe, NOW)
    part = {"emsSettings": box_settings()["emsSettings"]}                                  # the other objects timed out
    assert sa_.track(*args, sa_.legacy_snapshot(part), sa_.legacy_describe, NOW + dt.timedelta(hours=1)) == 0
    assert sa_.track(*args, {}, sa_.legacy_describe, NOW + dt.timedelta(hours=2)) == 0     # nothing at all came back
    # ...and what the partial read did not carry is still remembered: a change there is caught the next time
    moved = box_settings(DITs__weekdayDITHour22="68")
    assert sa_.track(*args, sa_.legacy_snapshot(moved), sa_.legacy_describe, NOW + dt.timedelta(hours=3)) == 1
    session.commit()
    assert [(r.label, r.new_text) for r in _rows()] == [("Indoor target, weekdays, 10 PM", "68°")]


@requires_postgres
def test_the_2010_poller_reads_a_boxs_settings_in_its_turn_and_records_a_change(world, session, app):
    import app.models as m
    from app import legacy
    app.config["LEGACY_SETTINGS_EVERY_MIN"] = 5               # every cycle, for the test
    app.config["LEGACY_HISTORY_ENABLED"] = False
    asked, state = [], {"settings": box_settings()}

    def poll(job, now):
        asked.append((job["id"], job["settings_due"]))
        out = {"id": job["id"], "polled_at": now, "ok": True, "data": {"status": {"callForHeat": "Satisfied"}, "alarm": None}}
        if job["settings_due"] and job["id"] == world["box_mine"]:
            out["settings"] = state["settings"]
        return out

    legacy.poll_all(app, NOW, poll=poll)
    assert all(due for _id, due in asked) and _rows() == []   # first sight: a baseline
    st = session.get(m.LegacyStatus, world["box_mine"])
    assert "settings" not in (st.data or {})                  # the live reading is not weighed down with them
    state["settings"] = box_settings(emsSettings__outsideDaySetpoint="60")
    legacy.poll_all(app, NOW + dt.timedelta(minutes=5), poll=poll)
    r, = _rows()
    assert (r.source, r.source_id, r.device, r.label, r.old_text, r.new_text) == (
        "legacy", world["box_mine"], "50 South 9 St", "Outdoor cutoff, day", "55°", "60°")
    assert r.building_id is not None and r.origin == "outside"

    app.config["LEGACY_SETTINGS_EVERY_MIN"] = 0               # switched off: no box is asked
    asked.clear()
    legacy.poll_all(app, NOW + dt.timedelta(minutes=10), poll=poll)
    assert asked and not any(due for _id, due in asked)


@requires_postgres
def test_a_2010_boxs_page_and_the_changes_page_show_its_settings_changes(root, pat, world, session):
    args = ("legacy", world["box_mine"], None, "50 South 9 St")
    sa_.track(*args, sa_.legacy_snapshot(box_settings()), sa_.legacy_describe, dt.datetime.utcnow() - dt.timedelta(hours=2))
    sa_.track(*args, sa_.legacy_snapshot(box_settings(emsSettings__insideDaySetpoint="74")), sa_.legacy_describe,
              dt.datetime.utcnow() - dt.timedelta(hours=1))
    session.commit()
    page = root.get(f"/live/{world['box_mine']}").get_data(as_text=True)
    assert "Settings changes" in page and "Indoor setpoint, day" in page and "72° → <b>74°</b>" in page
    seen = pat.get(f"/live/{world['box_mine']}")              # a viewer of the box: the page, without the audit
    assert seen.status_code == 200 and "Settings changes" not in seen.get_data(as_text=True)
    allp = root.get("/changes").get_data(as_text=True)
    assert "50 South 9 St" in allp and "2010 box" in allp and f'href="/live/{world["box_mine"]}"' in allp


# ── the settings tick: a person changes settings under their own name ────────

@requires_postgres
def test_a_person_ticked_for_a_property_changes_its_settings_under_their_own_name(root, pat, world, feed, session):
    import app.models as m
    assert pat.get(_url(world)).status_code == 403            # seeing a property is not changing it
    perms = root.get(f"/users/{world['viewer']}/permissions").get_data(as_text=True)
    assert "may change heating settings here" in perms and perms.count('name="set_offered"') == 3    # offered on every property

    r = root.post(f"/users/{world['viewer']}/properties",
                  data={"property_id": [world["berry"]], "set_offered": [world["berry"], world["other"]],
                        "set_property_id": [world["berry"], world["other"]]}, follow_redirects=True)
    said = r.get_data(as_text=True)
    assert "Pat Viewer can now change the heating settings at Berry." in said
    assert "Changing settings was not given at Other" in said           # Pat cannot see Other: no tick there
    assert [(c.user_id, c.property_id) for c in m.SettingControl.query.all()] == [(world["viewer"], world["berry"])]

    detail = pat.get(f"/skyline/{world['asset_mine']}").get_data(as_text=True)
    assert "Change settings…" in detail and "Settings changes" in detail
    assert pat.get(f"/skyline/{world['asset_other']}/settings").status_code == 403      # another owner's building
    preview = pat.post(_url(world), data={**FORM, "step": "preview", "ind-sp-night": "70"}).get_data(as_text=True)
    assert "Send 1 change to 440 Berry St" in preview and "settings changed by Pat Viewer" in preview
    page = pat.post(_url(world), data=_confirm(preview)).get_data(as_text=True)
    assert "Sent, and confirmed." in page and len(feed["puts"]) == 1
    change = m.SettingChange.query.one()
    assert (change.by, change.origin, change.label, change.old_text, change.new_text, change.note) == (
        "Pat Viewer", "portal", "Indoor setpoint, night", "72°", "70°", REASON)
    assert m.BloomSettingWrite.query.one().by == "Pat Viewer"
    assert "Pat Viewer" in root.get("/changes").get_data(as_text=True)
    assert pat.get("/changes").status_code == 403             # the fleet-wide ledger stays with the administrator
    assert "changes settings at Berry" in root.get("/users").get_data(as_text=True)

    # the tick taken off: the page is closed again; nothing else about Pat changes
    root.post(f"/users/{world['viewer']}/properties",
              data={"property_id": [world["berry"]], "set_offered": [world["berry"]]})
    assert m.SettingControl.query.count() == 0 and pat.get(_url(world)).status_code == 403
    assert pat.get(f"/skyline/{world['asset_mine']}").status_code == 200


@requires_postgres
def test_the_settings_tick_goes_when_the_view_goes(root, pat, world, feed, session):
    import app.models as m
    root.post(f"/users/{world['viewer']}/properties",
              data={"property_id": [world["berry"]], "set_offered": [world["berry"]], "set_property_id": [world["berry"]]})
    assert m.SettingControl.query.count() == 1 and pat.get(_url(world)).status_code == 200
    root.post(f"/users/{world['viewer']}/properties", data={"set_offered": [world["berry"]],
                                                           "set_property_id": [world["berry"]]})     # Berry unticked
    assert m.SettingControl.query.count() == 0                # ...so it cannot come back silently with the view
    root.post(f"/users/{world['viewer']}/properties", data={"property_id": [world["berry"]]})
    assert pat.get(_url(world)).status_code == 403 and feed["puts"] == []
