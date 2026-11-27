"""
app/utilities.py — the Utilities page: what the buildings use and what it costs

Four questions, one page (the user, 2026-11-27): is a building efficient, is
it exposed under LL97, are the bills right, and did a change save money. The
data arrives in layers, and the page says plainly which layers exist yet:

  1. NYC benchmarking (LL84) — annual energy, water and emissions for every
     NYC property over 25,000 sq ft (app/benchmark.py). Live now.
  2. The portfolio's own utility accounts and bills (UtilityAccount /
     UtilityBill) — from the bills on file, a Yardi payables export, and
     later the ConEd / National Grid / DEP feeds.
  3. Joined with the controls (burner minutes, apartments, fans) — once
     bills exist per building.

  view()            everything the page shows, as plain dicts
  property_row(p)   one benchmarked property: latest year, LL97, trend
"""
import datetime as _dt

from app import benchmark as bm

NYC_BOROUGHS = ("MANHATTAN", "BRONX", "BROOKLYN", "QUEENS", "STATEN ISLAND")


def _latest_with_data(years):
    """The newest year that reports energy, else the newest year, else None."""
    ys = sorted(years, key=lambda y: y.year, reverse=True)
    for y in ys:
        if y.site_eui or bm.ll97_emissions(y):
            return y
    return ys[0] if ys else None


def property_row(p, years, stats, buildings) -> dict:
    """One benchmarked property for the page. `stats` = {year: BenchmarkStat}
    for Multifamily Housing; `buildings` = [(building name, property name)]."""
    y = _latest_with_data(years)
    st = stats.get(y.year) if y else None
    e = bm.ll97_emissions(y) if y else None
    lim24 = bm.ghg_limit(p.property_type, p.gfa, 2026)
    lim30 = bm.ghg_limit(p.property_type, p.gfa, 2030)
    over24 = round(e - lim24, 1) if e is not None and lim24 else None
    over30 = round(e - lim30, 1) if e is not None and lim30 else None
    trend = [{"year": x.year, "site_eui": x.site_eui, "ll97": bm.ll97_emissions(x)}
             for x in sorted(years, key=lambda v: v.year)]
    fuel = None
    if y:
        f = {"gas": y.gas_kbtu or 0, "oil": (y.oil2_kbtu or 0) + (y.oil4_kbtu or 0) + (y.oil56_kbtu or 0),
             "steam": y.steam_kbtu or 0}
        fuel = max(f, key=f.get) if any(f.values()) else None
    return {
        "id": p.id, "name": p.name, "address": p.address, "borough": (p.borough or "").title(),
        "bbl": p.bbl, "type": p.property_type, "gfa": p.gfa, "units": p.units, "n_buildings": p.n_buildings,
        "pathway": p.ll97_pathway, "pathway_words": bm.PATHWAYS.get(p.ll97_pathway or ""),
        "buildings": buildings, "year": y.year if y else None,
        "site_eui": y.site_eui if y else None, "wn_site_eui": y.wn_site_eui if y else None,
        "energy_star": y.energy_star if y else None,
        "median_eui": st.wn_site_eui if st else None,
        "eui_x": (y.wn_site_eui / st.wn_site_eui) if y and y.wn_site_eui and st and st.wn_site_eui else None,
        "water_kgal": y.water_kgal if y else None,
        "water_per_unit": (y.water_kgal / p.units) if y and y.water_kgal and p.units else None,
        "median_water": st.water_per_unit if st else None,
        "elec_kwh": y.elec_kwh if y else None, "fuel": fuel,
        "gas_kbtu": y.gas_kbtu if y else None,
        "oil_kbtu": ((y.oil2_kbtu or 0) + (y.oil4_kbtu or 0) + (y.oil56_kbtu or 0)) if y else None,
        "estimated": bool(y and (y.estimated_energy or y.estimated_water)),
        "ll97": e, "limit24": lim24, "over24": over24,
        "penalty24": round(over24 * bm.LL97_PENALTY_PER_T) if over24 and over24 > 0 else 0,
        "limit30": lim30, "over30": over30,
        "penalty30": round(over30 * bm.LL97_PENALTY_PER_T) if over30 and over30 > 0 else 0,
        "trend": trend,
    }


def view() -> dict:
    """Everything /utilities shows. App context assumed."""
    from sqlalchemy.orm import joinedload
    from app.models import (BenchmarkProperty, BenchmarkStat, BenchmarkYear, Building, BuildingBenchmark,
                            Property, UtilityAccount, UtilityBill)
    blds = (Building.query.options(joinedload(Building.property).joinedload(Property.client))
            .join(Property).filter(Building.is_active.is_(True), Property.is_active.is_(True)).all())
    links = {x.building_id: x for x in BuildingBenchmark.query.all()}
    stats = {s.year: s for s in BenchmarkStat.query.filter_by(property_type="Multifamily Housing")}
    years = {}
    for y in BenchmarkYear.query.all():
        years.setdefault(y.property_id, []).append(y)
    by_prop = {}
    for b in blds:
        x = links.get(b.id)
        if x and x.property_id:
            by_prop.setdefault(x.property_id, []).append(b)
    props = []
    for p in BenchmarkProperty.query.filter(BenchmarkProperty.id.in_(list(by_prop) or ["-"])).all():
        props.append(property_row(p, years.get(p.id, []), stats,
                                  sorted({(b.name, b.property.name) for b in by_prop[p.id]})))
    exposure = sorted((r for r in props if r["ll97"] is not None and r["limit24"]),
                      key=lambda r: -(r["over30"] or -1e9))
    efficiency = sorted((r for r in props if r["eui_x"]), key=lambda r: -r["eui_x"])
    # buildings with no benchmarking report, by property
    missing = {}
    for b in blds:
        x = links.get(b.id)
        if x and (x.property_id or x.how == "none"):
            continue
        missing.setdefault(b.property.name, []).append({"id": b.id, "name": b.name, "address": b.address})
    latest = max(stats) if stats else None
    accounts = UtilityAccount.query.filter_by(is_active=True).count()
    bills = UtilityBill.query.count()
    refreshed = max((p.refreshed_at for p in BenchmarkProperty.query.all() if p.refreshed_at), default=None)
    return {
        "props": sorted(props, key=lambda r: (r["buildings"][0][1] if r["buildings"] else "", r["address"] or "")),
        "exposure": exposure, "efficiency": efficiency, "missing": dict(sorted(missing.items())),
        "n_buildings": len(blds), "n_linked": sum(len(v) for v in by_prop.values()),
        "n_props": len(props), "median": stats.get(latest), "median_year": latest,
        "over24": [r for r in exposure if (r["over24"] or 0) > 0],
        "over30": [r for r in exposure if (r["over30"] or 0) > 0],
        "penalty24": sum(r["penalty24"] for r in exposure), "penalty30": sum(r["penalty30"] for r in exposure),
        "pathways_known": sum(1 for r in props if r["pathway"]),
        "accounts": accounts, "bills": bills, "refreshed": refreshed,
        "linked_ids": {bid: x.property_id for bid, x in links.items()},
    }


# ── refreshing from NYC Open Data, in the background ─────────────────────────
import threading

_state = {"running": False, "last": None, "error": None, "at": None}
_lock = threading.Lock()


def refresh_state() -> dict:
    with _lock:
        return dict(_state)


def start_refresh(app) -> bool:
    """Download LL84 and refresh, in a thread (a minute or two). False when one
    is already running in this worker."""
    with _lock:
        if _state["running"]:
            return False
        _state.update(running=True, error=None)

    def run():
        try:
            with app.app_context():
                res = bm.refresh(bm.fetch_rows())
            with _lock:
                _state.update(last=res, at=_dt.datetime.utcnow())
        except Exception as e:                          # shown on the page; nothing half-written (one commit)
            import logging
            logging.getLogger(__name__).exception("LL84 refresh failed")
            with _lock:
                _state.update(error=f"{type(e).__name__}: {str(e)[:200]}")
        finally:
            with _lock:
                _state["running"] = False
    threading.Thread(target=run, name="ll84-refresh", daemon=True).start()
    return True
