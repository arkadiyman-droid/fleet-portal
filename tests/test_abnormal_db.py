"""The abnormal-box warnings on the Live wall and a box's page (Postgres tier):
a box reporting a target its tables do not give, and a box heating apartments
that are already well over target."""
import datetime as dt

import pytest

from tests.conftest import requires_postgres
from tests.test_abnormal import settings

pytestmark = requires_postgres


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def world(session):
    """Three live hot-water boxes: Odd Target reports 150 (tables say 70-71),
    Too Warm keeps firing with apartments 8 over target, Fine is fine."""
    import app.models as m
    now = dt.datetime.utcnow().replace(microsecond=0)
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    session.add(login); session.flush()
    out = {}
    for name, port, tit, avg in (("Odd Target", 8001, 150, 72), ("Too Warm", 8002, 71, 79), ("Fine", 8003, 71, 71)):
        b = m.LegacyBox(name=name, host="192.0.2.9", port=port, identity="gateway", login_id=login.id)
        session.add(b); session.flush()
        data = {"read_at": now.isoformat(), "config": {"heatControlType": "13"},
                "status": {"targetInsideTemperature": str(tit), "averageTemperature": str(avg), "systemTemperature0": "50",
                           "serverTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "callForHeat": "Circ Pump"}}
        session.add(m.LegacyStatus(box_id=b.id, polled_at=now, ok=True, last_ok_at=now, data=data))
        session.add(m.SettingSnapshot(source="legacy", source_id=b.id, at=now, data=settings()))
        # three hours of reads, every 5 minutes; the heat counter climbs 4 minutes a read (the burner ran)
        for i in range(37):
            ts = now - dt.timedelta(minutes=5 * (36 - i))
            dit = 71 if name != "Odd Target" else 150
            session.add(m.LegacyReading(box_id=b.id, ts=ts, inside=avg, dit=dit, heat1=100 + 4 * i, code="Circ Pump"))
        out[name] = b.id
    session.commit()
    return out


def test_the_live_wall_shows_and_filters_abnormal_boxes(client, world):
    page = client.get("/live").get_data(as_text=True)
    assert "Abnormal 2" in page                                            # the filter, with its count
    odd = client.get("/live?show=abnormal").get_data(as_text=True)
    assert "Odd Target" in odd and "Too Warm" in odd and "Fine" not in odd
    # read the issue panel's JSON the way the page does, so the degree sign is a real one
    import json, re
    panel = json.loads(re.search(r'id="lv-issues">(.*?)</script>', odd, re.S).group(1))
    said = [p for i in panel for p in i['problems']]
    assert any(p.startswith('Indoor target is 150°') for p in said)
    assert any(p.startswith('Overheating: the apartments averaged 8° over target') for p in said)


def test_a_box_page_lists_what_its_settings_do_not_explain(client, world):
    html = client.get(f"/live/{world['Odd Target']}").get_data(as_text=True)
    assert "look abnormal at this box" in html or "looks abnormal at this box" in html
    assert "Indoor target is 150°" in html
    fine = client.get(f"/live/{world['Fine']}").get_data(as_text=True)
    assert "abnormal at this box" not in fine


def test_overheating_needs_hours_of_it_and_the_burner_running(session, world):
    import app.models as m
    from app import abnormal
    now = dt.datetime.utcnow()
    hot = abnormal.overheating(list(world.values()), now)
    assert set(hot) == {world["Too Warm"]}
    # the same warm apartments with the burner idle: not heating a warm building, so not flagged
    session.query(m.LegacyReading).filter_by(box_id=world["Too Warm"]).update({"heat1": 500})
    session.commit()
    assert abnormal.overheating(list(world.values()), now) == {}
