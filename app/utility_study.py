"""
app/utility_study.py — one property's pool of utilities, for study
(/utilities/property/<id>, from the Utilities page)

Everything the Yardi bills (app/yardi_bills.py) hold for one property,
put on a calendar so it can be read as matrices:

  * bills are spread over the months they cover, by days (a Dec 2 - Jan 3
    bill is 30/33 December), so months and years add up whatever the
    billing cycle;
  * per fuel (electric, gas, water & sewer) and month: cost (all of it,
    supply and sewer and lines with no meter named; apartments' own
    electric is its own line), usage (the utility's delivery reads only,
    see yardi_bills), the share of the utility's own bills that carry
    usage (or are flat charges), and the price per unit where that share
    is at least COVER_MIN;
  * per meter (account) and month: cost, usage and a status (billed, flat,
    paid twice?, a gap in an account's bills);
  * weather: heating and cooling degree-days (65 F base) per month from
    Open-Meteo's archive (free, no key; only a city-level point is sent),
    cached under instance/weather/. A property's point is in
    instance/weather_places.json ({property id: [lat, lon, "label"]});
    New York City otherwise;
  * gas against heating degree-days and electric against cooling
    degree-days, month by month: a straight line through therms per day
    against degree-days per day splits use into a base load (hot water,
    cooking) and a part that follows the weather.

  study(property, today)   the whole picture as plain data (JSON-ready)
  spread(start, end)       [(month key, share of the bill's days)]
  fit(points)              least squares: base, slope, r2
"""
import datetime as _dt
import json
import math
import os
from collections import defaultdict

FUELS = ("electric", "gas", "water")
FUEL_OF = {"electric": "electric", "gas": "gas", "water": "water", "sewer": "water"}
UNIT = {"electric": "kWh", "gas": "therm", "water": "kgal"}
COVER_MIN = 0.9
DEFAULT_PLACE = (40.78, -73.97, "New York City")
WEATHER_URL = ("https://archive-api.open-meteo.com/v1/archive?latitude={lat:.2f}&longitude={lon:.2f}"
               "&start_date={start}&end_date={end}&daily=temperature_2m_mean"
               "&temperature_unit=fahrenheit&timezone=America%2FNew_York")


# ── calendar ─────────────────────────────────────────────────────────────────
def mkey(d):
    return d.year * 12 + d.month - 1


def mlabel(k):
    return f"{k // 12}-{k % 12 + 1:02d}"


def mdays(k):
    y, m = k // 12, k % 12 + 1
    nxt = _dt.date(y + (m == 12), m % 12 + 1, 1)
    return (nxt - _dt.date(y, m, 1)).days


def spread(start, end):
    """[(month key, share of the bill's days in that month)], days inclusive."""
    if end < start:
        start, end = end, start
    total = (end - start).days + 1
    out, d = [], start
    while d <= end:
        nxt = (d.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        seg = min(end, nxt - _dt.timedelta(days=1))
        out.append((mkey(d), ((seg - d).days + 1) / total))
        d = nxt
    return out


def covered_days(start, end):
    """{month key: days of that month the bill covers}."""
    if end < start:
        start, end = end, start
    out, d = {}, start
    while d <= end:
        nxt = (d.replace(day=1) + _dt.timedelta(days=32)).replace(day=1)
        seg = min(end, nxt - _dt.timedelta(days=1))
        out[mkey(d)] = (seg - d).days + 1
        d = nxt
    return out


# ── weather ──────────────────────────────────────────────────────────────────
def place_for(prop_id, instance_path):
    try:
        with open(os.path.join(instance_path, "weather_places.json"), encoding="utf-8") as f:
            p = json.load(f).get(str(prop_id))
        if p:
            return float(p[0]), float(p[1]), str(p[2])
    except (OSError, ValueError):
        pass
    return DEFAULT_PLACE


def fetch_daily(lat, lon, start, end):
    """{iso date: mean temperature F} from Open-Meteo's archive, or {}."""
    import urllib.request
    url = WEATHER_URL.format(lat=lat, lon=lon, start=start.isoformat(), end=end.isoformat())
    req = urllib.request.Request(url, headers={"User-Agent": "fleet-portal utility study"})
    with urllib.request.urlopen(req, timeout=15) as r:
        d = json.loads(r.read().decode())["daily"]
    return {t: v for t, v in zip(d["time"], d["temperature_2m_mean"]) if v is not None}


def daily_temps(lat, lon, start, end, instance_path, now=None):
    """Mean daily temperatures, cached per point; refetched when the cache is
    over three days old or does not reach back to `start`. {} on failure."""
    now = now or _dt.datetime.utcnow()
    path = os.path.join(instance_path, "weather", f"{lat:.2f}_{lon:.2f}.json")
    cache = {}
    try:
        with open(path, encoding="utf-8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        pass
    fresh = cache.get("at") and (now - _dt.datetime.fromisoformat(cache["at"])).days < 3
    if fresh and cache.get("start", "9999") <= start.isoformat():
        return cache.get("t", {})
    try:
        t = fetch_daily(lat, lon, start, end)
    except Exception:                                       # offline, rate-limited: use what is cached
        return cache.get("t", {})
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"at": now.isoformat(), "start": start.isoformat(), "t": t}, f)
    except OSError:
        pass
    return t


def degree_days(temps, months):
    """{month key: (HDD, CDD, days with a reading)} at 65 F."""
    acc = defaultdict(lambda: [0.0, 0.0, 0])
    for iso, v in temps.items():
        k = mkey(_dt.date.fromisoformat(iso))
        a = acc[k]
        a[0] += max(0.0, 65 - v)
        a[1] += max(0.0, v - 65)
        a[2] += 1
    return {k: tuple(acc[k]) for k in months if k in acc}


def fit_robust(points):
    """fit() after leaving out months far off the line (over 3.5 robust
    standard deviations: a rebill, a stuck meter, a catch-up bill).
    (base, slope, r2, [indices left out]) or None."""
    first = fit(points)
    if not first:
        return None
    base, slope, _ = first
    res = [p[1] - base - slope * p[0] for p in points]
    med = sorted(res)[len(res) // 2]
    mad = sorted(abs(r - med) for r in res)[len(res) // 2] * 1.4826
    out = [i for i, r in enumerate(res) if mad > 0 and abs(r - med) > 3.5 * mad]
    if out and len(points) - len(out) >= 6:
        again = fit([p for i, p in enumerate(points) if i not in out])
        if again:
            return again + (out,)
    return first + ([],)


def fit(points):
    """Least squares y = base + slope * x over [(x, y)]: (base, slope, r2), or None."""
    n = len(points)
    if n < 6:
        return None
    mx = sum(p[0] for p in points) / n
    my = sum(p[1] for p in points) / n
    sxx = sum((p[0] - mx) ** 2 for p in points)
    if sxx <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in points)
    slope = sxy / sxx
    base = my - slope * mx
    syy = sum((p[1] - my) ** 2 for p in points)
    ss_res = sum((p[1] - base - slope * p[0]) ** 2 for p in points)
    r2 = 1 - ss_res / syy if syy > 0 else None
    return base, slope, r2


# ── the study ────────────────────────────────────────────────────────────────
def _r(v, n=2):
    return None if v is None else round(v, n)


def study(prop, today=None, instance_path=None, weather=True):
    """Everything for one property's page. App context assumed."""
    from app.models import UtilityAccount, UtilityAccountBuilding, UtilityBill, Building
    from app.extensions import db
    today = today or _dt.date.today()
    accts = UtilityAccount.query.filter_by(source="yardi", property_id=prop.id).all()
    if not accts:
        return None
    by_id = {a.id: a for a in accts}
    bills = UtilityBill.query.filter(UtilityBill.account_id.in_(list(by_id))).all()
    if not bills:
        return None
    links = defaultdict(list)
    for x, name in (db.session.query(UtilityAccountBuilding.account_id, Building.name)
                    .join(Building, Building.id == UtilityAccountBuilding.building_id)
                    .filter(UtilityAccountBuilding.account_id.in_(list(by_id)))):
        links[x].append(name)

    imported = max(b.imported_at for b in bills).date()
    anchor = min(max(b.period_end for b in bills), imported)
    # the last month whose bills are all in: two months before the import
    last_full = mkey(anchor) - 2
    # NYC's Multi-Family Conservation Program bills water a year ahead, flat
    # per apartment; Yardi files each bill (and its voids and re-entries)
    # under one month. Where a property has such flat bills, its NYC Water
    # Board water lines without usage are spread over the 12 months they pay.
    mcp = any(b.unit == "flat" and "nyc water" in (by_id[b.account_id].utility or "").lower() for b in bills)

    def months_of(b, a):
        if (mcp and b.usage is None and a.commodity in ("water", "sewer")
                and "nyc water" in (a.utility or "").lower() and (b.period_end - b.period_start).days <= 31):
            s0 = b.period_start.replace(day=1)
            return spread(s0, _dt.date(s0.year + 1, s0.month, 1) - _dt.timedelta(days=1))
        return spread(b.period_start, b.period_end)

    first = max(min(mkey(b.period_start) for b in bills), mkey(today) - 12 * 5)
    last = min(max(months_of(b, by_id[b.account_id])[-1][0] for b in bills), mkey(today))   # prepaid months: up to now
    months = list(range(first, last + 1))
    ix = {k: i for i, k in enumerate(months)}
    n = len(months)

    def zeros():
        return [0.0] * n

    fuel = {g: {"cost": zeros(), "usage": zeros(), "own": zeros(), "cov": zeros()} for g in FUELS}
    apt_cost = zeros()
    # per account, per month: cost, usage, covered cost, days billed, flags
    am = defaultdict(lambda: {"cost": zeros(), "usage": zeros(), "cov": zeros(), "days": zeros(),
                              "twice": [False] * n, "flat": [False] * n})
    for b in bills:
        a = by_id[b.account_id]
        g = FUEL_OF.get(a.commodity)
        if g is None:
            continue
        cost = (b.cost_cents or 0) / 100
        r = am[a.id]
        covered = b.usage is not None or b.unit == "flat"
        parts = months_of(b, a)
        yearly = len(parts) >= 12 and (b.period_end - b.period_start).days <= 31
        for k, share in parts:
            if k not in ix:
                continue
            i = ix[k]
            r["cost"][i] += cost * share
            if b.usage is not None:
                r["usage"][i] += b.usage * share
            if covered:
                r["cov"][i] += cost * share
            if b.flags == "twice":
                r["twice"][i] = True
            if b.unit == "flat":
                r["flat"][i] = True
        if cost > 0:
            days = {k: mdays(k) for k, _ in parts} if yearly else covered_days(b.period_start, b.period_end)
            for k, d in days.items():
                if k in ix:
                    r["days"][ix[k]] += d

    totals = [sum(r["cost"][i] for r in am.values()) for i in range(n)]
    typical = sorted(totals)[n // 2] if n else 0
    start = next((i for i, t in enumerate(totals) if t >= 0.1 * typical), 0) if typical > 0 else 0
    if months[start] % 12 >= 9 and start + 12 - months[start] % 12 < n:
        start += 12 - months[start] % 12                   # data from Oct-Dec: start at the next January
    if start:
        months = months[start:]
        ix = {k: i for i, k in enumerate(months)}
        n = len(months)
        for r in am.values():
            for key in ("cost", "usage", "cov", "days", "twice", "flat"):
                r[key] = r[key][start:]
        fuel = {g: {"cost": zeros(), "usage": zeros(), "own": zeros(), "cov": zeros()} for g in FUELS}
        apt_cost = zeros()

    for aid, r in am.items():
        a = by_id[aid]
        g = FUEL_OF[a.commodity]
        f = fuel[g]
        for i in range(n):
            if a.kind in ("unit", "vacant"):
                apt_cost[i] += r["cost"][i]
                continue
            f["cost"][i] += r["cost"][i]
            f["usage"][i] += r["usage"][i]
            if a.kind == "building" and not a.supplier and a.commodity != "sewer":
                net = max(r["cost"][i], 0.0)
                f["own"][i] += net
                f["cov"][i] += min(max(r["cov"][i], 0.0), net)

    units = prop.unit_count
    out_fuels = {}
    for g in FUELS:
        f = fuel[g]
        cover = [(f["cov"][i] / f["own"][i]) if f["own"][i] > 0.5 else None for i in range(n)]
        full = [c is not None and c >= COVER_MIN and f["usage"][i] > 0 for i, c in enumerate(cover)]
        out_fuels[g] = {
            "unit": UNIT[g],
            "cost": [_r(v) for v in f["cost"]],
            "usage": [_r(v, 1) if v else None for v in f["usage"]],
            "cover": [_r(c, 3) for c in cover],
            "full": full,
            "price": [_r(f["cost"][i] / f["usage"][i], 4) if full[i] else None for i in range(n)],
            "per_apt_day": [_r(f["usage"][i] / units / mdays(months[i]), 4) if full[i] and units else None
                            for i in range(n)],
        }

    # years: calendar years from the monthly figures
    years = sorted({k // 12 for k in months})
    yrs = {}
    for g in FUELS:
        f, rows = fuel[g], []
        for y in years:
            idx = [ix[k] for k in months if k // 12 == y]
            done = all(months[i] <= last_full for i in idx) and len(idx) == 12
            cost = sum(f["cost"][i] for i in idx)
            use = sum(f["usage"][i] for i in idx)
            own = sum(f["own"][i] for i in idx)
            cov = sum(f["cov"][i] for i in idx)
            c = cov / own if own > 0.5 else None
            ok = c is not None and c >= COVER_MIN and use > 0
            rows.append({"year": y, "complete": done, "cost": _r(cost), "usage": _r(use, 1) if use else None,
                         "cover": _r(c, 3), "price": _r(cost / use, 4) if ok else None,
                         "per_apt": _r(use / units, 2) if ok and units and done else None,
                         "cost_per_apt": _r(cost / units, 2) if units and done else None})
        yrs[g] = rows
    apt_years = [{"year": y, "cost": _r(sum(apt_cost[ix[k]] for k in months if k // 12 == y))} for y in years]

    # meters: one row per account; apartments and no-meter lines as their own rows
    meters = []
    hi = ix.get(last_full, n - 1)
    lo = max(0, hi - 11)
    for aid, r in am.items():
        a = by_id[aid]
        billed = [i for i in range(n) if r["days"][i] > 0]
        status = []
        for i in range(n):
            if r["twice"][i]:
                s = "twice"
            elif r["flat"][i]:
                s = "flat"
            elif r["days"][i] >= 0.5 * mdays(months[i]) or abs(r["cost"][i]) > 0.005:
                s = "billed"
            elif a.kind == "building" and billed and billed[0] < i < billed[-1]:   # rolled-up lines have no cycle
                s = "gap"
            else:
                s = ""
            status.append(s)
        meters.append({
            "id": aid, "fuel": FUEL_OF[a.commodity], "commodity": a.commodity, "kind": a.kind,
            "supplier": bool(a.supplier), "utility": a.utility, "number": a.account_number,
            "address": a.service_address, "buildings": links.get(aid, []),
            "unit": UNIT.get(FUEL_OF[a.commodity]) if any(r["usage"]) else None,
            "cost": [_r(v) for v in r["cost"]],
            "usage": [_r(v, 1) if v else None for v in r["usage"]],
            "status": status,
            "cost12": _r(sum(r["cost"][lo:hi + 1])),
            "gaps": sum(1 for s in status if s == "gap"),
            "twice": sum(1 for s in status if s == "twice"),
        })
    meters.sort(key=lambda m: (FUELS.index(m["fuel"]), -(m["cost12"] or 0)))

    # weather and the weather fits
    wx = None
    if weather and instance_path:
        lat, lon, label = place_for(prop.id, instance_path)
        start = _dt.date(months[0] // 12, months[0] % 12 + 1, 1)
        temps = daily_temps(lat, lon, start, today - _dt.timedelta(days=6), instance_path)
        dd = degree_days(temps, months)
        if dd:
            hdd = [_r(dd[k][0], 1) if k in dd and dd[k][2] >= 0.8 * mdays(k) else None for k in months]
            cdd = [_r(dd[k][1], 1) if k in dd and dd[k][2] >= 0.8 * mdays(k) else None for k in months]
            fits = {}
            for g, x in (("gas", hdd), ("electric", cdd)):
                f = out_fuels[g]
                pts = [(x[i] / mdays(months[i]), fuel[g]["usage"][i] / mdays(months[i]), months[i])
                       for i in range(n) if x[i] is not None and f["full"][i] and months[i] <= last_full]
                res = fit_robust([(p[0], p[1]) for p in pts])
                if res:
                    base, slope, r2, out = res
                    kept = [p for i, p in enumerate(pts) if i not in out]
                    tot_x = sum(p[0] * mdays(p[2]) for p in kept)
                    tot_y = sum(p[1] * mdays(p[2]) for p in kept)
                    fits[g] = {"base": _r(base, 2), "slope": _r(slope, 3), "r2": _r(r2, 3), "n": len(kept),
                               "left_out": len(out),
                               "weather_share": _r(slope * tot_x / tot_y, 3) if tot_y > 0 else None,
                               "points": [[_r(p[0], 2), _r(p[1], 2), mlabel(p[2]), i in out]
                                          for i, p in enumerate(pts)]}
            wx = {"place": label, "hdd": hdd, "cdd": cdd, "fits": fits}

    return {
        "property": {"id": prop.id, "name": prop.name, "units": units},
        "months": [mlabel(k) for k in months], "last_full": mlabel(last_full),
        "fuels": out_fuels, "apartments": [_r(v) for v in apt_cost],
        "years": yrs, "apt_years": apt_years, "meters": meters, "weather": wx,
        "cover_min": COVER_MIN, "imported": imported.isoformat(), "mcp": mcp,
        "counts": {"accounts": len(accts), "bills": len(bills)},
    }
