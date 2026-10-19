"""
tests/test_ctrl_history_db.py — the EchoTherm controllers' chart history (Postgres tier)

A snapshot every 5 minutes from the poll the portal already makes; run
counters are seconds since the controller's midnight; the chart window gets
lines and burner minutes per hour, and the Alerts page a chart button.
"""
import datetime as dt
from unittest import mock

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

T0 = dt.datetime(2026, 1, 15, 4, 0)               # 11 pm New York: the controller's midnight falls inside


@pytest.fixture
def client(app):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def ctrl(session):
    import app.models as m
    c = m.Client(name="Acme"); session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Main St"); session.add(p); session.flush()
    b = m.Building(property_id=p.id, name="12 Main St"); session.add(b); session.flush()
    k = m.Controller(building_id=b.id, name="Boiler room", tailscale_ip="100.64.0.7", fleet_api_token="x" * 64)
    session.add(k); session.commit()
    return k


def status(minute):
    """What /api/status says `minute` minutes after T0: boiler 1 heats half
    of every hour; the counters reset at the controller's midnight (60 min in)."""
    since_midnight = (minute - 60) if minute >= 60 else minute + 23 * 60
    heat = (since_midnight // 60) * 1800 + min(since_midnight % 60, 30) * 60
    return {"bestOutdoor": 30, "avgAptTemp": 68.4, "tit": 68, "aquastat1": 171, "supTemp": 150, "stackTemp": 330,
            "coil2Temp": 0, "blr1HtDaySecs": heat, "blr1HwDaySecs": 0, "aptTemps": [69, 0, 66] + [0] * 125}


def test_snapshots_every_5_minutes_and_the_chart(app, session, ctrl, client):
    import app.models as m
    from app import ctrl_history
    kept = [ctrl_history.record(ctrl.id, status(k), T0 + dt.timedelta(minutes=k)) for k in range(0, 180)]
    assert sum(kept) == 36                                                      # one a poll, kept every 5 min
    r = m.ControllerReading.query.first()
    assert r.values["inside"] == 68.4 and r.values["heat1"] is not None and r.apt == [69, 0, 66]
    assert "coil2" not in r.values or r.values["coil2"] == 0

    session.add(m.ControllerStatus(controller_id=ctrl.id, ok=True, polled_at=T0, raw_json=status(0)))
    session.commit()
    cat = [c["key"] for c in ctrl_history.catalog(ctrl)]
    assert cat[:3] == ["out", "inside", "dit"] and "coil2" not in cat and "apt0" in cat and "apt1" not in cat
    d = ctrl_history.series(ctrl, 1, ["inside", "apt2", "nonsense"], now=T0 + dt.timedelta(hours=3))
    assert list(d["series"]) == ["inside", "apt2"] and d["series"]["apt2"][0] == 66
    assert d["bars"]["heat"] == [30, 30, 30] and d["note"]                       # 30 min an hour across midnight
    assert client.get(f"/fleet/{ctrl.id}/series.json?days=1&keys=inside").status_code == 200


def test_a_poll_keeps_a_snapshot(app, session, ctrl):
    import app.models as m
    from app.fleet_poller import poll_controller
    resp = mock.Mock(status_code=200, json=lambda: status(0) | {"buildingName": "12 Main St", "version": "x"})
    resp.raise_for_status = lambda: None
    with mock.patch("app.fleet_poller.requests.get", return_value=resp):
        poll_controller(app, ctrl)
    assert m.ControllerReading.query.filter_by(controller_id=ctrl.id).count() == 1


def test_alerts_page_has_controller_chart_buttons(app, session, ctrl, client):
    import app.models as m
    session.add(m.AlertState(controller_id=ctrl.id, alerting=True, opened_at=T0, issue="Offline for 12 min"))
    session.add(m.AlertEvent(controller_id=ctrl.id, controller_name=ctrl.name, kind="open", title="ALERT: Boiler room",
                             text="Heating alarm: Safety", delivered="ntfy ok", created_at=T0))
    session.commit()
    page = client.get("/alerts").data.decode()
    assert 'data-ctrl="%d"' % ctrl.id in page and 'data-k="inside,out"' in page             # offline
    assert 'data-k="stack1,aqua1,supp,inside"' in page and "sensorchart.js" in page          # heating alarm


def test_controller_page_and_card_show_target_supply(app, session, ctrl, client):
    import app.models as m
    raw = status(0) | {"heatCtrlType": 20, "dSupTemp": 160, "supTemp": 78, "hwrTemp": 100, "heatDemand": True}
    session.add(m.ControllerStatus(controller_id=ctrl.id, ok=True, polled_at=dt.datetime.utcnow(), raw_json=raw))
    session.commit()
    page = client.get(f"/fleet/{ctrl.id}").data.decode()
    assert "Target supply" in page and 'data-ctrl="%d"' % ctrl.id in page and "sensorchart.js" in page
    assert 'class="lv-t t-cold" data-k="dsupp,supp,hwr"' in page and "Hot water cold: supply 78 F, under 80 F" in page
    card = client.get("/overview").data.decode()
    assert "Target supply" in card and "160°" in card and 'class="v low" title="Hot water cold: supply 78 F' in card

