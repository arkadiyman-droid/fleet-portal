"""
tests/test_alerts_legacy_db.py — alerts for the 2010 boxes (Postgres tier)

Readings are written straight into LegacyStatus (what the poller stores),
then app/alerts.check_legacy runs as the poller would after each cycle.
Sending is stubbed (app/notify.send).
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

T0 = dt.datetime(2026, 10, 14, 12, 0, 0)


@pytest.fixture
def client(app):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def sent(monkeypatch):
    import app.notify as n
    calls = []
    monkeypatch.setattr(n, "send", lambda title, body, **kw: calls.append((title, body, kw)) or "ntfy ok")
    return calls


def _boxes(session, n):
    import app.models as m
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    session.add(login); session.flush()
    out = [m.LegacyBox(name=f"{i} Bedell St", host="192.0.2.7", port=8060 + i, identity="gateway", login_id=login.id)
           for i in range(n)]
    session.add_all(out); session.commit()
    return out


def _read(session, box, at, ok=True, code="Outsd Sat", alarm=None, auth_failed=False):
    import app.models as m
    row = session.get(m.LegacyStatus, box.id) or m.LegacyStatus(box_id=box.id)
    row.polled_at, row.ok, row.auth_failed = at, ok, auth_failed
    if ok:
        row.last_ok_at = at
        row.data = {"status": {"callForHeat": code}, "alarm": alarm}
    session.add(row); session.commit()


def _check(app, session, minute):
    from app import alerts
    alerts.check_legacy(app, now=T0 + dt.timedelta(minutes=minute))
    session.expire_all()


def _alarm(name, at):
    return {"name": name, "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "active": True}


def test_safety_three_reads_then_a_clear_recovered(app, session, sent, client):
    import app.models as m
    (b,) = _boxes(session, 1)
    for minute, code in ((0, "Outsd Sat"), (5, "Safety"), (10, "Safety")):
        _read(session, b, T0 + dt.timedelta(minutes=minute), code=code)
        _check(app, session, minute)
    assert sent == []                                                  # two reads are not enough
    _read(session, b, T0 + dt.timedelta(minutes=15), code="Safety")
    _check(app, session, 15)
    assert len(sent) == 1 and sent[0][0] == "ALERT: 0 Bedell St" and "Status: Safety." in sent[0][1]
    assert sent[0][2]["path"] == f"/live/{b.id}" and sent[0][2]["urgent"]

    page = client.get("/alerts").data.decode()
    assert "0 Bedell St" in page and "2010 box" in page and "Status: Safety." in page
    assert 'data-box="%d"' % b.id in page and 'data-k="stack,aqua,inside"' in page and "sensorchart.js" in page
    assert "In alarm" in client.get(f"/live/{b.id}").data.decode()

    _read(session, b, T0 + dt.timedelta(minutes=20), code="Safety")
    _check(app, session, 20)
    assert len(sent) == 1                                              # no repeats
    _read(session, b, T0 + dt.timedelta(minutes=50), code="Outsd Sat")
    _check(app, session, 50)
    assert len(sent) == 2 and sent[1][0] == "Recovered: 0 Bedell St" and sent[1][2]["good_news"]
    assert "Safety: cleared after 35 min." in sent[1][1] and "Status now: Outside Temp. Satisfied." in sent[1][1]
    kinds = [e.kind for e in m.AlertEvent.query.filter_by(legacy_box_id=b.id).order_by(m.AlertEvent.id)]
    assert kinds == ["open", "recovered"]


def test_a_short_safety_alert_closes_quietly(app, session, sent):
    import app.models as m
    (b,) = _boxes(session, 1)
    for minute, code in ((0, "Safety"), (5, "Safety"), (10, "Safety"), (15, "Outsd Sat")):
        _read(session, b, T0 + dt.timedelta(minutes=minute), code=code)
        _check(app, session, minute)
    assert [s[0] for s in sent] == ["ALERT: 0 Bedell St"]                # no "back to normal after 5 min"
    quiet = m.AlertEvent.query.filter_by(legacy_box_id=b.id, kind="recovered").one()
    assert "closed quietly" in quiet.delivered


def test_a_building_that_keeps_tripping_gets_one_message(app, session, sent):
    import app.models as m
    (b,) = _boxes(session, 1)
    minute = 0
    for _trip in range(5):                                             # Safety 15 min, then fine 45 min, five times
        for code in ("Safety", "Safety", "Safety", "Outsd Sat"):
            _read(session, b, T0 + dt.timedelta(minutes=minute), code=code)
            _check(app, session, minute)
            minute += 5
        minute += 40
    titles = [s[0] for s in sent]
    assert titles == ["ALERT: 0 Bedell St", "ALERT: 0 Bedell St", "Keeps tripping: 0 Bedell St"]
    assert "3 alerts in 6 hours" in sent[2][1] and "paused" in sent[2][1]
    later = m.AlertEvent.query.filter_by(legacy_box_id=b.id, kind="open").order_by(m.AlertEvent.id).all()[-1]
    assert "paused until" in later.delivered                           # trips 4 and 5: recorded, not sent


def test_the_morning_summary(app, session, sent):
    import app.models as m
    from app import alerts
    a, c = _boxes(session, 2)
    session.add(m.LegacyAlertState(box_id=a.id, alerting=True, opened_at=T0, issue="Status: Safety."))
    session.add(m.AlertEvent(legacy_box_id=c.id, controller_name=c.name, kind="open", title="ALERT", text="x",
                             delivered="ntfy ok", created_at=T0))
    session.commit()
    seven = dt.datetime(2026, 10, 14, 11, 0)                             # 7 am New York (EDT)
    eight = dt.datetime(2026, 10, 14, 12, 5)
    assert alerts.daily_digest(app, now=seven) is False and sent == []
    assert alerts.daily_digest(app, now=eight) is True
    assert alerts.daily_digest(app, now=eight + dt.timedelta(hours=3)) is False      # once a day
    title, body, _kw = sent[0]
    assert title == "Morning summary: 1 in alarm"
    assert "- 0 Bedell St: Status: Safety" in body and "Tripped and cleared in the last 24 h (1): 1 Bedell St" in body


def test_old_alarms_never_sent_a_trip_the_box_clears_itself_is_not_sent(app, session, sent):
    (b,) = _boxes(session, 1)
    old = _alarm("ignitionFail", T0 - dt.timedelta(days=9))
    _read(session, b, T0, alarm=old)
    _check(app, session, 0)
    _read(session, b, T0 + dt.timedelta(minutes=5), alarm=old)
    _check(app, session, 5)
    assert sent == []
    new = _alarm("ignitionFail", T0 + dt.timedelta(minutes=8))
    _read(session, b, T0 + dt.timedelta(minutes=10), alarm=new)       # status already fine again
    _check(app, session, 10)
    _read(session, b, T0 + dt.timedelta(minutes=15), alarm=new)
    _check(app, session, 15)
    assert sent == []                                                  # a one-off the box cleared: history only


def test_dark_after_reading_but_never_answered_is_quiet(app, session, sent):
    import app.models as m
    reading, never = _boxes(session, 2)
    session.add(m.LegacyStatus(box_id=never.id, polled_at=T0, ok=False)); session.commit()
    _read(session, reading, T0)
    for minute in (5, 20, 35, 40, 45):
        _read(session, reading, T0 + dt.timedelta(minutes=minute), ok=False)
        _check(app, session, minute)
    assert len(sent) == 1 and sent[0][0] == "ALERT: 0 Bedell St" and "Not answering since" in sent[0][1]
    from flask import render_template_string
    with app.test_request_context():
        macro = render_template_string(open("app/templates/alerts.html", encoding="utf-8").read().split("{% block content %}")[0]
                                       .split("{% macro ckeys")[1].join(["{% macro ckeys", ""]) + "{{ ckeys('Not answering since 5 PM.') }}")
    assert macro.strip() == "inside,out"                                      # a quiet box: where its data stops
    _read(session, reading, T0 + dt.timedelta(minutes=80))
    _check(app, session, 80)
    assert sent[-1][0] == "Recovered: 0 Bedell St" and "Not answering since" in sent[-1][1]


def test_many_dark_together_is_one_outage_message(app, session, sent):
    import app.models as m
    boxes = _boxes(session, 12)
    for b in boxes:
        _read(session, b, T0)
    for minute in (35, 40, 45):
        for b in boxes:
            _read(session, b, T0 + dt.timedelta(minutes=minute), ok=False)
        _check(app, session, minute)
    assert [s[0] for s in sent] == ["2010 boxes: most stopped answering"]
    assert m.LegacyAlertState.query.filter_by(alerting=True).count() == 0
    for b in boxes:
        _read(session, b, T0 + dt.timedelta(minutes=50))
    _check(app, session, 50)
    assert [s[0] for s in sent] == ["2010 boxes: most stopped answering", "2010 boxes: answering again"]


def test_mute_and_hide(app, session, sent, client):
    import app.models as m
    a, b = _boxes(session, 2)
    r = client.post(f"/live/{a.id}/mute", data={"hours": "24"})
    assert r.status_code == 302
    for minute in (0, 5, 10):
        _read(session, a, T0 + dt.timedelta(minutes=minute), code="Safety")
        _read(session, b, T0 + dt.timedelta(minutes=minute), code="Safety")
        _check(app, session, minute)
    assert [s[0] for s in sent] == ["ALERT: 1 Bedell St"]             # the muted one stays quiet
    client.post("/live/action", data={"action": "hide", "box_id": str(b.id)})
    _check(app, session, 15)
    assert not session.get(m.LegacyAlertState, b.id).alerting       # closed quietly
    assert len(sent) == 1
    assert "Muted until" in client.get(f"/live/{a.id}").data.decode()


def test_a_steam_ignition_alert_carries_the_evidence(app, session, sent):
    import app.models as m
    from app import history
    (b,) = _boxes(session, 1)
    reads = []
    for k in range(0, 45, 5):                                       # aquastat and hot water sliding down
        data = {"status": {"callForHeat": "Ign Fail", "boiler1HeatRunMinutes": "100",
                           "systemTemperature0": str(170 - k), "systemTemperature1": str(122 - k // 3)},
                "names": {"systemSensorName0": "Aqu1", "systemSensorName1": "Mix"}, "config": {"heatControlType": "1"}}
        reads.append(history.reading(b.id, T0 + dt.timedelta(minutes=k), data))
    history.save(reads, [], T0)
    for minute in (40, 45):
        row = session.get(m.LegacyStatus, b.id) or m.LegacyStatus(box_id=b.id)
        row.polled_at, row.ok, row.last_ok_at = T0 + dt.timedelta(minutes=minute), True, T0 + dt.timedelta(minutes=minute)
        row.data = {"status": {"callForHeat": "Ign Fail"}, "alarm": None, "config": {"heatControlType": "1"}}
        session.add(row); session.commit()
        _check(app, session, minute)
    assert len(sent) == 1 and "Status: Ignition Failure." in sent[0][1]
    assert "Aquastat 170 -> 130 F in 40 min (falling)" in sent[0][1] and "looks real" in sent[0][1]


def test_a_hot_water_ignition_failure_sends_nothing(app, session, sent):
    import app.models as m
    (b,) = _boxes(session, 1)
    for minute in (0, 5, 10):
        row = session.get(m.LegacyStatus, b.id) or m.LegacyStatus(box_id=b.id)
        row.polled_at, row.ok, row.last_ok_at = T0 + dt.timedelta(minutes=minute), True, T0 + dt.timedelta(minutes=minute)
        row.data = {"status": {"callForHeat": "Ign Fail"}, "alarm": None, "config": {"heatControlType": "13"}}
        session.add(row); session.commit()
        _check(app, session, minute)
    assert sent == []


def _steam_reads(session, b, minutes, code, aqua, mix=None, hct="1"):
    from app import history
    reads = []
    for k, (c, a) in enumerate(zip([code] * len(aqua) if isinstance(code, str) else code, aqua)):
        st = {"callForHeat": c, "systemTemperature0": str(a), "boiler1HeatRunMinutes": str(5 * k)}
        names = {"systemSensorName0": "Aqu1"}
        if mix:
            st["systemTemperature1"], names["systemSensorName1"] = str(mix[k]), "Mix"
        reads.append(history.reading(b.id, T0 + dt.timedelta(minutes=minutes[k]),
                                     {"status": st, "names": names, "config": {"heatControlType": hct}}))
    history.save(reads, [], T0)


def test_steam_no_heat_from_a_falling_aquastat(app, session, sent):
    import app.models as m
    from app import alerts
    (b,) = _boxes(session, 1)
    mins = list(range(0, 60, 5))
    aqua = [160, 158, 157, 155, 153, 151, 149, 147, 145, 143, 141, 139]   # calling all along, still dropping
    mix = [121, 120, 119, 118, 117, 116, 115, 114, 113, 112, 111, 110]
    _steam_reads(session, b, mins, "Aqua Call", aqua, mix)
    assert alerts.steam_no_heat(b.id, T0 + dt.timedelta(minutes=20)) is None             # 20 min: too early
    line = alerts.steam_no_heat(b.id, T0 + dt.timedelta(minutes=55))
    assert line.startswith("No heat: calling for heat 55 min (Aquastat Call) but the aquastat fell 160 -> 139 F")
    assert "hot water mix 121 -> 110 F" in line
    for minute in (40, 45, 50, 55):                    # the 30-min call is seen at 40 (drop 13); confirmed 9+ min later
        row = session.get(m.LegacyStatus, b.id) or m.LegacyStatus(box_id=b.id)
        at = T0 + dt.timedelta(minutes=minute)
        row.polled_at, row.ok, row.last_ok_at = at, True, at
        row.data = {"status": {"callForHeat": "Aqua Call"}, "alarm": None, "config": {"heatControlType": "1"}}
        session.add(row); session.commit()
        _check(app, session, minute)
    assert len(sent) == 1 and sent[0][0] == "ALERT: 0 Bedell St" and sent[0][1].startswith("No heat: calling for heat")


def test_a_normal_steam_call_and_hot_water_never_say_no_heat(app, session):
    from app import alerts
    rising, hot = _boxes(session, 2)
    mins = list(range(0, 60, 5))
    _steam_reads(session, rising, mins, "Aqua Call", [150, 149, 152, 160, 170, 180, 190, 195, 198, 200, 201, 202])
    _steam_reads(session, hot, mins, "Aqua Call", [160 - 2 * k for k in range(12)], hct="13")
    assert alerts.steam_no_heat(rising.id, T0 + dt.timedelta(minutes=55)) is None         # it lit: the aquastat climbs
    import app.models as m
    session.add(m.LegacyStatus(box_id=hot.id, polled_at=T0 + dt.timedelta(minutes=55), ok=True,
                               last_ok_at=T0 + dt.timedelta(minutes=55),
                               data={"status": {"callForHeat": "Aqua Call"}, "config": {"heatControlType": "13"}}))
    session.commit()
    alerts.check_legacy(app, now=T0 + dt.timedelta(minutes=55))
    assert (session.get(m.LegacyAlertState, hot.id).issue or "") == ""                    # hot water: not this rule


def test_hot_water_supply_far_below_target(app, session, sent):
    import app.models as m
    from app import alerts, history
    (b,) = _boxes(session, 1)
    reads = []
    for k in range(12):                                 # calling 55 min; supply stuck near 75 F against 140 F
        data = {"status": {"callForHeat": "Circ Pump", "desiredSupplyTemp": "140", "systemTemperature0": str(74 + k % 3)},
                "names": {"systemSensorName0": "HWtS"}, "config": {"heatControlType": "13"}}
        reads.append(history.reading(b.id, T0 + dt.timedelta(minutes=5 * k), data))
    history.save(reads, [], T0)
    assert m.LegacyReading.query.filter_by(box_id=b.id).first().dsup == 140                # stored for the charts
    data_now = reads and {"status": {"callForHeat": "Circ Pump", "desiredSupplyTemp": "140"}, "config": {"heatControlType": "13"}}
    assert alerts.hydronic_below_target(b.id, data_now, T0 + dt.timedelta(minutes=20)) is None    # under 30 min
    line = alerts.hydronic_below_target(b.id, data_now, T0 + dt.timedelta(minutes=55))
    assert line.startswith("Supply below target: supply ") and "below its target 140 F after 55 min calling" in line
    for minute in (40, 45, 50, 55):
        row = session.get(m.LegacyStatus, b.id) or m.LegacyStatus(box_id=b.id)
        at = T0 + dt.timedelta(minutes=minute)
        row.polled_at, row.ok, row.last_ok_at = at, True, at
        row.data = dict(data_now, alarm=None)
        session.add(row); session.commit()
        _check(app, session, minute)
    assert len(sent) == 1 and sent[0][1].startswith("Supply below target")
    assert alerts.hydronic_below_target(b.id, {**data_now, "config": {"heatControlType": "1"}}, T0 + dt.timedelta(minutes=55)) is None
