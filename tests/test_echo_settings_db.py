"""
tests/test_echo_settings_db.py — a box's settings changed from the portal: the
shared form rules (app/box_settings.py, no database) and the EchoTherm
controller's page (Postgres tier, the controller faked)

A controller lists what the portal may change, with its own ranges, and only
takes a change where its site has switched that on. The portal shows the
change old -> new, sends only what was shown with the name of who asked, reads
it back and records it in the settings ledger.
"""
import datetime as dt
import re

import pytest

from app import box_settings as bx
from tests.conftest import requires_postgres
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

REASON = "Tenants cold at night"
VALUES = {"dit.target_inside_temp": 72, "dit.night_setback": 4, "dit.day_outside_sp": 56, "dit.night_outside_sp": 52,
          "dit.day_st_hour": 6, "dit.nit_st_hour": 22, "dit.summer_end_month": 9, "dit.summer_end_day": 15,
          "dit.summer_st_month": 5, "dit.summer_st_day": 16}
SHAPE = {"dit.target_inside_temp": ("Indoor target, day", "deg", 55, 90), "dit.night_setback": ("Night setback", "deg", 0, 15),
         "dit.day_outside_sp": ("Outdoor cutoff, day", "deg", 40, 80), "dit.night_outside_sp": ("Outdoor cutoff, night", "deg", 40, 80),
         "dit.day_st_hour": ("Day period starts", "hour", 0, 23), "dit.nit_st_hour": ("Night period starts", "hour", 0, 23),
         "dit.summer_end_month": ("Heating season starts after, month", "month", 1, 12),
         "dit.summer_end_day": ("Heating season starts after, day", "day", 1, 31),
         "dit.summer_st_month": ("Heating season ends, month", "month", 1, 12),
         "dit.summer_st_day": ("Heating season ends, day", "day", 1, 31)}
FORM = {k: str(v) for k, v in VALUES.items()}
CHECKS = (bx.before("dit.day_st_hour", "dit.nit_st_hour", "the day has to start before the night"),
          bx.real_date("dit.summer_end_month", "dit.summer_end_day"), bx.real_date("dit.summer_st_month", "dit.summer_st_day"))


def fields(**change):
    vals = {**VALUES, **change}
    return [{"key": k, "label": n, "kind": kind, "lo": lo, "hi": hi, "value": vals[k], "help": "", "group": "G",
             "cell": None, "group_help": None} for k, (n, kind, lo, hi) in SHAPE.items()]


def row(p, key):
    return next(r for r in p["rows"] if r["key"] == key)


# ── the shared rules (pure) ──────────────────────────────────────────────────

def test_values_read_as_a_person_says_them():
    assert bx.text("deg", 72) == "72°" and bx.text("hour", 6) == "6 AM" and bx.text("hour", 22) == "10 PM"
    assert bx.text("hour", 0) == "12 AM" and bx.text("month", 9) == "September" and bx.text("day", 15) == "15"
    assert bx.text("deg", None) == "—" and bx.text("num", "x") == "—" and bx.text("deg", "71") == "71°"


def test_the_form_as_shown_changes_nothing_and_only_differences_are_sent():
    wanted, errors = bx.parse(FORM, fields())
    assert errors == {} and wanted == VALUES
    p = bx.plan(fields(), wanted, errors, CHECKS)
    assert p["n_changes"] == 0 and p["changes"] == {} and p["digest"] == "" and not p["errors"]
    wanted, _ = bx.parse({**FORM, "dit.target_inside_temp": "70", "dit.nit_st_hour": "23"}, fields())
    p = bx.plan(fields(), wanted, {}, CHECKS)
    assert p["changes"] == {"dit.target_inside_temp": 70, "dit.nit_st_hour": 23}
    assert p["expect"] == {"dit.target_inside_temp": 72, "dit.nit_st_hour": 22} and len(p["digest"]) == 16
    assert (row(p, "dit.nit_st_hour")["old_text"], row(p, "dit.nit_st_hour")["new_text"]) == ("10 PM", "11 PM")
    assert bx.summary(p["log"]) == "Indoor target, day 72° → 70°; Night period starts 10 PM → 11 PM"
    assert bx.parse(dict(bx.hidden_fields(wanted)) | {k: str(v) for k, v in bx.hidden_fields(wanted)}, fields())[0] == wanted
    assert bx.parse({}, fields()) == ({}, {}) and bx.parse({"dit.target_inside_temp": " "}, fields()) == ({}, {})


def test_a_refused_value_sends_nothing_at_all():
    for key, bad, why in (("dit.target_inside_temp", "91", "allowed 55° to 90°"), ("dit.night_setback", "16", "allowed 0° to 15°"),
                          ("dit.target_inside_temp", "70.5", "whole numbers only"), ("dit.day_st_hour", "22", "start before"),
                          ("dit.nit_st_hour", "6", "start before"), ("dit.summer_end_day", "31", "does not exist"),
                          ("dit.target_inside_temp", "warm", "not a number")):
        wanted, errors = bx.parse({**FORM, key: bad, "dit.day_outside_sp": "58"}, fields())
        p = bx.plan(fields(), wanted, errors, CHECKS)
        assert p["errors"] and why in row(p, key)["error"], (key, bad)
        assert p["changes"] == {} and p["expect"] == {} and p["digest"] == ""      # not even the good half
    ok = bx.plan(fields(), {"dit.day_st_hour": 7, "dit.nit_st_hour": 21, "dit.summer_end_month": 10, "dit.summer_end_day": 31}, {}, CHECKS)
    assert not ok["errors"] and ok["n_changes"] == 4
    # a value the box already holds outside its range is not our error until someone changes it
    assert not bx.plan(fields(**{"dit.night_setback": 20}), {"dit.night_setback": 20, "dit.target_inside_temp": 70}, {}, CHECKS)["errors"]


def test_the_digest_follows_the_change_and_what_it_was_made_against():
    a = bx.plan(fields(), {"dit.target_inside_temp": 70}, {}, CHECKS)["digest"]
    assert a == bx.plan(fields(), {"dit.target_inside_temp": 70, "dit.night_setback": 4}, {}, CHECKS)["digest"]
    assert a != bx.plan(fields(), {"dit.target_inside_temp": 69}, {}, CHECKS)["digest"]
    assert a != bx.plan(fields(**{"dit.target_inside_temp": 74}), {"dit.target_inside_temp": 70}, {}, CHECKS)["digest"]
    assert bx.landed(fields(**{"dit.target_inside_temp": 70}), {"dit.target_inside_temp": 70})
    assert not bx.landed(fields(), {"dit.target_inside_temp": 70})


# ── the controller's page ────────────────────────────────────────────────────

@pytest.fixture
def bench(monkeypatch, world, session):
    """A controller on Berry, faked: {"vals", "enabled", "weekly", "old", "posts", "refuse"}."""
    import app.models as m
    from app import routes
    b = m.Building.query.filter_by(name="50 South 9 St").one()
    c = m.Controller(building_id=b.id, name="Berry boiler room", tailscale_ip="100.64.0.9", fleet_api_token="tok", is_active=True)
    session.add(c)
    session.commit()
    st = {"id": c.id, "vals": dict(VALUES), "enabled": True, "weekly": False, "old": False, "posts": [], "refuse": None, "down": False}

    def call(ctrl, method="get", body=None):
        if st["down"]:
            raise ConnectionError("no route to host")
        if st["old"]:
            return 401, {"error": "not authenticated"}
        if method == "post":
            st["posts"].append(body)
            if not st["enabled"]:
                return 403, {"ok": False, "code": "remote_settings_off", "error": "switched off on this controller"}
            if st["refuse"]:
                return 200, {"ok": False, "code": "rejected", "error": "nothing was changed",
                             "results": {k: {"status": "rejected", "error": st["refuse"]} for k in body["changes"]}}
            st["vals"].update(body["changes"])
            return 200, {"ok": True, "changed": len(body["changes"]), "results": {}}
        return 200, {"settings": dict(st["vals"]), "changes": [], "labels": {},
                     "remote": {"enabled": st["enabled"], "weeklyScheduleEnabled": st["weekly"],
                                "fields": [{"key": k, "name": n, "kind": kind, "label": n, "min": lo, "max": hi,
                                            "value": st["vals"][k], "tip": ""} for k, (n, kind, lo, hi) in SHAPE.items()]}}

    monkeypatch.setattr(routes, "_echo_call", call)
    return st


def _confirm(page, **over):
    card = page[page.index("What will change at"):page.index('name="step" value="preview"')]
    data = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', card))
    data["note"] = REASON
    data.update(over)
    return data


@requires_postgres
def test_the_page_shows_the_controllers_own_list_and_sends_nothing(root, world, bench):
    page = root.get(f"/fleet/{bench['id']}/settings").get_data(as_text=True)
    for label in ("Indoor target, day", "Night setback", "Outdoor cutoff, night", "Heating season ends, month"):
        assert label in page
    assert 'name="dit.target_inside_temp" value="72" min="55" max="90"' in page and "allowed 55° to 90°" in page
    assert '<option value="6" selected>6 AM</option>' in page and '<option value="9" selected>September</option>' in page
    assert "Review changes" in page and "What will change" not in page and bench["posts"] == []
    assert "Change settings…" in root.get(f"/fleet/{bench['id']}").get_data(as_text=True)


@requires_postgres
def test_a_confirmed_change_goes_with_the_persons_name_and_lands_in_the_ledger(root, world, bench):
    import app.models as m
    url = f"/fleet/{bench['id']}/settings"
    preview = root.post(url, data={**FORM, "step": "preview", "dit.target_inside_temp": "70", "dit.nit_st_hour": "23"}
                        ).get_data(as_text=True)
    assert "Send 2 changes to Berry boiler room" in preview and bench["posts"] == []
    assert re.search(r"<td>Night period starts</td><td>10 PM</td><td>→</td><td class=\"new\">11 PM</td>", preview)
    page = root.post(url, data=_confirm(preview)).get_data(as_text=True)
    assert bench["posts"] == [{"by": "the administrator", "changes": {"dit.target_inside_temp": 70, "dit.nit_st_hour": 23},
                               "expect": {"dit.target_inside_temp": 72, "dit.nit_st_hour": 22}}]
    assert "Changed, and confirmed." in page and "Indoor target, day 72° → 70°" in page
    rows = m.SettingChange.query.order_by(m.SettingChange.id).all()
    assert [(r.source, r.key, r.label, r.old_text, r.new_text, r.origin, r.by, r.note) for r in rows] == [
        ("echotherm", "dit.target_inside_temp", "Indoor target, day", "72°", "70°", "portal", "the administrator", REASON),
        ("echotherm", "dit.nit_st_hour", "Night period starts", "10 PM", "11 PM", "portal", "the administrator", REASON)]
    assert all(r.seen_at is not None and r.building_id for r in rows)
    root.post(url, data=_confirm(preview))                    # the same confirmation again: no longer the change shown
    assert len(bench["posts"]) == 1 and m.SettingChange.query.count() == 2


@requires_postgres
def test_what_the_controller_refuses_changes_nothing_and_is_said_plainly(root, world, bench):
    import app.models as m
    url = f"/fleet/{bench['id']}/settings"
    preview = root.post(url, data={**FORM, "step": "preview", "dit.day_outside_sp": "60"}).get_data(as_text=True)
    bench["refuse"] = "it is 58 here now, not 56: look again before changing it"
    page = root.post(url, data=_confirm(preview)).get_data(as_text=True)
    assert "Not changed." in page and "it is 58 here now" in page and m.SettingChange.query.count() == 0
    bench["refuse"], bench["vals"]["dit.day_outside_sp"] = None, 58       # it really did move at the site
    page = root.post(url, data=_confirm(preview)).get_data(as_text=True)
    assert "Nothing was sent." in page and len(bench["posts"]) == 1       # caught here, before asking the controller
    page = root.post(url, data={**FORM, "step": "send", "dit.target_inside_temp": "95", "digest": "x", "note": REASON}
                     ).get_data(as_text=True)
    assert "Nothing can be sent yet." in page and "allowed 55° to 90°" in page and len(bench["posts"]) == 1


@requires_postgres
def test_a_site_that_has_not_switched_it_on_is_read_only(root, world, bench):
    bench["enabled"] = False
    url = f"/fleet/{bench['id']}/settings"
    page = root.get(url).get_data(as_text=True)
    assert "does not take settings from the portal" in page and "Review changes" not in page
    assert 'name="dit.target_inside_temp" value="72" min="55" max="90" step="1" inputmode="numeric" disabled' in page
    root.post(url, data={**FORM, "step": "send", "dit.target_inside_temp": "70", "digest": "x", "note": REASON})
    assert bench["posts"] == []                               # not even asked
    bench["enabled"], bench["weekly"] = True, True
    assert "The weekly schedule is on at this controller." in root.get(url).get_data(as_text=True)
    bench["old"] = True
    page = root.get(url).get_data(as_text=True)
    assert "Not available on this controller yet." in page and "Review changes" not in page
    bench["old"], bench["down"] = False, True
    assert "Could not read this controller" in root.get(url).get_data(as_text=True)


@requires_postgres
def test_only_the_administrator_or_a_ticked_person_gets_the_page(root, pat, world, bench):
    import app.models as m
    url = f"/fleet/{bench['id']}/settings"
    assert pat.get(url).status_code == 403
    assert "Change settings" not in pat.get(f"/fleet/{bench['id']}").get_data(as_text=True)
    root.post(f"/users/{world['viewer']}/properties",
              data={"property_id": [world["berry"]], "set_offered": [world["berry"]], "set_property_id": [world["berry"]]})
    assert "Change settings…" in pat.get(f"/fleet/{bench['id']}").get_data(as_text=True)
    preview = pat.post(url, data={**FORM, "step": "preview", "dit.night_setback": "6"}).get_data(as_text=True)
    pat.post(url, data=_confirm(preview))
    assert bench["posts"][-1]["by"] == "Pat Viewer"
    r = m.SettingChange.query.one()
    assert (r.by, r.label, r.old_text, r.new_text) == ("Pat Viewer", "Night setback", "4°", "6°")
