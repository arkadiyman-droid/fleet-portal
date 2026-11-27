"""app/benchmark.py — matching the portfolio's buildings to NYC's LL84
benchmarking data, and the LL97 arithmetic. Pure."""
from app import benchmark as bm


def row(pid, addr, boro="BROOKLYN", ptype="Multifamily Housing", year="2024", **kw):
    r = {"property_id": pid, "address_1": addr, "borough": boro, "primary_property_type": ptype, "report_year": year,
         "number_of_buildings": "1"}
    r.update(kw)
    return r


def test_addresses_are_read_the_way_they_are_written():
    assert bm.parse_address("590-600 Gates Ave") == ((590, 600), "gates ave")
    assert bm.parse_address("1756 Park pl") == ((1756, 1756), "park pl")
    assert bm.parse_address("Ind-1 85 Taylor N") == ((85, 85), "taylor")
    assert bm.parse_address("Wil-3 255 Haveme S") == ((255, 255), "haveme")
    assert bm.parse_address("801 South St North Zone") == ((801, 801), "s st")          # read the same way as the city writes it
    assert bm.parse_address("125-60 Sutphin Blvd") == (("125-60",), "sutphin blvd")      # Queens
    assert bm.parse_address("114 E 104th St") == ((114, 114), "e 104 st")
    assert bm.parse_address("90-94-100 Spruce St.") == ((90, 100), "spruce st")
    assert bm.parse_address("627 Alan Page Dr, Canton, OH") == ((627, 627), "alan page dr")
    assert bm.parse_address("RP 10AH") is None and bm.parse_address("Bench") is None
    assert bm.parse_address("590 GATES AVENUE") == ((590, 590), "gates ave")


def test_a_property_matches_on_number_street_and_borough():
    rows = [row("1", "590 Gates Avenue"), row("2", "690 GATES AVE"), row("9", "590 Gates Ave", boro="QUEENS"),
            row("3", "660 Fox Street", boro="BRONX", ptype="K-12 School")]
    b = [{"id": 1, "name": "590-600 Gates Ave", "prop_id": 7}, {"id": 2, "name": "690-700 Gates Ave", "prop_id": 7}]
    m = bm.match(b, rows)
    assert m[1]["id"] == "1" and m[1]["borough"] == "BROOKLYN" and m[1]["exact"]   # the property's borough wins
    assert m[2]["id"] == "2"
    assert bm.match([{"id": 5, "name": "655-663 Fox St.", "prop_id": 8}], rows) == {}   # a school is not one of ours


def test_the_other_side_of_the_street_is_not_a_match():
    rows = [row("1", "54-56 Franklin St", boro="MANHATTAN")]
    assert bm.match([{"id": 1, "name": "55-61 Franklin St", "prop_id": 3}], rows) == {}
    rows = [row("1", "711-735 Garden Street", boro="BRONX")]
    assert bm.match([{"id": 1, "name": "725-735 Garden st Z1", "prop_id": 3}], rows)[1]["id"] == "1"


def test_a_lone_hit_in_a_big_property_is_not_trusted():
    rows = [row("1", "67 Franklin Street", boro="MANHATTAN")]
    b = [{"id": i, "name": n, "prop_id": 4} for i, n in enumerate(["63-69 Franklin St.", "101-109 Lowell st",
                                                                       "30-36 Bradford st", "60-68 Hampshire St"], 1)]
    assert bm.match(b, rows) == {}                                   # 1 of 4: another city's Franklin Street


def test_the_most_specific_report_wins():
    rows = [row("10", "85 TAYLOR STREET", number_of_buildings="6"), row("11", "85 Taylor Street", number_of_buildings="1")]
    m = bm.match([{"id": 1, "name": "Ind-1 85 Taylor N", "prop_id": 2}], rows)
    assert m[1]["id"] == "11" and m[1]["alt"] == ["10"]


def test_ll97_arithmetic():
    assert bm.ll97_emissions({"elec_kwh": 100000, "gas_kbtu": 1e6}) == 82.0
    assert bm.ll97_emissions({"oil2_kbtu": 1e6, "steam_kbtu": 1e6}) == 119.1
    assert bm.ll97_emissions({}) is None
    assert bm.ghg_limit("Multifamily Housing", 100000, 2026) == 675.0
    assert round(bm.ghg_limit("Multifamily Housing", 100000, 2031), 1) == 334.7
    assert bm.ghg_limit("Office", 100000, 2026) is None


def test_year_values_and_medians():
    r = row("1", "1 A St", site_eui_kbtu_ft="80.5", weather_normalized_site_eui="Not Available", energy_star_score="61",
            natural_gas_use_kbtu="1,000,000", electricity_use_grid_purchase_1="100000", water_use_all_water_sources="500",
            estimated_values_energy="Yes", property_gfa_self_reported="50000", multifamily_housing_total="50")
    v = bm.year_values(r)
    assert v["site_eui"] == 80.5 and v["wn_site_eui"] is None and v["energy_star"] == 61 and v["gas_kbtu"] == 1e6
    assert v["estimated_energy"] is True
    rows = [r, {**r, "site_eui_kbtu_ft": "100.5"}, {**r, "primary_property_type": "Office", "site_eui_kbtu_ft": "300"}]
    med = bm.medians(rows)
    assert med[0]["year"] == 2024 and med[0]["n"] == 2 and med[0]["site_eui"] == 90.5
    assert med[0]["water_per_unit"] == 10.0 and round(med[0]["ghg_per_sf"], 5) == round(82 / 50000, 5)
