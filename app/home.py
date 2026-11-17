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

# ── "How to read this page" ──────────────────────────────────────────────────
#
# Written for someone opening the portal for the first time. The first outside
# viewer's first questions (2026-11-17) were what "Heating" and "OK" mean, and
# what to do about "Not reporting" and "Fan down" — none of which the page
# said. One list per table drives both the guide at the top of the page and
# the tooltip on every status pill, so the two can never disagree.
# key = the entry's status tone (heating) / fan_key() (fans); css = the pill.
#
# "What to do" is the owner's decision, not the portal's (2026-11-17): a
# controller that is not reporting is emailed in, with no step on site; a fan
# that is down gets the property's own maintenance person or electrician
# first. Nobody is named: the page serves every viewer, whoever invited them.
TELL = "email the person who gave you this access"
HEAT_GUIDE = [
    {"key": "crit", "css": "crit", "label": "Problem",
     "means": "The boiler is stopped on a safety or failed to light, or the building is calling for heat and "
              "no heat is moving. The row says which.",
     "do": f"This needs someone at the boiler. Please {TELL} right away."},
    {"key": "warn", "css": "warn", "label": "Warning",
     "means": "The controller has been switched to manual (bypass) or has an open alarm. "
              "The building may still be heating normally.",
     "do": "Not urgent, but worth asking about the same day."},
    {"key": "heat", "css": "heat", "label": "Heating",
     "means": "Normal. The boiler or the pump is running right now: to heat the building, or to keep the hot water hot.",
     "do": "Nothing."},
    {"key": "ok", "css": "ok", "label": "OK",
     "means": "Normal. The controller is reporting and the heat is resting, because it is warm enough outside or in "
              "the apartments, or the heat cycle has finished. It starts again by itself when heat is needed.",
     "do": "Nothing."},
    {"key": "dark", "css": "dark", "label": "Not reporting",
     "means": "The portal cannot reach the building's controller, so nothing is known about the building right now. "
              "This is about the connection: by itself it does not mean the heat is off.",
     "do": f"A gap of a few minutes usually clears by itself. If the row shows it has lasted more than an hour, {TELL}."},
]
FAN_GUIDE = [
    {"key": "fail", "css": "crit", "label": "Fan down",
     "means": "At least one fan has been told to run and is not running. The Down column says how many; "
              "click the building to see which fan.",
     "do": "Send your own maintenance person or electrician to that fan first: the switch at the fan, the breaker, "
           f"the belt and the motor. If the fan is fine and still does not run, {TELL}."},
    {"key": "hand", "css": "warn", "label": "Ignoring stop",
     "means": "At least one fan is running although it has been told to stop. Usually its switch at the fan is set to Hand.",
     "do": "Not urgent: the fan works, it is only running when it should rest. Have your maintenance person "
           "check that the switch at the fan is on Auto."},
    {"key": "on", "css": "ok", "label": "Running",
     "means": "Normal. The fans have been told to run and are running.",
     "do": "Nothing."},
    {"key": "off", "css": "dark", "label": "Off",
     "means": "Normal. The fans are off on their schedule: in cold weather they rest for part of the day.",
     "do": "Nothing."},
    {"key": "stale", "css": "dark", "label": "Not reporting",
     "means": "Nothing has been heard from the building's fan controller, so nothing is known about those fans right now.",
     "do": f"If the row shows it has lasted more than an hour, {TELL}."},
]
COLUMNS_GUIDE = [
    ("Outdoor", "the temperature outside the building"),
    ("Average", "the average of the apartment temperature sensors"),
    ("Setpoint", "the inside temperature the building is set to hold"),
    ("Supply and Return", "the heating pipe leaving the boiler, and the pipe coming back to it"),
    ("Hot water", "the hot water going out to the taps"),
    ("Heat run", "how long the heat has run today, in hours and minutes"),
    ("Read by", "which controller reports the building"),
]
_HEAT_BY_KEY = {r["key"]: r for r in HEAT_GUIDE}
_FAN_BY_KEY = {r["key"]: r for r in FAN_GUIDE}
GUIDE = {"heat": HEAT_GUIDE, "fans": FAN_GUIDE, "columns": COLUMNS_GUIDE}


def fan_key(f) -> str:
    """Which FAN_GUIDE row a fan building is in. Worst first: a building that
    is not reporting says nothing else; a fan down outranks one ignoring a stop."""
    if f.get("stale"):
        return "stale"
    if f.get("fail"):
        return "fail"
    if f.get("hand"):
        return "hand"
    return "on" if f.get("on") else "off"


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
        e["tip"] = (_HEAT_BY_KEY.get(e.get("status")) or {}).get("means")
        e["source_label"] = SOURCE_LABEL.get(e.get("source"), e.get("source") or "—")
        p["buildings"].append(e)
    for f in fans:
        p = props.get(f.get("prop_id"))
        if p is not None:                               # a fan site is only shown under a property being shown
            g = _FAN_BY_KEY[fan_key(f)]
            p["fans"].append(dict(f, pill=g["label"], pill_css=g["css"], tip=g["means"]))

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
