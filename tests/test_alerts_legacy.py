"""
tests/test_alerts_legacy.py — what makes a 2010 box red (app/alerts.py, pure)

Learned on the real boxes: an alarm stays "active" in a box's list for weeks,
so only the CURRENT status code and a NEW safety/ignition alarm count; a box
that never answered is never paged about; one that stops is, after a wait.
"""
import datetime as dt
import types

from app import alerts

NOW = dt.datetime(2026, 10, 14, 22, 0, 0)
DARK = dt.timedelta(minutes=30)


def st(ok=True, code="Outsd Sat", last_ok=NOW, auth_failed=False, alarm=None):
    return types.SimpleNamespace(ok=ok, last_ok_at=last_ok, auth_failed=auth_failed,
                                 data={"status": {"callForHeat": code}, "alarm": alarm})


def alarm(name, minutes_ago):
    at = NOW - dt.timedelta(minutes=minutes_ago)
    return {"name": name, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "active": True}


def test_status_codes():
    assert alerts.legacy_issues(st(code="Safety"), NOW, DARK) == ["Status: Safety."]
    assert alerts.legacy_issues(st(code="Ign Fail"), NOW, DARK) == ["Status: Ignition Failure."]
    assert alerts.legacy_issues(st(code="Aqua Call"), NOW, DARK) == []
    assert alerts.legacy_issues(st(code="RmByp On"), NOW, DARK) == []          # yellow never pages


def test_dark_boxes():
    assert alerts.legacy_issues(st(ok=False, last_ok=None), NOW, DARK) == []   # never answered
    assert alerts.legacy_issues(st(ok=False, last_ok=NOW - dt.timedelta(minutes=10)), NOW, DARK) is None
    line = alerts.legacy_issues(st(ok=False, last_ok=NOW - dt.timedelta(minutes=45)), NOW, DARK)[0]
    assert line.startswith("Not answering since Oct 14 5:15 PM")              # New York time
    refused = alerts.legacy_issues(st(ok=False, auth_failed=True, last_ok=NOW - dt.timedelta(hours=1)), NOW, DARK)
    assert "refuses the portal's login" in refused[0]
    assert alerts.legacy_issues(None, NOW, DARK) is None


def test_only_new_trip_alarms_are_sent():
    old = alarm("ignitionFail", 60 * 24 * 9)
    data = {"alarm": old}
    key, line = alerts.legacy_fresh_alarm(data, None, NOW)                   # first look: remember only
    assert key.startswith("ignitionFail@") and line is None
    assert alerts.legacy_fresh_alarm(data, key, NOW)[1] is None              # same alarm again
    assert alerts.legacy_fresh_alarm({"alarm": alarm("emsRestart", 3)}, key, NOW)[1] is None   # information only
    assert alerts.legacy_fresh_alarm({"alarm": alarm("safetyActive", 60 * 5)}, key, NOW)[1] is None   # new but stale
    k2, line = alerts.legacy_fresh_alarm({"alarm": alarm("safetyActive", 4)}, key, NOW)
    assert k2 != key and line == "Box alarm: Safety at Oct 14 5:56 PM."
    assert alerts.legacy_fresh_alarm({"alarm": None}, key, NOW) == (key, None)    # an empty answer is not news
    assert alerts.legacy_fresh_alarm({"alarm": old}, key, NOW)[1] is None           # nor is the same alarm again
