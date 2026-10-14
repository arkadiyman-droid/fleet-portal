"""
tests/test_legacy.py — reading the installed 2010 gateways (no database)

The XML below is what the first real box (10-12 Bedell St, firmware
3.2.2_1406170930) answered on 2026-10-14; the card it produces must match
what Anagha showed for that building at the time. The safety rules are
checked with a fake network: no login to an address that is not a gateway,
a refused login is reported, and a status that arrived before a failure is
kept.
"""
from unittest import mock

import pytest
import requests

from app import legacy

STATUS_XML = """<?xml version="1.0" encoding="UTF-8"?>
<obj href="http://11bedell.omnieye.org:8060/obix/status" is="useg:Status" >
<abstime name="serverTime" val="2026-10-14T21:13:26Z"/>
<str name="callForHeat" val="Outsd Sat"/>
<int name="averageTemperature" val="75"/>
<int name="targetInsideTemperature" val="69"/>
<int name="apartmentTemperature0" val="75"/>
<int name="apartmentTemperature1" val="0"/>
<int name="stackTemperature" val="90"/>
<int name="highStackTemp" val="91"/>
<int name="boiler1HeatRunMinutes" val="2"/>
<int name="boiler1HWtrRunMinutes" val="179"/>
<int name="boiler2HeatRunMinutes" val="0"/>
<int name="waterMeterCount" val="0"/>
<int name="fuelLevel" val="0"/>
<int name="gallons" val="0"/>
<int name="systemTemperature0" val="67"/>
<int name="systemTemperature1" val="83"/>
<int name="systemTemperature2" val="109"/>
<int name="systemTemperature3" val="119"/>
<int name="systemTemperature4" val="83"/>
<int name="systemTemperature5" val="83"/>
<int name="systemTemperature8" val="0"/>
</obj>"""

NAMES_XML = """<obj href="x/obix/systemnames" is="useg:SysNames" writable="true" >
<str name="systemSensorName0" val="Out " writable="true" min="0" max="6"/>
<str name="systemSensorName1" val="Aqu1" writable="true" min="0" max="6"/>
<str name="systemSensorName2" val="Coil" writable="true" min="0" max="6"/>
<str name="systemSensorName3" val="Mix " writable="true" min="0" max="6"/>
<str name="systemSensorName4" val="HWtR" writable="true" min="0" max="6"/>
<str name="systemSensorName5" val="HWtS" writable="true" min="0" max="6"/>
<str name="systemSensorName8" val="Rsr1" writable="true" min="0" max="6"/>
</obj>"""

ALARM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<obj href="http://11bedell.omnieye.org:8060/obix/alarm/45" is="obix:StatefulAlarm" display="ignitionFail">
<!--USE-Group Alarm Event Object -->
<ref name="source" href="obix/alarmItems/ignitionFail" />
<abstime name="timestamp" val="2026-09-23T08:02:00-04:00"/>
<int name="alarmValue" val="1"/>
<abstime name="normalTimestamp" val=""/>
</obj>"""


def test_the_card_matches_what_anagha_showed_for_the_same_box():
    c = legacy.card({"status": legacy.values(STATUS_XML), "names": legacy.values(NAMES_XML),
                     "alarm": legacy.parse_alarm(ALARM_XML)})
    assert (c["status"], c["tone"]) == ("Outside Temp. Satisfied", "ok")
    assert (c["out"], c["inside"], c["dit"]) == (67, 75, 69)
    assert (legacy.hhmm(c["heat_rt"]), legacy.hhmm(c["hw_rt"]), legacy.hhmm(c["total_rt"])) == ("00:02", "02:59", "03:01")
    assert (c["aqua"], c["coil"], c["mix"]) == (83, 109, 119)
    assert (c["stack"], c["hi_stack"], c["water"]) == (90, 91, 0)
    assert (c["supp"], c["retn"], c["risr"]) == (83, 83, 0)
    assert c["oil_pct"] is None and c["apartments"] == [75]
    assert c["alarm"] == {"name": "ignitionFail", "at": "2026-09-23T08:02:00-04:00", "active": True}
    a = legacy.alarm_info(c["alarm"], now=__import__("datetime").datetime(2026, 9, 23, 13, 0))
    assert (a["words"], a["level"], a["recent"], a["when"]) == ("Ignition failure", "crit", True, "Sep 23 8:02 AM")


def test_an_empty_alarm_list_is_not_an_alarm():
    empty = ('<obj href="x/obix/alarm" is="obix:StatefulAlarm">' + chr(10) +
             '<abstime name="timestamp" val="2026-10-14T21:27:52Z"/>' + chr(10) + '</obj>')
    assert legacy.parse_alarm(empty) is None


def test_alarm_times_are_shown_in_new_york_time_whatever_the_box_offset():
    import datetime as dt
    now = dt.datetime(2026, 10, 14, 21, 30)
    a = legacy.alarm_info({"name": "emsRestart", "at": "2026-10-14T15:02:00-05:00", "active": True}, now=now)
    assert (a["words"], a["level"], a["when"], a["recent"]) == ("Controller restarted", "info", "Oct 14 4:02 PM", True)
    old = legacy.alarm_info({"name": "safetyActive", "at": "2026-10-07T18:49:00-03:00", "active": True}, now=now)
    assert old["recent"] is False and old["words"] == "Safety"


def test_other_boxes_name_their_sensors_differently():
    names = {"systemSensorName0": "Outsid", "systemSensorName1": "Aquast", "systemSensorName4": "CondRe",
             "systemSensorName5": "Supply", "systemSensorName8": "Riser1"}
    st = {"systemTemperature0": "53", "systemTemperature1": "180", "systemTemperature4": "152",
          "systemTemperature5": "177", "systemTemperature8": "117", "callForHeat": "Aqua Call"}
    c = legacy.card({"status": st, "names": names})
    assert (c["out"], c["aqua"], c["supp"], c["retn"], c["risr"]) == (53, 180, 177, 152, 117)
    assert (c["status"], c["tone"]) == ("Aquastat Call", "heat")


def test_status_words_and_colours():
    for code, tone in (("Safety", "crit"), ("Ign Fail", "crit"), ("RmByp On", "warn"), ("Summer", "idle"),
                       ("DTime Rem", "heat"), ("Insd Sat", "ok")):
        assert legacy.card({"status": {"callForHeat": code}})["tone"] == tone
    assert legacy.card({"status": {"callForHeat": "Mystery"}})["status"] == "Mystery"
    assert legacy.card({})["status"] == "No status"


def test_an_alarm_that_cleared():
    xml = ALARM_XML.replace('name="normalTimestamp" val=""', 'name="normalTimestamp" val="2026-09-23T09:00:00-04:00"')
    assert legacy.parse_alarm(xml)["active"] is False
    assert legacy.parse_alarm("") is None


# ── polling safety ──────────────────────────────────────────────────────────

BOX = {"id": 1, "url": "http://192.0.2.7:8060", "user": "u", "password": "p", "verified": False, "names_due": True}


def resp(status, text_=""):
    return mock.Mock(status_code=status, text=text_, headers={})


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(legacy, "PAUSE", 0)
    legacy._sessions.clear()
    yield
    legacy._sessions.clear()


def test_no_login_goes_to_an_address_that_is_not_a_gateway(monkeypatch):
    monkeypatch.setattr(legacy, "identify", lambda url: "other")
    made = []
    monkeypatch.setattr(legacy, "_session", lambda *a: made.append(a))
    out = legacy.poll_one(dict(BOX))
    assert out["ok"] is False and "no login sent" in out["error"] and made == []


def test_a_full_read(monkeypatch):
    monkeypatch.setattr(legacy, "identify", lambda url: "gateway")
    pages = {"obix/status": STATUS_XML, "obix/alarm": ALARM_XML, "obix/systemnames": NAMES_XML}
    s = mock.Mock()
    s.get.side_effect = lambda url, timeout: resp(200, pages[url.split(":8060/")[1]])
    monkeypatch.setattr(legacy, "_session", lambda *a: s)
    out = legacy.poll_one(dict(BOX))
    assert out["ok"] and out["identity"] == "gateway"
    assert out["data"]["status"]["callForHeat"] == "Outsd Sat" and out["data"]["names"]["systemSensorName1"] == "Aqu1"
    assert [c.args[0].rsplit("/", 2)[-2:] for c in s.get.call_args_list] == [["obix", "status"], ["obix", "alarm"], ["obix", "systemnames"]]


def test_a_refused_login_is_reported(monkeypatch):
    s = mock.Mock()
    s.get.return_value = resp(401)
    monkeypatch.setattr(legacy, "_session", lambda *a: s)
    out = legacy.poll_one(dict(BOX, verified=True))
    assert out["ok"] is False and out["auth_failed"] is True


def test_status_is_kept_when_the_box_drops_the_next_request(monkeypatch):
    s = mock.Mock()
    s.get.side_effect = [resp(200, STATUS_XML), requests.ConnectionError("reset by peer")]
    monkeypatch.setattr(legacy, "_session", lambda *a: s)
    out = legacy.poll_one(dict(BOX, verified=True))
    assert out["ok"] is True and out["partial"] is True and out["data"]["status"]["averageTemperature"] == "75"
    assert 1 not in legacy._sessions                       # a fresh session next time


def test_identify(monkeypatch):
    def get(url, timeout, allow_redirects):
        return {"a": mock.Mock(status_code=401, headers={"WWW-Authenticate": 'Digest realm="USEnergy", qop="auth"'}, text=""),
                "b": mock.Mock(status_code=401, headers={"WWW-Authenticate": 'Basic realm="router"'}, text=""),
                "c": mock.Mock(status_code=200, headers={}, text='<obj is="useg:About">')}[url[7]]
    monkeypatch.setattr(legacy.requests, "get", get)
    assert [legacy.identify(f"http://{x}") for x in "abc"] == ["gateway", "other", "open"]


def test_a_box_clock_an_hour_fast_is_corrected():
    import datetime as dt
    data = {"status": {"serverTime": "2026-10-14T22:28:02Z"}, "read_at": "2026-10-14T21:28:00"}
    drift = legacy.clock_drift(data)
    assert round(drift.total_seconds() / 60) == 60
    a = legacy.alarm_info({"name": "ignitionFail", "at": "2026-10-14T18:09:00-04:00", "active": True},
                          now=dt.datetime(2026, 10, 14, 21, 45), drift=drift)
    assert a["when"] == "Oct 14 5:09 PM" and a["recent"] is True
    assert legacy.clock_drift({"status": {"serverTime": "2026-10-14T21:30:00Z"}, "read_at": "2026-10-14T21:28:00"}).total_seconds() == 0
    assert legacy.clock_drift({}).total_seconds() == 0


def test_a_changed_password_gets_a_new_session_even_with_the_same_user_name():
    legacy._sessions.clear()
    a = legacy._session(7, "USEAdmin", "first-password")
    assert legacy._session(7, "USEAdmin", "first-password") is a
    b = legacy._session(7, "USEAdmin", "second-password")
    assert b is not a and b.auth.password == "second-password"
    legacy._sessions.clear()


def test_a_refused_login_drops_the_session(monkeypatch):
    s = mock.Mock()
    s.get.return_value = resp(401)
    legacy._sessions[1] = s
    monkeypatch.setattr(legacy, "_session", lambda *a: s)
    legacy.poll_one(dict(BOX, verified=True))
    assert 1 not in legacy._sessions
