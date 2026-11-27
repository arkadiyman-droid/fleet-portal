"""The Utilities page and the LL84 refresh (Postgres tier): NYC benchmarking
rows matched to the portfolio's buildings, stored with every year, LL97
exposure on the page, and links a person set kept through a refresh."""
import pytest

from tests.conftest import requires_postgres
from tests.test_benchmark import row

pytestmark = requires_postgres


def _year(pid, addr, year, **kw):
    base = dict(site_eui_kbtu_ft="120", weather_normalized_site_eui="118", natural_gas_use_kbtu="9000000",
                electricity_use_grid_purchase_1="300000", water_use_all_water_sources="4000",
                property_gfa_self_reported="100000", multifamily_housing_total="100", energy_star_score="20",
                property_name=addr, nyc_borough_block_and_lot="3012340001")
    base.update(kw)
    return row(pid, addr, year=str(year), **base)


@pytest.fixture
def world(session):
    import app.models as m
    c = m.Client(name="PATHS")
    session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Betty and Medgar")
    q = m.Property(client_id=c.id, name="Far Away")
    session.add_all([p, q]); session.flush()
    b1, b2 = m.Building(property_id=p.id, name="590-600 Gates Ave"), m.Building(property_id=p.id, name="690-700 Gates Ave")
    b3 = m.Building(property_id=q.id, name="627 Alan Page Dr", address="627 Alan Page Dr, Canton, OH")
    session.add_all([b1, b2, b3]); session.commit()
    return {"b1": b1.id, "b2": b2.id, "b3": b3.id}


ROWS = [_year("100", "590 Gates Ave", 2023), _year("100", "590 Gates Ave", 2024),
        _year("200", "690 Gates Ave", 2024, natural_gas_use_kbtu="2000000", site_eui_kbtu_ft="60",
              weather_normalized_site_eui="58"),
        row("300", "1 Other St", site_eui_kbtu_ft="90", weather_normalized_site_eui="88")]


def test_a_refresh_stores_what_the_buildings_need(app, session, world):
    import app.models as m
    from app import benchmark as bm
    res = bm.refresh(ROWS)
    assert res["properties"] == 2 and res["linked"] == 2
    assert {p.id for p in m.BenchmarkProperty.query.all()} == {"100", "200"}           # only ours are kept
    assert m.BenchmarkYear.query.filter_by(property_id="100").count() == 2
    assert session.get(m.BuildingBenchmark, world["b1"]).property_id == "100"
    assert session.get(m.BuildingBenchmark, world["b3"]) is None                       # Canton, OH: not in LL84
    st = session.get(m.BenchmarkStat, ("Multifamily Housing", 2024))
    assert st and st.n == 3


def test_a_link_a_person_set_survives_a_refresh(app, session, world):
    import app.models as m
    from app import benchmark as bm
    bm.refresh(ROWS)
    x = session.get(m.BuildingBenchmark, world["b2"])
    x.property_id, x.how = "100", "manual"
    session.commit()
    bm.refresh(ROWS)
    assert session.get(m.BuildingBenchmark, world["b2"]).property_id == "100"


def test_the_utilities_page(app, session, world):
    from app import benchmark as bm
    bm.refresh(ROWS)
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    html = c.get("/utilities").get_data(as_text=True)
    assert "Utilities" in html and "LL97 exposure" in html and "Efficiency against NYC" in html
    # 590 Gates: 9,000,000 kBtu gas + 300,000 kWh = 478 + 86.7 = 564.6 t against a 675 t limit, 334.7 t in 2030
    assert "590 Gates Ave" in html and ">565<" in html and ">675<" in html
    assert "627 Alan Page Dr" in html                                                  # listed as having no NYC report
    r = c.post("/utilities/link", data={"building_id": world["b3"], "property_id": "none"}, follow_redirects=True)
    assert "marked as having no NYC benchmarking report" in r.get_data(as_text=True)
    other = app.test_client()
    assert other.get("/utilities").status_code in (302, 403)
