"""
tests/test_setting_audit_echotherm_db.py — the settings ledger for EchoTherm
controllers (Postgres tier; the pure tests first)

A controller answers GET /api/fleet/settings (the fleet token, read-only) with
every setting as it stands and its own note of who saved what lately. The
portal compares the settings with its stored snapshot and uses the note to
name who made a change at the controller.
"""
import datetime as dt

from app import setting_audit as sa_
from tests.conftest import requires_postgres
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

NOW = dt.datetime(2026, 11, 21, 2, 0, 0)


def reply(changes=(), **over):
    """What a controller's GET /api/fleet/settings brings back (cut down)."""
    settings = {"heat_ctrl_type": 11, "setpoints.inside_day": 72, "setpoints.inside_night": 70,
                "summer_off": False, "alerting.sms.auth_token": "set", "building_name": "Bench",
                "weekly_schedule.weekday": [70, 70, 71, 71],
                "sensor_registry": [{"slot": 1, "name": "Apt 101", "bias": 0}, {"slot": 3, "name": "Apt 103", "bias": 0}]}
    settings.update(over)
    return {"settings": settings, "changes": list(changes)}


def saved(by, *keys, ts=1795000000):
    return {"ts": ts, "by": by, "changes": [{"key": k, "old": None, "new": None} for k in keys]}


# ── pure ─────────────────────────────────────────────────────────────────────

def test_a_list_is_spread_out_so_one_changed_entry_is_one_row():
    snap = sa_.echotherm_snapshot(reply()["settings"])
    assert snap["setpoints.inside_day"] == 72 and snap["summer_off"] is False
    assert snap["weekly_schedule.weekday[2]"] == 71 and snap["weekly_schedule.weekday.count"] == 4
    assert snap["sensor_registry[slot 3].name"] == "Apt 103" and snap["sensor_registry.count"] == 2
    assert not any(isinstance(v, (list, dict)) for v in snap.values())
    one_hour = sa_.echotherm_snapshot(reply(**{"weekly_schedule.weekday": [70, 68, 71, 71]})["settings"])
    assert sa_.diff(snap, one_hour) == [("weekly_schedule.weekday[1]", 70, 68)]
    gone = sa_.echotherm_snapshot(reply(sensor_registry=[{"slot": 1, "name": "Apt 101", "bias": 0}])["settings"])
    assert sa_.diff(snap, gone) == [("sensor_registry.count", 2, 1)]      # an entry removed still shows
    assert sa_.echotherm_snapshot(None) == {} and sa_.echotherm_snapshot({}) == {}


def test_controller_settings_read_in_words():
    d = sa_.echotherm_describe
    assert d("setpoints.inside_day", 72) == ("Setpoints › inside day", "72")
    assert d("weekly_schedule.weekday[2]", 71) == ("Weekly schedule › weekday › 3", "71")
    assert d("sensor_registry[slot 3].name", "Apt 103") == ("Sensor registry › slot 3 › name", "Apt 103")
    assert d("sensor_registry.count", 2) == ("Sensor registry › number of entries", "2")
    assert d("summer_off", True) == ("Summer off", "on") and d("summer_off", False)[1] == "off"
    assert d("alerting.sms.auth_token", "set") == ("Alerting › sms › auth token", "set")
    assert d("building_name", "")[1] == "(empty)" and d("x", None)[1] == "—"


def test_the_controllers_own_note_names_who_changed_a_setting():
    who = sa_.echotherm_who([saved("API key 'bms'", "setpoints.inside_day", ts=1),
                             saved("the dashboard (admin login)", "setpoints.inside_day", "weekly_schedule.weekday", ts=2),
                             saved("the controller itself (its screen or its own software)", "sensor_registry", ts=3)])
    assert who("setpoints.inside_day") == "the dashboard (admin login)"          # the latest save wins
    assert who("weekly_schedule.weekday[1]") == "the dashboard (admin login)"    # a list entry belongs to its list
    assert who("sensor_registry[slot 3].name").startswith("the controller itself")
    assert who("sensor_registry.count").startswith("the controller itself")
    assert who("summer_off") is None                                              # nobody noted it: no name
    assert sa_.echotherm_who(None)("x") is None and sa_.echotherm_who([{"junk": 1}, "x"])("y") is None


# ── the ledger ───────────────────────────────────────────────────────────────

def _rows():
    import app.models as m
    return m.SettingChange.query.order_by(m.SettingChange.id).all()


def _controller(session, world):
    import app.models as m
    b = m.Building.query.filter_by(name="50 South 9 St").one()
    c = m.Controller(building_id=b.id, name="Berry boiler room", tailscale_ip="100.64.0.9", fleet_api_token="tok",
                     is_active=True)
    session.add(c)
    session.flush()
    session.add(m.ControllerStatus(controller_id=c.id, polled_at=dt.datetime.utcnow(), ok=True, raw_json={}))
    session.commit()
    return c


class FakeReply:
    __test__ = False
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.HTTPError(f"HTTP {self.status_code}")


@requires_postgres
def test_a_change_at_the_controller_lands_in_the_ledger_with_who_made_it(world, session, app, monkeypatch):
    from app import fleet_poller
    c = _controller(session, world)
    state, asked = {"r": FakeReply(200, reply())}, []
    monkeypatch.setattr(fleet_poller.requests, "get",
                        lambda url, timeout=None, headers=None: asked.append((url, headers)) or state["r"])

    assert fleet_poller.audit_settings(app, c, NOW) == 0      # first sight: a baseline
    assert asked[0][0] == "http://100.64.0.9:8061/api/fleet/settings" and asked[0][1] == {"Authorization": "Bearer tok"}
    assert _rows() == []
    assert fleet_poller.audit_settings(app, c, NOW + dt.timedelta(minutes=5)) == 0      # nobody touched it

    state["r"] = FakeReply(200, reply(
        [saved("the dashboard (admin login)", "setpoints.inside_night", "weekly_schedule.weekday")],
        **{"setpoints.inside_night": 66, "weekly_schedule.weekday": [70, 70, 71, 69], "alerting.sms.auth_token": "not set"}))
    assert fleet_poller.audit_settings(app, c, NOW + dt.timedelta(minutes=10)) == 3
    got = {r.key: r for r in _rows()}
    night = got["setpoints.inside_night"]
    assert (night.source, night.source_id, night.device, night.label, night.old_text, night.new_text) == (
        "echotherm", c.id, "Berry boiler room", "Setpoints › inside night", "70", "66")
    assert night.origin == "outside" and night.by == "the dashboard (admin login)" and night.building_id == c.building_id
    assert sa_.who(night) == "the dashboard (admin login), at the controller"
    assert got["weekly_schedule.weekday[3]"].by == "the dashboard (admin login)"
    token = got["alerting.sms.auth_token"]                    # a credential: that it changed, never what it was
    assert (token.old_text, token.new_text, token.by) == ("set", "not set", None)
    assert "outside the portal (at the controller)" == sa_.who(token)
    assert fleet_poller.audit_settings(app, c, NOW + dt.timedelta(minutes=15)) == 0 and len(_rows()) == 3


@requires_postgres
def test_a_controller_on_older_software_is_simply_not_compared(world, session, app, monkeypatch):
    import app.models as m
    from app import fleet_poller
    c = _controller(session, world)
    for status in (401, 403, 404):
        monkeypatch.setattr(fleet_poller.requests, "get", lambda url, timeout=None, headers=None, s=status: FakeReply(s))
        assert fleet_poller.audit_settings(app, c, NOW) is None
    assert _rows() == [] and m.SettingSnapshot.query.count() == 0


@requires_postgres
def test_the_controllers_page_and_the_changes_page_show_it(root, pat, world, session, app, monkeypatch):
    from app import fleet_poller
    c = _controller(session, world)
    state = {"r": FakeReply(200, reply())}
    monkeypatch.setattr(fleet_poller.requests, "get", lambda url, timeout=None, headers=None: state["r"])
    fleet_poller.audit_settings(app, c, dt.datetime.utcnow() - dt.timedelta(hours=2))
    state["r"] = FakeReply(200, reply([saved("API key 'bms'", "setpoints.inside_day")], **{"setpoints.inside_day": 74}))
    fleet_poller.audit_settings(app, c, dt.datetime.utcnow() - dt.timedelta(hours=1))

    page = root.get(f"/fleet/{c.id}").get_data(as_text=True)
    assert "Settings changes" in page and "Setpoints › inside day" in page and "72 → <b>74</b>" in page
    assert "API key &#39;bms&#39;, at the controller" in page
    allp = root.get("/changes").get_data(as_text=True)
    assert "Berry boiler room" in allp and "EchoTherm" in allp and f'href="/fleet/{c.id}"' in allp
    assert "Outside the portal <b>1</b>" in allp
    seen = pat.get(f"/fleet/{c.id}")                          # a viewer of the building: the page, without the audit
    assert seen.status_code == 200 and "Settings changes" not in seen.get_data(as_text=True)
