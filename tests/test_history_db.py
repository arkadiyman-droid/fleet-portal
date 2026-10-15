"""
tests/test_history_db.py — the 2010 boxes' history end to end (Postgres tier)

One cold January day for two buildings goes in as reads (the shape the
poller stores); the rollups must produce hourly and daily rows, analytics
must rank the cold, wasteful building first, and the pages, JSON and CSV
must serve it. A real poll cycle must write reads and change events.
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres
from tests.test_legacy_db import fake_poll

pytestmark = requires_postgres

DAY = dt.date(2026, 1, 15)
START = dt.datetime(2026, 1, 15, 5, 0)            # midnight in New York (EST)
NOW = dt.datetime(2026, 1, 16, 6, 0)


@pytest.fixture
def client(app):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def two(session):
    import app.models as m
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    session.add(login); session.flush()
    good = m.LegacyBox(name="Good Bldg", host="192.0.2.7", port=8060, identity="gateway", login_id=login.id)
    cold = m.LegacyBox(name="Cold Bldg", host="192.0.2.7", port=8061, identity="gateway", login_id=login.id)
    session.add_all([good, cold]); session.commit()
    return good, cold


def _data(minute, cold):
    """One read: minute = minutes since New York midnight."""
    hour = minute // 60
    night = hour < 6 or hour >= 22
    burn = 40 if cold else 20                         # heat minutes per hour
    st = {
        "callForHeat": "Safety" if cold and 600 <= minute < 660 else ("DTime Rem" if minute % 60 < burn else "Outsd Sat"),
        "averageTemperature": "64" if cold else "70", "targetInsideTemperature": "68",
        "stackTemperature": "520" if cold else "350", "highStackTemp": "560",
        "boiler1HeatRunMinutes": str(hour * burn + min(minute % 60, burn)),
        "boiler1HWtrRunMinutes": str(hour * (30 if cold else 8) + min(minute % 60, 30 if cold else 8)),
        "systemTemperature0": "20", "systemTemperature1": "150",
        "apartmentTemperature0": "70", "apartmentTemperature1": ("58" if cold and night else "69"),
        "apartmentTemperature2": "0",
    }
    return {"status": st, "names": {"systemSensorName0": "Out", "systemSensorName1": "Aqu1"}, "alarm": None}


def _fill(app, good, cold):
    from app import history
    reads = []
    for minute in range(0, 24 * 60 + 60, 5):
        ts = START + dt.timedelta(minutes=minute)
        m = minute % (24 * 60)
        reads.append(history.reading(good.id, ts, _data(m, False)))
        reads.append(history.reading(cold.id, ts, _data(m, True)))
    history.save(reads, [{"box_id": cold.id, "ts": START + dt.timedelta(hours=10), "kind": "status",
                          "code": "Safety", "text": "Desired Time Remaining -> Safety"}], NOW)


def test_rollups(app, session, two):
    import app.models as m
    from app import history
    good, cold = two
    _fill(app, good, cold)
    hours, days = history.rollup(NOW, poll_min=5, max_hours=100)
    assert hours == 24 and days == 1
    h = session.get(m.LegacyHourly, (cold.id, START + dt.timedelta(hours=2)))   # 2 am New York
    assert h.n == 12 and h.out_avg == 20 and not h.out_fleet
    assert h.heat_min == 40 and h.law_req == 62 and h.law_low == 1 and h.apt_avg == [70, 58]
    assert abs(h.hdd - 45 / 24) < 1e-3
    d = session.get(m.LegacyDaily, (cold.id, DAY))
    assert d.hours == 24 and d.heat_min == 24 * 40
    assert d.safety_min == 60 and d.law_hours == 8 and d.apt_low == 58
    g = session.get(m.LegacyDaily, (good.id, DAY))
    assert g.law_hours == 0 and g.heat_min < d.heat_min
    again = history.rollup(NOW, poll_min=5)
    assert again == (0, 0)                                                    # nothing left to sum


def test_analytics_and_pages(app, session, two, client):
    from app import analytics, history
    good, cold = two
    _fill(app, good, cold)
    history.rollup(NOW, poll_min=5, max_hours=100)
    a = analytics.portfolio("7", now=NOW)
    by = {r["box"].name: r for r in a["rows"]}
    assert by["Cold Bldg"]["mpd"] > by["Good Bldg"]["mpd"] and by["Cold Bldg"]["trips"] == 1
    assert a["kpi"]["law_boxes"] == 1 and a["law"][cold.id]["worst"][0][0] == 2     # sensor #2
    top = a["findings"][0]
    assert top["box"].name == "Cold Bldg" and top["cat"] == "Heat-season minimum"
    assert any(f["cat"] == "Safety" for f in a["findings"])

    import app.analytics as an
    an_now = NOW
    orig = an.portfolio
    an.portfolio = lambda key, now=None, poll_min=5: orig(key, now=an_now, poll_min=poll_min)
    try:
        page = client.get("/analytics?period=7").data.decode()
        assert "Cold Bldg" in page and "Heat-season minimum" in page and "Every building" in page
        csv = client.get("/analytics.csv?period=7").data.decode()
        assert csv.splitlines()[0].startswith("building,answered_pct") and "Cold Bldg" in csv
        assert "sensorchart.js" in page and 'data-box="%d"' % cold.id in page             # temperatures open the chart
        assert 'data-k="inside"' in page and 'data-k="apt1"' in page                       # worst sensor #2 -> apt1
    finally:
        an.portfolio = orig

    orig_series = an.box_series
    an.box_series = lambda box_id, days, now=None: orig_series(box_id, days, now=NOW)
    try:
        js = client.get(f"/live/{cold.id}/history.json?days=2").get_json()
        assert js["res"] == "every read" and len(js["t"]) == len(js["lines"]["inside"]) > 280
        assert js["bars"]["step"] == "hour" and js["apt"]["sensors"] == [1, 2]
        assert js["events"][0]["kind"] == "status"
    finally:
        an.box_series = orig_series
    box = client.get(f"/live/{cold.id}").data.decode()
    assert "Day by day" in box and "charts.js" in box
    raw = client.get(f"/live/{cold.id}/history.csv?res=daily&days=365").data.decode()
    assert raw.splitlines()[0].startswith("day,n,hours") and "2026-01-15" in raw


def test_a_poll_cycle_writes_reads_and_events(app, session):
    import app.models as m
    from app import legacy
    login = m.LegacyLogin(label="main", username="Manager", password="pw")
    session.add(login); session.flush()
    b = m.LegacyBox(name="10-12 Bedell St", host="192.0.2.7", port=8060, identity="gateway", login_id=login.id)
    session.add(b); session.commit()
    t0 = dt.datetime.utcnow()
    legacy.poll_all(app, now=t0, poll=fake_poll)
    for k in (5, 10, 15):                                  # dark is logged once, after two misses
        legacy.poll_all(app, now=t0 + dt.timedelta(minutes=k),
                        poll=lambda j, now: {"id": j["id"], "polled_at": now, "ok": False, "error": "no answer"})
    session.expire_all()
    assert m.LegacyReading.query.filter_by(box_id=b.id).count() == 1
    assert [e.kind for e in m.LegacyEvent.query.filter_by(box_id=b.id)] == ["dark"]


def test_the_live_pop_up_chart(app, session, two, client):
    import app.models as m
    from app import analytics
    good, cold = two
    _fill(app, good, cold)
    session.add(m.LegacyStatus(box_id=cold.id, polled_at=NOW, ok=True, last_ok_at=NOW, data=_data(600, True)))
    session.commit()
    keys = [c["key"] for c in analytics.sensor_catalog(cold)]
    assert keys[:3] == ["out", "inside", "dit"] and "sys0" in keys and "apt1" in keys and "apt2" not in keys
    d = analytics.box_sensor_series(cold, 1, ["supp", "sys0", "apt1", "nonsense"], now=NOW)
    assert list(d["series"]) == ["supp", "sys0", "apt1"] and d["res"] == "every read"
    assert len(d["t"]) == len(d["series"]["supp"]) and d["series"]["sys0"][0] == 20
    assert d["labels"]["sys0"] == "Out" and d["range"][1] - d["range"][0] == 86400000
    assert sum(d["bars"]["heat"]) >= 900 and len(d["bars"]["t"]) >= 23             # 40 min an hour, from the counters
    from app import history
    history.rollup(NOW, poll_min=5, max_hours=100)
    h = analytics.box_sensor_series(cold, 30, ["inside", "sys0"], now=NOW)
    assert h["res"] == "hourly averages" and h["note"] and set(h["series"]["sys0"]) == {None}   # box sensors: reads only
    assert len(h["t"]) == 24 and h["series"]["inside"][0] == 64 and sum(h["bars"]["heat"]) == 960
    r = client.get(f"/live/{cold.id}/series.json?days=7&keys=inside,out")
    assert r.status_code == 200 and r.get_json()["name"] == "Cold Bldg"
    page = client.get("/live?view=table").data.decode()
    assert 'data-box="%d"' % cold.id in page and "sensorchart.js" in page and "http-equiv" not in page
    box = client.get(f"/live/{cold.id}").data.decode()                       # the box page: same chart, by click
    assert "sensorchart.js" in box and 'data-k="apt1"' in box and 'data-k="sys0"' in box and 'data-k="inside"' in box
