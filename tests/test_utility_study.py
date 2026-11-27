"""app/utility_study.py — one property's utilities as matrices: bills spread
over the months they cover, the weather fit, the cached weather, and the
page (Postgres tier for the last ones)."""
import datetime as dt
import json

import pytest

from app import utility_study as us
from tests.conftest import requires_postgres

def day(*a):
    return dt.date(*a)


def test_a_bill_is_spread_over_its_months_by_days():
    parts = us.spread(day(2025, 1, 25), day(2025, 2, 24))                     # 7 days of January, 24 of February
    assert [us.mlabel(k) for k, _ in parts] == ["2025-01", "2025-02"]
    assert round(parts[0][1], 4) == round(7 / 31, 4) and round(parts[1][1], 4) == round(24 / 31, 4)
    assert us.spread(day(2025, 3, 1), day(2025, 3, 31)) == [(us.mkey(day(2025, 3, 1)), 1.0)]
    assert sum(s for _, s in us.spread(day(2024, 6, 9), day(2025, 6, 8))) == pytest.approx(1.0)   # a year-long bill
    assert us.covered_days(day(2025, 1, 25), day(2025, 2, 24)) == {us.mkey(day(2025, 1, 1)): 7, us.mkey(day(2025, 2, 1)): 24}
    assert us.mdays(us.mkey(day(2024, 2, 1))) == 29


def test_the_weather_line_splits_base_load_from_heating():
    pts = [(x, 12.0 + 3.5 * x) for x in (0, 2, 5, 10, 18, 25, 30, 33)]  # 12 therms a day + 3.5 per degree-day
    base, slope, r2 = us.fit(pts)
    assert base == pytest.approx(12.0) and slope == pytest.approx(3.5) and r2 == pytest.approx(1.0)
    assert us.fit(pts[:5]) is None                                       # too few months to say
    dd = us.degree_days({"2025-01-01": 30.0, "2025-01-02": 70.0}, [us.mkey(day(2025, 1, 1))])
    assert dd[us.mkey(day(2025, 1, 1))] == (35.0, 5.0, 2)


def test_weather_is_cached_and_survives_being_offline(tmp_path, monkeypatch):
    calls = []

    def fake(lat, lon, start, end):
        calls.append((lat, lon))
        return {"2025-01-01": 30.0}
    monkeypatch.setattr(us, "fetch_daily", fake)
    now = dt.datetime(2026, 11, 27, 12)
    assert us.daily_temps(40.78, -73.97, day(2023, 1, 1), day(2026, 11, 20), str(tmp_path), now) == {"2025-01-01": 30.0}
    us.daily_temps(40.78, -73.97, day(2023, 1, 1), day(2026, 11, 20), str(tmp_path), now)
    assert len(calls) == 1                                               # the second read is the cache

    def down(*a):
        raise OSError("offline")
    monkeypatch.setattr(us, "fetch_daily", down)
    later = now + dt.timedelta(days=10)
    assert us.daily_temps(40.78, -73.97, day(2023, 1, 1), day(2026, 11, 20), str(tmp_path), later) == {"2025-01-01": 30.0}
    (tmp_path / "weather_places.json").write_text(json.dumps({"7": [41.70, -73.92, "Poughkeepsie, NY"]}))
    assert us.place_for(7, str(tmp_path)) == (41.70, -73.92, "Poughkeepsie, NY")
    assert us.place_for(8, str(tmp_path)) == us.DEFAULT_PLACE


@requires_postgres
def test_the_property_page(app, session, monkeypatch, tmp_path):
    import app.models as m
    from app import yardi_bills as yb
    from tests.test_yardi_bills import row
    c = m.Client(name="PATHS")
    session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Twin and Grote", unit_count=100)
    session.add(p); session.flush()
    session.add(m.Building(property_id=p.id, name="725-735 Garden st Z1")); session.commit()
    lines = [yb.parse_line(r) for r in [
        row("Electricity 01/25/2025-02/24/2025 725 GARDENST ENTM", 20000.0, inv="26271-11000-4-HBB25033012395", prop="twinpark"),
        row("Gas 01/01/2025-01/31/2025 725 GARDENST ENTM", 9000.0, inv="25507-71000-9-AJX25032918296", prop="twinpark",
            gl="512-020"),
        row("Gas 02/01/2025-02/28/2025 725 GARDENST ENTM", 9000.0, inv="25507-71000-9-AJX25042914972", prop="twinpark",
            gl="512-020"),
        row("Gas 02/01/2025-02/28/2025 725 GARDENST ENTM", 9000.0, inv="25507-71000-9-AJX25042914999", prop="twinpark",
            gl="512-020"),                                                # the same month paid twice
        row("Gas 04/01/2025-04/30/2025 725 GARDENST ENTM", 3000.0, inv="25507-71000-9-AJX25062914972", prop="twinpark",
            gl="512-020"),                                                # March is missing: a gap
    ]]
    usage = yb.usage_index([
        ["26271-11000-4", "HBB25033012395", [["electric", "725 GARDENST ENTM", "CA", 15000.0, 100000, "kWh"]]],
        ["25507-71000-9", "AJX25032918296", [["gas", "725 GARDENST ENTM", "CA", 9000.0, 6000, "Therms"]]],
    ])
    yb.import_lines(lines, usage=usage)
    monkeypatch.setattr(us, "fetch_daily", lambda *a: {})
    s = us.study(p, today=day(2026, 11, 27), instance_path=str(tmp_path))
    i = s["months"].index("2025-01")
    assert s["fuels"]["electric"]["cost"][i] == pytest.approx(20000 * 7 / 31, abs=0.01)
    assert s["fuels"]["electric"]["usage"][i + 1] == pytest.approx(100000 * 24 / 31, abs=0.1)
    assert s["fuels"]["gas"]["price"][i] == pytest.approx(1.5)            # $9,000 / 6,000 therms
    gas = [x for x in s["meters"] if x["fuel"] == "gas"][0]
    assert gas["status"][i + 1] == "twice" and gas["status"][i + 2] == "gap" and gas["gaps"] == 1
    assert s["weather"] is None                                          # no temperatures: no weather section data
    app.config["UTILITY_WEATHER"] = False
    app.config["WTF_CSRF_ENABLED"] = False
    cl = app.test_client()
    with cl.session_transaction() as x:
        x["superuser"] = True
    html = cl.get(f"/utilities/property/{p.id}").get_data(as_text=True)
    assert "Twin and Grote" in html and "Meters × month" in html and 'id="us-data"' in html
    assert f"/utilities/property/{p.id}" in cl.get("/utilities").get_data(as_text=True)   # linked from the page
    assert cl.get("/utilities/property/999999").status_code == 404
    with cl.session_transaction() as x:
        x["superuser"] = False
    assert cl.get(f"/utilities/property/{p.id}").status_code in (302, 403)


def test_a_month_far_off_the_line_is_left_out_of_it():
    pts = [(x, 12.0 + 3.5 * x + (0.3 if i % 2 else -0.3)) for i, x in enumerate((0, 2, 5, 10, 18, 25, 30, 33))]
    pts.append((8, 400.0))                                               # a catch-up bill
    base, slope, r2, out = us.fit_robust(pts)
    assert out == [8] and base == pytest.approx(12.0, abs=0.5) and slope == pytest.approx(3.5, abs=0.05) and r2 > 0.99


@requires_postgres
def test_nyc_flat_rate_water_is_spread_over_the_year_it_pays_for(app, session, tmp_path):
    import app.models as m
    from app import yardi_bills as yb
    from tests.test_yardi_bills import row
    c = m.Client(name="PATHS")
    session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="River Park", unit_count=1654)
    session.add(p); session.flush()
    session.add(m.Building(property_id=p.id, name="10-20 Richman Plaza", address="10-20 Richman Plaza, Bronx, NY"))
    session.commit()
    w = dict(prop="river", gl="512-040", payee="NYC Water Board")
    lines = [yb.parse_line(row("Water 07/01/2025-07/31/2025 10-20 RICHMAN PZ", 12000.0,
                               inv="9000167642001-HTX25070900129", **w))]
    yb.import_lines(lines, usage=yb.usage_index([["9000167642001", "HTX25070900129",
                                                  [["water", "10-20 RICHMAN PZ", "CA", 12000.0, 0, "Gal"]]]]))
    s = us.study(p, today=day(2026, 11, 27), instance_path=str(tmp_path), weather=False)
    assert s["mcp"] is True
    i = s["months"].index("2025-07")
    assert s["fuels"]["water"]["cost"][i] == pytest.approx(12000 * 31 / 365, abs=0.01)
    assert sum(v for v in s["fuels"]["water"]["cost"] if v) == pytest.approx(12000, abs=0.05)
    mtr = s["meters"][0]
    assert mtr["gaps"] == 0 and mtr["status"][i + 5] == "flat"            # covered all year, no false gaps
