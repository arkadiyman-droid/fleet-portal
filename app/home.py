"""
app/home.py — "My properties": what one signed-in person is allowed to watch.

The landing page for an account that is not the administrator. It shows the
properties that account has been granted (app/permissions.py), each with its
buildings' heating status — from whatever reads the building: an EchoTherm
controller, a Bloom asset or a 2010 box (app/portfolio.building_entry) — and
its roof exhaust fans (app/bloom_fans.summary).

assemble() is pure: the route hands it the already-scoped buildings and fan
rows, so nothing here decides who may see what. Read-only.
"""

TONE_ORDER = {"crit": 0, "warn": 1, "heat": 2, "ok": 3, "dark": 4}
TONE_LABEL = {"crit": "Problem", "warn": "Warning", "heat": "Heating", "ok": "OK", "dark": "Not reporting"}
SOURCE_LABEL = {"EchoTherm": "EchoTherm controller", "Bloom": "Bloom", "2010": "2010 controller", "—": "no controller"}


def _natural(s):
    import re
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(s or "").lower())]


def assemble(buildings, fans) -> dict:
    """buildings: [{"client", "client_id", "prop", "prop_id", "entry"}] where
    entry is a portfolio.building_entry dict. fans: fan summary dicts carrying
    "prop_id", "name", "on", "total", "fail", "stale", "tone", "status".
    Returns {"owners": [...], "summary": {...}}: owners -> properties, each
    with its buildings (worst first), its fan rows and its own tally."""
    props = {}
    for b in buildings:
        p = props.setdefault(b["prop_id"], {"id": b["prop_id"], "name": b["prop"], "client": b["client"],
                                            "client_id": b["client_id"], "buildings": [], "fans": []})
        e = dict(b["entry"])
        e["tone_label"] = TONE_LABEL.get(e.get("status"), "—")
        e["source_label"] = SOURCE_LABEL.get(e.get("source"), e.get("source") or "—")
        p["buildings"].append(e)
    for f in fans:
        p = props.get(f.get("prop_id"))
        if p is not None:                               # a fan site is only shown under a property being shown
            p["fans"].append(f)

    total = {"buildings": 0, "crit": 0, "warn": 0, "heat": 0, "ok": 0, "dark": 0,
             "fans": 0, "fans_on": 0, "fans_down": 0, "fan_sites": 0, "fan_dark": 0}
    for p in props.values():
        p["buildings"].sort(key=lambda e: (TONE_ORDER.get(e.get("status"), 9), _natural(e.get("name"))))
        p["fans"].sort(key=lambda f: _natural(f.get("name")))
        t = {"buildings": len(p["buildings"]), "crit": 0, "warn": 0, "heat": 0, "ok": 0, "dark": 0}
        for e in p["buildings"]:
            if e.get("status") in t:
                t[e["status"]] += 1
        live = [f for f in p["fans"] if not f.get("stale")]
        t.update(fan_sites=len(p["fans"]), fan_dark=len(p["fans"]) - len(live),
                 fans=sum(f.get("total") or 0 for f in live), fans_on=sum(f.get("on") or 0 for f in live),
                 fans_down=sum(f.get("fail") or 0 for f in live))
        p["tally"] = t
        for k in total:
            total[k] += t.get(k, 0)

    owners = {}
    for p in sorted(props.values(), key=lambda p: (str(p["client"] or "").lower(), _natural(p["name"]))):
        owners.setdefault(p["client_id"], {"name": p["client"], "props": []})["props"].append(p)
    total["properties"] = len(props)
    return {"owners": list(owners.values()), "summary": total}
