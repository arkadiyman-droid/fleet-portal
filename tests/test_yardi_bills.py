"""app/yardi_bills.py — reading Yardi's Expense Distribution export into
utility accounts and bills. Pure (no database). The rows below are written
the way the real export writes them (2026-11-27)."""
import datetime as dt

from app import yardi_bills as yb


def row(note, amount, inv="15912-81000-7-HBB26102007499", prop="river", payee="Con Edison", gl="512-010",
        period=dt.datetime(2026, 10, 1), control="P-1"):
    return {"Account Code": gl, "Property": prop, "Payee Name": payee, "Invoice #": inv, "Amount": amount,
            "Notes": note, "Period": period, "Payable Control": control}


def test_account_numbers_come_out_of_the_invoice_number():
    assert yb.account_number("15912-81000-7-HBB26102007499 ") == "15912-81000-7"
    assert yb.account_number("68157-42000-8-deposit-ZHX26101") is None          # cut short in the export
    assert yb.account_number("68157-42000-8-deposit-ZHX26101234") == "68157-42000-8"
    assert yb.account_number("071-013-519-0-1-CCX26093000042") == "071-013-519-0-1"
    assert yb.account_number("1151013491") is None                               # not <account>-<batch>
    assert yb.account_number("586742-25821-Wesley-10/26") is None


def test_service_addresses_are_keyed_for_matching():
    assert yb.street_key("725 GARDENST ENTM") == (725, "garde")
    assert yb.street_key("725 Garden St Entm") == (725, "garde")
    assert yb.street_key("801 SOUTH ST MDRP") == (801, "s")
    assert yb.street_key("195 1st St HSE 2") == (195, "1")
    assert yb.street_key("130 East 104th Street") == (130, "104")
    assert yb.street_key("125-30 SUTPHIN BLVD PLPW") == ("125-30", "sutph")      # Queens: one number
    assert yb.street_key("[700-5F] 700 Gates Ave # 5F    Brooklyn  NY - 11221") == (700, "gates")
    assert yb.street_key("Apartments") is None
    assert yb.building_key("590-600 Gates Ave") == (590, 600, "gates")
    assert yb.building_key("801 South St North Zone") == (801, 801, "s")
    assert yb.building_key("125-60 Sutphin Blvd") == ("125-60", "125-60", "sutph")


def test_a_line_is_read_whole():
    ln = yb.parse_line(row("Electricity 08/31/2026-09/30/2026 20 RICHMAN PLZ", 168395.59))
    assert ln["start"] == dt.date(2026, 8, 31) and ln["end"] == dt.date(2026, 9, 30)
    assert ln["address"] == "20 RICHMAN PLZ" and ln["desc"] == "Electricity" and ln["kind"] == "building"
    assert ln["account"] == "15912-81000-7" and ln["amount"] == 168395.59
    v = yb.parse_line(row("Vacant Electric 09/03/2019-09/29/2019 [DI-6028] 6602 District Heights Pkwy # 8", 52.66))
    assert v["kind"] == "vacant" and v["address"].startswith("6602 District")
    assert yb.parse_line(row("Master Unit Electric 07/10/2026-08/10/2026 915 ALAN PAGE DR SE APT 4", 2.66))["kind"] == "unit"
    assert yb.parse_line(row("Electricity 08/26/2026-09/25/2026 700 Gates Ave 3C", 99.72))["kind"] == "unit"
    assert yb.parse_line(row("Gas Deposit 08/03/2026-08/03/2026 20 Richman PLAZ CHP", 32237.0))["kind"] == "deposit"
    assert yb.parse_line(row("Wesley Second Installment payment NRG", 26888.01, inv="586742-25821-Wesley-10/26"))["kind"] == "other"
    assert yb.parse_line({"Account Code": "Total 512-050", "Amount": 11127.88}) is None


def _props():
    return [{"id": 1, "name": "Twin and Grote", "buildings": [(11, "725-735 Garden st Z1", None), (12, "2260 Crotona Ave", None),
                                                              (13, "2311 Southern Blvd", None)]},
            {"id": 2, "name": "River Park", "buildings": [(21, "RP 10AH", None)]},
            {"id": 3, "name": "Southern", "buildings": [(31, "737 Southern blvd", None)]},
            {"id": 4, "name": "Plaza", "buildings": [(41, "23 New Lots Ave", None)]}]


def test_yardi_properties_match_by_address_then_name():
    lines = [yb.parse_line(r) for r in [
        row("Electricity 08/25/2026-09/24/2026 725 GARDENST ENTM", 100, prop="twinpark"),
        row("Electricity 08/27/2026-09/28/2026 2311 SOUTHERN BLVD ENTM", 100, prop="grote"),
        row("Electricity 08/31/2026-09/30/2026 20 RICHMAN PLZ", 100, prop="river"),
        row("Electricity 08/31/2026-09/30/2026 1 Tulsa Way", 100, prop="plazahil"),
        row("Electricity 08/31/2026-09/30/2026 9 Nowhere St", 100, prop="westvill")]]
    m = yb.match_properties(lines, _props())
    assert m["twinpark"] == (1, "address") and m["grote"] == (1, "address")    # 2311 Southern is Twin & Grote's
    assert m["river"] == (2, "name")
    assert m["plazahil"] == (None, None)                                          # not Brooklyn's Plaza
    assert m["westvill"] == (None, None)


def test_lines_become_accounts_and_netted_bills():
    rows = [
        row("Electricity 08/25/2026-09/24/2026 725 GARDENST ENTM", 19585.61, inv="26271-11000-4-HBB26093012395", prop="twinpark"),
        # a voided check and its reissue: + - + nets to one bill
        row("Electricity 07/25/2026-08/24/2026 725 GARDENST ENTM", 500.0, inv="26271-11000-4-HBB26083012395", prop="twinpark"),
        row("Electricity 07/25/2026-08/24/2026 725 GARDENST ENTM", -500.0, inv="26271-11000-4-HBB26083012395", prop="twinpark"),
        row("Electricity 07/25/2026-08/24/2026 725 GARDENST ENTM", 500.0, inv="26271-11000-4-HBB26083012395", prop="twinpark"),
        # one supply contract over two buildings -> two accounts
        row("Electric Supply 08/26/2026-09/24/2026 725 Garden St Entm", 17458.56, inv="1520602-AJB26100720632",
            payee="Direct Energy Business", prop="twinpark"),
        row("Electric Supply 08/26/2026-09/24/2026 2260 Crotona Av Entm", 10534.35, inv="1520602-AJB26100720632",
            payee="Direct Energy Business", prop="twinpark"),
        # the same account on gas is another account
        row("Gas 08/25/2026-09/24/2026 725 GARDENST ENTM", 900.0, inv="26271-11000-4-HBB26093012395", prop="twinpark", gl="512-020"),
        # apartments roll up; a deposit is not a bill
        row("Vacant Electric 08/26/2026-09/25/2026 [2B] 725 Garden St # 2B", 40.0, inv="11111-11000-1-PSA26100205673", prop="twinpark"),
        row("Vacant Electric 09/26/2026-10/25/2026 [3C] 725 Garden St # 3C", 60.0, inv="22222-11000-1-PSA26100205674", prop="twinpark"),
        row("Electricity Deposit 09/25/2026-09/25/2026 725 GARDENST ENTM", 28186.0, inv="26271-11000-4-deposit-ZHX26102012345", prop="twinpark"),
    ]
    lines = [yb.parse_line(r) for r in rows]
    props = _props()
    res = yb.build(lines, yb.match_properties(lines, props), {p["id"]: p["buildings"] for p in props})
    acc = {(a["utility"], a["commodity"], a["service_address"]): a for a in res["accounts"]}
    con = acc[("Con Edison", "electric", "725 GARDENST ENTM")]
    assert con["building_id"] == 11 and con["account_number"] == "26271-11000-4" and con["property_id"] == 1
    assert sorted(round(b["cost"], 2) for b in con["bills"].values()) == [500.0, 19585.61]
    assert acc[("Direct Energy Business", "electric", "2260 Crotona Av Entm")]["building_id"] == 12
    assert acc[("Direct Energy Business", "electric", "725 Garden St Entm")]["supplier"] is True
    assert ("Con Edison", "gas", "725 GARDENST ENTM") in acc
    vac = acc[("Con Edison", "electric", "Vacant apartments")]
    assert vac["kind"] == "vacant" and vac["account_number"] is None and len(vac["bills"]) == 2   # by month
    assert res["deposits"] == {"twinpark": 28186.0}
    assert len(res["accounts"]) == 5


def test_a_period_paid_twice_is_flagged_and_a_rebill_is_not():
    acct = {"kind": "building"}
    twice = {"inv": {"A": 13204.10, "B": 13204.10}, "cost": 26408.20}
    rebill = {"inv": {"A": 66075.72, "A-7/24": 66075.72, "A-void": -66075.72}, "cost": 66075.72}
    small = {"inv": {"A": 20.0, "B": 20.0}, "cost": 40.0}
    assert yb.flags(twice, acct) == "twice"
    assert yb.flags(rebill, acct) is None
    assert yb.flags(small, acct) is None
    assert yb.flags(twice, {"kind": "vacant"}) is None


def test_lines_survive_the_trip_to_the_server(tmp_path):
    ln = yb.parse_line(row("Electricity 08/31/2026-09/30/2026 20 RICHMAN PLZ", 1.5))
    p = tmp_path / "l.json.gz"
    yb.dump_lines([ln], p)
    assert yb.load_lines(p) == [ln]


def test_a_typo_in_the_service_period_falls_back_to_the_posting_month():
    ln = yb.parse_line(row("Electricity 08/31/2065-09/30/2065 20 RICHMAN PLZ", 10.0, period=dt.datetime(2026, 10, 1)))
    assert ln["start"] is None and ln["end"] is None and ln["address"] == "20 RICHMAN PLZ"
    assert yb.parse_line(row("Water 07/01/2024-06/30/2025 BBL-2-02765-0226", 1.0, period=dt.datetime(2025, 7, 1)))["end"] \
        == dt.date(2025, 6, 30)                                                   # NYC Water's yearly charge is real
    assert yb.parse_line(row("Gas 01/01/2015-02/01/2015 1 A St", 1.0, period=dt.datetime(2026, 1, 1)))["start"] is None


def test_the_other_ways_notes_are_written():
    w = yb.parse_line(row("1000785042001-12/22", 73.61, inv="1000785042001-12/22", payee="NYC Water Board", gl="512-040",
                          period=dt.datetime(2023, 1, 1)))
    assert w["account"] == "1000785042001" and w["kind"] == "building" and w["start"] is None
    assert yb.norm_account("1001-0394-9000-1") == yb.norm_account("1001039490001")
    b = yb.parse_line(row("Electric-10/2/24-10/31/24 acct#3304522518", 82.66, inv="3304522518", payee="BGE",
                          period=dt.datetime(2024, 11, 1)))
    assert b["start"] == dt.date(2024, 10, 2) and b["end"] == dt.date(2024, 10, 31)
    assert b["account"] == "3304522518" and b["address"] == "" and b["kind"] == "building"
    e = yb.parse_line(row("Management Office. Electricity Service from 01.09.23 - 02.06.23", 855.77,
                          inv="5170-279-9008-Clay", period=dt.datetime(2023, 2, 1)))
    assert e["start"] == dt.date(2023, 1, 9) and e["end"] == dt.date(2023, 2, 6)
    g = yb.parse_line(row("Gas Acct 21997724 001 000 7 08/26", 94.52, inv="21997724 001 000 7 8/26", gl="512-020",
                          period=dt.datetime(2026, 8, 1)))
    assert g["account"] == "21997724 001 000 7"
    assert yb.parse_line(row("Conservice refund-Late Fee Refund", -2622.34, inv="HBB22111705021"))["kind"] == "fee"
    assert yb.parse_line(row("Gas 06/01/2024-06/30/2024 23 New Lots Ave Unit UM", 1708.16,
                             period=dt.datetime(2024, 7, 1)))["kind"] == "building"        # a meter, not a flat
    assert yb.parse_line(row("Something 3", 5.0, inv="HBB22111705021"))["kind"] == "other"


# ── usage from the Conservice bill records ───────────────────────────────────
def _usage():
    return yb.usage_index([
        # Con Ed electric with the supply line repeating the kWh: counted once
        ["26271-11000-4", "HBB25033012395\r", [["electric", "725 GARDENST ENTM", "CA", 15000.0, 100000, "kWh"],
                                               ["elecsupp", "725 GARDENST ENTM", "CA", 5000.0, 100000, "kWh"]]],
        # one gas account over two buildings: each its own therms
        ["52487-85000-1", "AJX26010310531", [["gas", "775 SOUTHERN BLVD", "CA", 9000.0, 6000, "Therms"],
                                             ["gas", "755 SOUTHERN BLVD", "CA", 4204.1, 3000, "Therms"],
                                             ["gassupp", "775 SOUTHERN BLVD", "CA", 1.0, 6000, "Therms"]]],
        # the same bill paid on a second invoice: the same therms again
        ["52487-85000-1", "PSA26010622668", [["gas", "775 SOUTHERN BLVD", "CA", 9000.0, 6000, "Therms"],
                                             ["gas", "755 SOUTHERN BLVD", "CA", 4204.1, 3000, "Therms"]]],
        # a utility's data error: 36M kWh on a $5,497 bill
        ["2100-1858-54-3", "AJA25081200306", [["electric", "135 MAIN ST", "CA", 5497.0, 36000000, "kWh"]]],
        # an ESCO's own invoice repeats the kWh
        ["644660-80133", "AJB24040610409", [["electric", "20 Richman Plaza", "CA", 900.0, 7000, "kWh"]]],
        ["9", "HBB26033012499", [["water", "1 Far Away Rd", "CA", 700.0, 120000, "Gal"],
                                 ["sewer", "1 Far Away Rd", "CA", 77.0, 120000, "Gal"]]],
    ])


def test_usage_counts_the_utilitys_delivery_items_once():
    idx = _usage()
    assert yb.batch_of("26271-11000-4-HBB25033012395") == "HBB25033012395"
    items = idx[("26271110004", "HBB25033012395")]
    assert yb.invoice_usage(items, "electric", "725 GARDENST ENTM") == (100000.0, "kWh", 15000.0)
    gas = idx[("52487850001", "AJX26010310531")]
    assert yb.invoice_usage(gas, "gas", "775 SOUTHERN BLVD")[:2] == (6000.0, "therm")
    assert yb.invoice_usage(gas, "gas", "755 Southern Blvd.")[:2] == (3000.0, "therm")
    assert yb.invoice_usage(gas, "gas", "1 Elsewhere St") is None           # several buildings, none of them this
    w = idx[("9", "HBB26033012499")]
    assert yb.invoice_usage(w, "water", "1 Far Away Rd")[:2] == (120.0, "kgal")
    assert yb.invoice_usage(w, "sewer", "1 Far Away Rd") is None            # sewer repeats water's gallons


def test_usage_lands_on_the_bills_once_and_data_errors_are_dropped():
    lines = [yb.parse_line(r) for r in [
        row("Electricity 01/25/2025-02/24/2025 725 GARDENST ENTM", 20000.0, inv="26271-11000-4-HBB25033012395"),
        row("Gas 12/02/2025-12/31/2025 775 SOUTHERN BLVD", 13204.10, inv="52487-85000-1-AJX26010310531", gl="512-020"),
        row("Gas 12/02/2025-12/31/2025 775 SOUTHERN BLVD", 13204.10, inv="52487-85000-1-PSA26010622668", gl="512-020"),
        row("Electricity 07/02/2025-07/31/2025 135 MAIN ST", 5497.0, inv="2100-1858-54-3-AJA25081200306",
            payee="Central Hudson"),
        row("Electricity 03/02/2024-04/01/2024 20 Richman Plaza", 900.0, inv="644660-80133-AJB24040610409",
            payee="Direct Energy Business"),
    ]]
    res = yb.build(lines, {}, {})
    out = yb.attach_usage(res["accounts"], _usage())
    by = {a["account_number"]: list(a["bills"].values())[0] for a in res["accounts"]}
    assert by["26271-11000-4"]["usage"] == 100000.0 and by["26271-11000-4"]["unit"] == "kWh"
    assert by["52487-85000-1"]["usage"] == 6000.0                           # paid twice, used once
    assert "usage" not in by["2100-1858-54-3"]                              # $0.00015 per kWh: not real
    assert "usage" not in by["644660-80133"]                                # the ESCO's copy of the kWh
    assert out["bills"] == 3 and out["with_usage"] == 2 and len(out["dropped"]) == 1
