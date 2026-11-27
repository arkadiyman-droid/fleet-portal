"""Importing Yardi utility payables (Postgres tier): accounts and bills land
on the right property and building, a re-import replaces them without
doubling, a building link a person set survives it, and the Utilities page
shows the costs and the bills to check."""
import datetime as dt

import pytest

from tests.conftest import requires_postgres
from tests.test_yardi_bills import row

pytestmark = requires_postgres


@pytest.fixture
def world(session):
    import app.models as m
    c = m.Client(name="PATHS")
    session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Twin and Grote", unit_count=100)
    q = m.Property(client_id=c.id, name="Longwood")
    session.add_all([p, q]); session.flush()
    b1, b2 = m.Building(property_id=p.id, name="725-735 Garden st Z1"), m.Building(property_id=p.id, name="2260 Crotona Ave")
    b3 = m.Building(property_id=q.id, name="775 Southern blvd.")
    session.add_all([b1, b2, b3]); session.commit()
    return {"p": p.id, "q": q.id, "b1": b1.id, "b2": b2.id, "b3": b3.id}


def _lines():
    from app import yardi_bills as yb
    rows = [
        row("Electricity 01/25/2025-02/24/2025 725 GARDENST ENTM", 20000.0, inv="26271-11000-4-HBB25033012395", prop="twinpark"),
        row("Electricity 01/25/2026-02/24/2026 725 GARDENST ENTM", 25000.0, inv="26271-11000-4-HBB26033012395", prop="twinpark"),
        row("Electricity 01/25/2026-02/24/2026 2260 CROTONA AVE ENTM", 9000.0, inv="25319-14000-7-HBB26033012410", prop="twinpark"),
        row("Gas 12/02/2025-12/31/2025 775 SOUTHERN BLVD", 13204.10, inv="52487-85000-1-AJX26010310531", prop="longwood", gl="512-020"),
        row("Gas 12/02/2025-12/31/2025 775 SOUTHERN BLVD", 13204.10, inv="52487-85000-1-PSA26010622668", prop="longwood", gl="512-020"),
        row("Water 12/02/2025-12/31/2025 1 Far Away Rd", 777.0, inv="9-HBB26033012499", prop="westvill", gl="512-040",
            payee="City of Ithaca"),
    ]
    return [yb.parse_line(r) for r in rows]


def test_an_import_files_accounts_and_bills(app, session, world):
    import app.models as m
    from app import yardi_bills as yb
    res = yb.import_lines(_lines(), source_ref="test")
    assert res["accounts"] == 4 and res["bills"] == 5 and res["unmatched"] == ["westvill"]
    garden = m.UtilityAccount.query.filter_by(account_number="26271-11000-4").one()
    assert garden.property_id == world["p"] and [x.building_id for x in garden.buildings] == [world["b1"]]
    assert sorted(b.cost_cents for b in garden.bills) == [2000000, 2500000]
    lw = m.UtilityAccount.query.filter_by(account_number="52487-85000-1").one()
    assert lw.commodity == "gas" and lw.bills[0].flags == "twice" and lw.bills[0].invoices == 2
    # again: replaced, not doubled
    yb.import_lines(_lines(), source_ref="test")
    assert m.UtilityAccount.query.filter_by(source="yardi").count() == 4
    assert m.UtilityBill.query.count() == 5


def test_a_person_set_building_link_survives_a_reimport(app, session, world):
    import app.models as m
    from app import yardi_bills as yb
    yb.import_lines(_lines())
    a = m.UtilityAccount.query.filter_by(account_number="25319-14000-7").one()
    a.buildings[:] = [m.UtilityAccountBuilding(building_id=world["b1"], share=0.5),
                      m.UtilityAccountBuilding(building_id=world["b2"], share=0.5)]
    a.links_set_by = "tester"
    session.commit()
    yb.import_lines(_lines())
    a = m.UtilityAccount.query.filter_by(account_number="25319-14000-7").one()
    assert sorted((x.building_id, x.share) for x in a.buildings) == sorted([(world["b1"], 0.5), (world["b2"], 0.5)])
    assert a.links_set_by == "tester"


def test_the_summary_and_the_page(app, session, world):
    from app import yardi_bills as yb
    yb.import_lines(_lines())
    s = yb.summary(today=dt.date(2026, 11, 27))
    rows = {r["name"]: r for r in s["rows"]}
    tg = rows["Twin and Grote"]
    assert tg["years"][2025] == 20000.0 and tg["years"][2026] == 34000.0
    assert s["ref"] == 2025 and tg["per_unit"] == 200.0
    assert s["others"][0]["code"] == "westvill" and s["others"][0]["total"] == 777.0
    assert len(s["checks"]) == 1 and s["checks"][0]["property"] == "Longwood"
    assert round(s["check_total"], 2) == 13204.10
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as x:
        x["superuser"] = True
    html = c.get("/utilities").get_data(as_text=True)
    assert "Utility costs by property (Yardi)" in html and "Twin and Grote" in html
    assert "paid twice?" in html and "$26,408" in html
    assert "Yardi properties not in the portal" in html and "westvill" in html
