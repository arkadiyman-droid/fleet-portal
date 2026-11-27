"""
app/benchmark.py — NYC Local Law 84 benchmarking data for the portfolio's buildings

Every NYC building over 25,000 sq ft reports its year's energy, water and
emissions (ENERGY STAR Portfolio Manager), and the city publishes it on NYC
Open Data: "NYC Building Energy and Water Data Disclosure for Local Law 84"
(dataset 5zyy-y8am, calendar 2022 on). It is the one portfolio-wide source of
annual utility use that needs no account access, so the Utilities work starts
here (the user, 2026-11-27).

The whole dataset is downloaded (only the columns used) and matched HERE, so
the list of our buildings never goes to an outside service.

  parse_address(text)    -> (numbers, street) of a building name / address
  match(buildings, rows) -> {building_id: [LL84 property ids]}, by house
                            number + street, a property's buildings settling
                            on one borough together
  ghg_limit(...)         -> the LL97 emissions limit for a property

Matching notes: the portal's building names are street addresses written by
hand ("590-600 Gates Ave", "1756 Park pl", "Ind-1 85 Taylor N", "735 Gates")
with no borough or zip, and the same address can exist in several boroughs.
A building name with a number range matches any LL84 address in that range on
that street; LL84 addresses can themselves be ranges. A property's buildings
are usually in one borough, so where a building matches in several boroughs
the one most of its property's buildings matched in wins. LL84 reports a
PROPERTY, which may hold several buildings (a campus, a row of walk-ups): two
portal buildings can share one LL84 property and that is reported as such.
"""
import re

DATASET = "5zyy-y8am"
COLUMNS = ("property_id,property_name,address_1,city,postal_code,borough,nyc_borough_block_and_lot,"
           "nyc_building_identification,report_year,year_ending,primary_property_type,property_gfa_self_reported,"
           "property_gfa_calculated,multifamily_housing_total,number_of_buildings,site_eui_kbtu_ft,"
           "weather_normalized_site_eui,source_eui_kbtu_ft,energy_star_score,natural_gas_use_kbtu,fuel_oil_2_use_kbtu,"
           "fuel_oil_4_use_kbtu,fuel_oil_5_6_use_kbtu,district_steam_use_kbtu,electricity_use_grid_purchase,"
           "electricity_use_grid_purchase_1,water_use_all_water_sources,total_location_based_ghg,"
           "total_location_based_ghg_1,net_emissions_metric_tons,estimated_values_energy,estimated_values_water")

_SUFFIX = {"avenue": "ave", "av": "ave", "street": "st", "place": "pl", "boulevard": "blvd", "road": "rd",
           "drive": "dr", "lane": "ln", "court": "ct", "terrace": "ter", "parkway": "pkwy", "circle": "cir",
           "circ": "cir", "square": "sq", "highway": "hwy", "concourse": "concourse", "expressway": "expy"}
_DIRS = {"north": "n", "south": "s", "east": "e", "west": "w"}
_ORD = re.compile(r"^(\d+)(st|nd|rd|th)$")
# building-name decorations that are not part of the address
_PREFIX = re.compile(r"^(?:ind|wil|rp)-?\d+\s+", re.I)
_TRAIL = re.compile(r"\s+(?:north|south|n|s|z\d|nw|sw|ne|se)(?:\s+zone)?$|\s+zone$", re.I)


def _street(words):
    out = []
    for i, w in enumerate(words):
        w = w.strip(".,").lower()
        if not w:
            continue
        m = _ORD.match(w)
        if m:
            w = m.group(1)                                   # 1st -> 1
        elif w in _SUFFIX and i == len(words) - 1:
            w = _SUFFIX[w]
        elif w in _DIRS and i == 0 and len(words) > 1:
            w = _DIRS[w]                                     # "East 104th St" -> "e 104 st"
        out.append(w)
    return " ".join(out)


def parse_address(text):
    """(numbers, street) or None. numbers is (low, high) for a range, or a
    single Queens-style "125-60" as the tuple ("125-60",). street is
    normalised: lower case, suffixes shortened, ordinals bare."""
    s = (text or "").split(",")[0].strip()
    s = _PREFIX.sub("", s)
    s = _TRAIL.sub("", s).strip()
    s = s.replace("MLK", "Martin Luther King")
    m = re.match(r"^(\d+[a-z]?)(?:\s*-\s*(\d+))?(?:\s*-\s*(\d+))?\s+(.+)$", s, re.I)
    if not m:
        return None
    a, b, c, street = m.group(1), m.group(2), m.group(3), m.group(4)
    words = street.split()
    st = _street(words)
    if not st:
        return None
    na = int(re.sub(r"\D", "", a))
    if b and not c and len(b) == 2 and na >= 100 and int(b) < na:
        return (f"{na}-{b}",), st                            # Queens: 125-60 Sutphin Blvd
    hi = int(c or b) if (b or c) else na
    return (na, max(na, hi)), st


def _street_match(ours, theirs):
    """Same street. Our names sometimes leave the type off ("735 Gates",
    "1804 Weeks"): then the words we have must start theirs."""
    if ours == theirs:
        return True
    a, b = ours.split(), theirs.split()
    return len(a) < len(b) and b[:len(a)] == a and b[len(a)] in set(_SUFFIX.values())


def _covers(ours, theirs):
    """How our numbers and LL84's meet: 2 = the same number, 1 = the ranges
    overlap on the same side of the street, 0 = they do not. Both may be
    ranges; a Queens number matches only itself. A range of one parity
    ("55-61") is one side of the street; it never meets the other side
    ("54-56")."""
    if isinstance(ours[0], str) or isinstance(theirs[0], str):
        return 2 if ours == theirs else 0
    if not (ours[0] <= theirs[1] and theirs[0] <= ours[1]):
        return 0
    if ours[0] % 2 == ours[1] % 2 and theirs[0] % 2 == theirs[1] % 2 and ours[0] % 2 != theirs[0] % 2:
        return 0
    return 2 if ours[0] == theirs[0] else 1


RESIDENTIAL = ("Multifamily", "Senior", "Residen", "Mixed Use")      # property types we house people in


def _residential(ptype):
    return any(k in (ptype or "") for k in RESIDENTIAL)


def index_rows(rows):
    """{street: [(numbers, property_id, borough)]} over LL84 rows (any year)."""
    idx = {}
    seen = set()
    for r in rows:
        if not _residential(r.get("primary_property_type")):
            continue
        p = parse_address(r.get("address_1"))
        if not p:
            continue
        key = (r.get("property_id"), p)
        if key in seen:
            continue
        seen.add(key)
        idx.setdefault(p[1], []).append((p[0], r.get("property_id"), (r.get("borough") or "").upper()))
    return idx


def match(buildings, rows):
    """buildings = [{id, name, address, prop_id}] -> {building_id: {"id": the
    LL84 property id chosen, "alt": [the others that also matched], "borough",
    "exact": True when the house number itself matched}}.

    A property's buildings settle on one borough together (the one most of
    them matched in), and a property where fewer than two buildings, or under
    half of them, match is dropped: a lone hit on a common street name is more
    likely another city's address than ours. Of several LL84 properties at one
    building, the most specific wins: an exact house number, then the one
    covering the fewest buildings, then the largest id (the latest
    registration)."""
    idx = index_rows(rows)
    nb = {}
    for r in rows:
        try:
            nb[r.get("property_id")] = int(float(r.get("number_of_buildings") or 0))
        except ValueError:
            nb[r.get("property_id")] = 0
    cand = {}
    parseable = {}
    for b in buildings:
        p = parse_address(b.get("address") or b.get("name"))
        if not p:
            continue
        parseable[b["prop_id"]] = parseable.get(b["prop_id"], 0) + 1
        nums, street = p
        found = []
        for st, entries in idx.items():
            if _street_match(street, st):
                for n, pid, boro in entries:
                    how = _covers(nums, n)
                    if how:
                        found.append((pid, boro, how))
        if found:
            cand[b["id"]] = found
    votes = {}
    for b in buildings:
        for boro in {x[1] for x in cand.get(b["id"], [])}:
            votes.setdefault(b["prop_id"], {}).setdefault(boro, 0)
            votes[b["prop_id"]][boro] += 1
    out = {}
    for b in buildings:
        found = cand.get(b["id"])
        if not found:
            continue
        v = votes.get(b["prop_id"]) or {}
        boro = max(v, key=lambda k: (v[k], k))
        agree, of = v[boro], parseable.get(b["prop_id"], 1)
        if of > 1 and (agree < 2 or agree * 2 < of):
            continue                                      # too few of the property's buildings agree
        mine = [x for x in found if x[1] == boro]
        if not mine:
            continue
        best = max(mine, key=lambda x: (x[2], -nb.get(x[0], 0) if nb.get(x[0]) else -99, int(x[0]) if str(x[0]).isdigit() else 0))
        out[b["id"]] = {"id": best[0], "alt": sorted({x[0] for x in mine} - {best[0]}), "borough": boro,
                        "exact": best[2] == 2}
    return out


# ── LL97 ────────────────────────────────────────────────────────────────────
# Emissions limits, tCO2e per square foot per year, for the property types that
# matter here (NYC Administrative Code 28-320.3.1 / .3.2; the 2030 figures per
# the 2024 rules). Multifamily is occupancy group R-2.
LL97_LIMITS = {"Multifamily Housing": {2024: 0.00675, 2030: 0.00335}}
LL97_PENALTY_PER_T = 268                      # dollars per tonne over the limit


def ghg_limit(property_type, gfa, year):
    """tCO2e a year this property may emit, or None for a type not covered here."""
    lim = LL97_LIMITS.get(property_type)
    if not lim or not gfa:
        return None
    period = 2030 if year >= 2030 else 2024
    return lim[period] * gfa


# LL97 greenhouse-gas coefficients for 2024-2029 (NYC Administrative Code
# 28-320.3.1.1; 1 RCNY 103-14), tCO2e per unit of energy as LL84 reports it.
LL97_COEF = {"elec_kwh": 0.000288962, "gas_kbtu": 0.00005311, "oil2_kbtu": 0.00007421,
             "oil4_kbtu": 0.00007529, "oil56_kbtu": 0.00007529, "steam_kbtu": 0.00004493}
# 2030-2034 limit for Multifamily Housing: the 2023 rule's table by property
# type gives 0.00334664; the original law's R-2 figure was 0.00407. Sources
# disagree (checked 2026-11-27), so the page labels 2030 an estimate.
LL97_LIMITS["Multifamily Housing"][2030] = 0.00334664


def ll97_emissions(y):
    """tCO2e a year by LL97's own coefficients from one BenchmarkYear (or a
    dict with the same keys); None when no energy is reported at all."""
    get = y.get if isinstance(y, dict) else (lambda k: getattr(y, k, None))
    parts = [get(k) * c for k, c in LL97_COEF.items() if get(k)]
    return round(sum(parts), 1) if parts else None


def _f(v):
    try:
        x = float(str(v).replace(",", ""))
        return x if x == x else None
    except (TypeError, ValueError):
        return None


def _units(v):
    x = _f(v)
    return int(x) if x is not None and x > 0 and abs(x - round(x)) < 1e-6 else None


def year_values(r):
    """The BenchmarkYear columns from one LL84 row."""
    def est(v):
        return None if not v or v == "Not Available" else str(v).lower().startswith("yes")
    es = _f(r.get("energy_star_score"))
    return {"site_eui": _f(r.get("site_eui_kbtu_ft")), "wn_site_eui": _f(r.get("weather_normalized_site_eui")),
            "source_eui": _f(r.get("source_eui_kbtu_ft")), "energy_star": int(es) if es is not None else None,
            "gas_kbtu": _f(r.get("natural_gas_use_kbtu")), "oil2_kbtu": _f(r.get("fuel_oil_2_use_kbtu")),
            "oil4_kbtu": _f(r.get("fuel_oil_4_use_kbtu")), "oil56_kbtu": _f(r.get("fuel_oil_5_6_use_kbtu")),
            "steam_kbtu": _f(r.get("district_steam_use_kbtu")), "elec_kwh": _f(r.get("electricity_use_grid_purchase_1")),
            "water_kgal": _f(r.get("water_use_all_water_sources")), "ghg_t": _f(r.get("total_location_based_ghg")),
            "estimated_energy": est(r.get("estimated_values_energy")),
            "estimated_water": est(r.get("estimated_values_water"))}


def medians(rows):
    """NYC-wide medians per year for Multifamily Housing: [{property_type,
    year, n, site_eui, wn_site_eui, ghg_per_sf, water_per_unit}]."""
    import statistics
    by = {}
    for r in rows:
        if r.get("primary_property_type") != "Multifamily Housing":
            continue
        try:
            yr = int(r.get("report_year"))
        except (TypeError, ValueError):
            continue
        by.setdefault(yr, []).append(r)

    def med(v):
        return round(statistics.median(v), 6) if v else None
    out = []
    for yr, rs in sorted(by.items()):
        eui = [x for x in (_f(r.get("site_eui_kbtu_ft")) for r in rs) if x and 5 < x < 500]
        wn = [x for x in (_f(r.get("weather_normalized_site_eui")) for r in rs) if x and 5 < x < 500]
        ghg, wat = [], []
        for r in rs:
            gfa = _f(r.get("property_gfa_self_reported"))
            e = ll97_emissions(year_values(r))
            if gfa and gfa > 1000 and e:
                ghg.append(e / gfa)
            u, w = _units(r.get("multifamily_housing_total")), _f(r.get("water_use_all_water_sources"))
            if u and w and 0 < w / u < 500:
                wat.append(w / u)
        out.append({"property_type": "Multifamily Housing", "year": yr, "n": len(eui), "site_eui": med(eui),
                    "wn_site_eui": med(wn), "ghg_per_sf": med(ghg), "water_per_unit": med(wat)})
    return out


def fetch_rows(get=None, timeout=600):
    """Every row of the LL84 dataset, only COLUMNS, as dicts (one request to
    NYC Open Data; nothing about our buildings is sent)."""
    import csv
    import io
    import requests
    get = get or requests.get
    r = get("https://data.cityofnewyork.us/resource/" + DATASET + ".csv",
            params={"$select": COLUMNS, "$limit": 400000}, timeout=timeout)
    r.raise_for_status()
    return list(csv.DictReader(io.StringIO(r.content.decode("utf-8"))))


def refresh(rows, now=None):
    """Store what our buildings need from the LL84 rows: the matched properties
    with every year, the NYC medians, and the building links. Links a person
    set (how = manual / none) are never changed. App context assumed; commits."""
    import datetime as _dt
    from app.extensions import db
    from app.models import (BenchmarkProperty, BenchmarkStat, BenchmarkYear, Building, BuildingBenchmark,
                            Property)
    now = now or _dt.datetime.utcnow()
    blds = [{"id": b.id, "name": b.name, "address": b.address, "prop_id": b.property_id}
            for b in Building.query.join(Property).filter(Building.is_active.is_(True), Property.is_active.is_(True))]
    found = match(blds, rows)
    links = {x.building_id: x for x in BuildingBenchmark.query.all()}
    keep = {bid for bid, x in links.items() if x.how in ("manual", "none")}
    wanted = {v["id"] for bid, v in found.items() if bid not in keep}
    wanted |= {x.property_id for bid, x in links.items() if bid in keep and x.property_id}
    latest, years = {}, {}
    for r in rows:
        pid = r.get("property_id")
        if pid not in wanted:
            continue
        try:
            yr = int(r.get("report_year"))
        except (TypeError, ValueError):
            continue
        years.setdefault(pid, {})[yr] = r
        if pid not in latest or yr > int(latest[pid]["report_year"]):
            latest[pid] = r
    for pid, r in latest.items():
        p = db.session.get(BenchmarkProperty, pid) or BenchmarkProperty(id=pid)
        p.name, p.address = (r.get("property_name") or "")[:300], (r.get("address_1") or "")[:300]
        p.borough, p.postal_code = (r.get("borough") or "")[:20], (r.get("postal_code") or "")[:10]
        p.bbl = (r.get("nyc_borough_block_and_lot") or "")[:20]
        p.bin = (r.get("nyc_building_identification") or "")[:200]
        p.property_type = (r.get("primary_property_type") or "")[:120]
        p.gfa = _f(r.get("property_gfa_self_reported")) or _f(r.get("property_gfa_calculated"))
        p.units = _units(r.get("multifamily_housing_total"))
        nb = _f(r.get("number_of_buildings"))
        p.n_buildings = int(nb) if nb else None
        p.refreshed_at = now
        db.session.add(p)
    db.session.flush()
    for pid, ys in years.items():
        for yr, r in ys.items():
            y = db.session.get(BenchmarkYear, (pid, yr)) or BenchmarkYear(property_id=pid, year=yr)
            for k, v in year_values(r).items():
                setattr(y, k, v)
            db.session.add(y)
    for bid, v in found.items():
        if bid in keep:
            continue
        x = links.get(bid) or BuildingBenchmark(building_id=bid)
        x.property_id, x.how = v["id"], ("exact" if v["exact"] else "range")
        x.alternates, x.set_by, x.set_at = v["alt"], "automatic", now
        db.session.add(x)
    gone = 0
    for bid, x in links.items():
        if bid not in keep and bid not in found:
            db.session.delete(x)                          # no longer matches: drop the automatic link
            gone += 1
    for s in medians(rows):
        st = db.session.get(BenchmarkStat, (s["property_type"], s["year"])) or BenchmarkStat(
            property_type=s["property_type"], year=s["year"])
        for k in ("n", "site_eui", "wn_site_eui", "ghg_per_sf", "water_per_unit"):
            setattr(st, k, s[k])
        db.session.add(st)
    db.session.commit()
    return {"rows": len(rows), "properties": len(latest), "linked": len(found), "kept_manual": len(keep),
            "dropped": gone}


def load_cbl(rows):
    """The city's LL97 Covered Buildings List rows [{bbl, pathway}] onto our
    properties (by BBL). Returns how many properties got a pathway."""
    from app.extensions import db
    from app.models import BenchmarkProperty
    by = {}
    for r in rows:
        b = re.sub(r"\D", "", str(r.get("bbl") or ""))
        if b and r.get("pathway"):
            by[b] = str(r["pathway"]).strip()[:10]
    n = 0
    for p in BenchmarkProperty.query.all():
        cp = by.get(re.sub(r"\D", "", p.bbl or ""))
        if cp:
            p.ll97_pathway = cp
            n += 1
    db.session.commit()
    return n


PATHWAYS = {"CP0": "Article 320, limits from 2024", "CP1": "Article 320, limits from 2026",
            "CP2": "Article 320, limits from 2035",
            "CP3": "Article 321 (rent-regulated / affordable): prescriptive measures, not the limits",
            "CP4": "City-owned / NYCHA"}
