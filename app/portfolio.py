"""
app/portfolio.py — the whole fleet, browsed by owner and property.

One place turns each building into a single status entry regardless of what
drives it — an EchoTherm controller (app/fleet_health.py), a Bloom asset
(app/bloom.py) or a 2010 oBIX box (app/legacy.py) — and builds the
Client -> Property -> Building tree the /portfolio page renders, plus the
"Unassigned" bucket of 2010 boxes not yet filed into a property.

Read-only (DB reads only); the entry dicts serialise straight to JSON for the
page. Status tones are unified to: crit, warn, heat, ok, dark.
"""
import datetime as _dt
import time as _time

from app.models import BloomAsset, Building, Client, LegacyBox, RcDhwTower

TONES = ("crit", "warn", "heat", "ok", "dark")


def _hhmm(mins):
    if mins is None:
        return None
    try:
        mins = int(mins)
    except (TypeError, ValueError):
        return None
    return f"{mins // 60}:{mins % 60:02d}"


# ── per-source entries (all return the same shape) ───────────────────────────

# What a 2010 box is doing, in words for someone who has never met its heat-mode
# codes (legacy.STATUS keeps the engineer's wording, which a code not listed
# here falls back to). "Resting" = the heat is off because nothing needs it.
_LEGACY_DOING = {
    "Summer": "Heat is off for the summer",
    "Outsd Sat": "Resting: warm enough outside",
    "Insd Sat": "Resting: the apartments are warm enough",
    "DTime Sat": "Resting: this heat cycle has finished",
    "DTime Rem": "Heating: a heat cycle is running",
    "Aqua Call": "Boiler is firing to keep its water hot",
    "Pressure": "Heating: the boiler is building pressure",
    "Circ Pump": "Heating: the pump is circulating",
    "Ign Fail": "The boiler tried to light and failed",
    "Safety": "The boiler is stopped on a safety",
}


def _silent(last_ok):
    """What a building whose controller is not answering is 'doing': since when
    (New York time), so a two-minute gap and a two-day one do not look alike."""
    if not last_ok:
        return "The controller has never answered"
    from app import legacy
    at = last_ok.replace(tzinfo=_dt.timezone.utc).astimezone(legacy.LOCAL)
    return "The controller has not answered since " + at.strftime("%b %d, %I:%M %p").replace(" 0", " ")


def _blank(name, source, url=None, doing=None):
    # avg = the average of the apartment / space sensors; dhw = the hot water
    # going out to the building (the mixing-valve reading where there is one);
    # doing = one plain sentence on what the building is doing right now.
    return {"name": name, "source": source, "status": "dark", "out": None, "avg": None, "no_sensors": False,
            "sup": None, "ret": None, "rt": None, "dhw": None, "dhw_src": None, "spt": None, "mode": None,
            "alarms": [], "doing": doing, "url": url}


def _bloom_entry(name, ba):
    from app import bloom
    url = f"/skyline/{ba.id}"
    st = ba.status
    if not (st and st.ok and st.data):
        return _blank(name, "Bloom", url, _silent(st.last_ok_at if st else None))
    c = bloom.card(st.data, ba.view, ba.plant_tag)
    if c["cold_call"]:
        doing = "Calling for heat, but no heat is moving"
    elif c["heating"]:
        doing = "Heating: heat is moving through the building"
    else:
        doing = "Resting: nothing is calling for heat"
    return {"name": name, "source": "Bloom", "status": c["tone"], "out": c["outside_f"],
            "avg": c.get("space_avg_f"), "no_sensors": False,
            "sup": c["supply_f"], "ret": c["return_f"], "rt": _hhmm(c["heat_rt"]),
            "dhw": c["dhw_supply_f"], "dhw_src": "Bloom" if c["dhw_supply_f"] is not None else None,
            "spt": c["setpoint_f"], "mode": c["mode"],
            "alarms": c["alarms"], "doing": doing, "url": url}


def _echo_entry(name, ctrl, now, now_dt):
    from app import fleet_health
    ev = fleet_health.evaluate(ctrl, getattr(ctrl, "status", None), now, now_dt)
    lvl, heating = ev.get("level"), (ev.get("heating") or {})
    if ev.get("online") is False or lvl == "unknown":
        tone = "dark"
    elif lvl == "crit":
        tone = "crit"
    elif lvl == "warn":
        tone = "warn"
    elif heating.get("demand"):
        tone = "heat"
    else:
        tone = "ok"
    alarms = [(a.get("label") or f"alarm {a.get('type')}") for a in (ev.get("alarms") or [])]
    if tone == "dark":
        seen = ev.get("seen")
        doing = "The controller is not answering" + (f" ({seen})" if seen and seen != "never" else "")
    else:
        doing = heating.get("reason") or ("Heating" if heating.get("demand") else "Resting: nothing is calling for heat")
    return {"name": name, "source": "EchoTherm", "status": tone, "out": heating.get("outdoor"),
            "avg": heating.get("avgApt"), "no_sensors": bool(heating.get("noSensors")),
            "sup": heating.get("supply"), "ret": heating.get("ret"), "rt": None,
            "dhw": None, "dhw_src": None, "spt": heating.get("target"), "mode": heating.get("mode"),
            "alarms": alarms, "doing": doing, "url": f"/fleet/{ctrl.id}"}


def _legacy_entry(name, box, now_dt):
    from app import legacy
    st = box.status
    c = legacy.card(st.data, guard_out=True) if st and st.data else None   # live display: drop a railed OAT to "—"
    last_ok = st.last_ok_at if st else None
    if box.identity == "other" or (not last_ok) or (now_dt - last_ok) > legacy.STALE or not c:
        e = _blank(name, "2010", f"/live/{box.id}", _silent(last_ok))
    else:
        tone = {"idle": "ok", "heat": "heat", "ok": "ok", "warn": "warn", "crit": "crit"}.get(c.get("tone"), "ok")
        # The Live wall's "Avg" and "DHW" columns: the box's average of its
        # apartment sensors (None while every sensor is silent — the box then
        # reports a placeholder, legacy.no_sensors) and its mixing-valve sensor.
        # A mix of 0 is a sensor that is not reading, not hot water at 0 degrees.
        mix = c.get("mix") or None
        e = {"name": name, "source": "2010", "status": tone, "out": c.get("out"),
             "avg": c.get("inside"), "no_sensors": bool(c.get("no_sensors")),
             "sup": c.get("supp"), "ret": c.get("retn"),
             "rt": legacy.hhmm(c["heat_rt"]) if c.get("heat_rt") is not None else None,
             "dhw": mix, "dhw_src": "mixing valve" if mix is not None else None,
             "spt": c.get("dit"), "mode": None, "alarms": [],
             "doing": _LEGACY_DOING.get(c.get("code")) or c.get("status"), "url": f"/live/{box.id}"}
    e["id"] = box.id
    e["kind"] = "legacy"
    return e


def rc_dhw_by_building() -> dict:
    """{building id: hot-water supply} from the Reliable Controls scrape
    (app/rc_dhw.py), for the buildings whose hot water no controller reads."""
    from sqlalchemy.orm import joinedload
    out = {}
    for tw in RcDhwTower.query.options(joinedload(RcDhwTower.status)).filter(RcDhwTower.is_active.is_(True)).all():
        st = tw.status
        if tw.building_id and st and st.ok and st.supply_f is not None:
            out[tw.building_id] = st.supply_f
    return out


def drivers() -> dict:
    """Every building's Bloom asset and 2010 box, each with its status, in two
    queries: {"bloom": {building id: asset}, "legacy": {building id: box}}.
    A caller building many entries passes this to building_entry(); asking per
    building was ~4 queries each (900 for the Portfolio page, 3 s on the Pi).
    Where a building has two of one kind, the lowest id wins, as before."""
    from sqlalchemy.orm import joinedload
    out = {"bloom": {}, "legacy": {}}
    for kind, model in (("bloom", BloomAsset), ("legacy", LegacyBox)):
        for x in (model.query.options(joinedload(model.status))
                  .filter(model.building_id.isnot(None), model.is_active.is_(True), model.deleted_at.is_(None))
                  .order_by(model.id).all()):
            out[kind].setdefault(x.building_id, x)
    return out


def building_entry(b, now, now_dt, rc_dhw=None, found=None):
    """One building -> a unified status entry, by whatever drives it. A managed
    EchoTherm controller wins, then a Bloom asset, then a linked 2010 box.
    Carries building_id + kind="building" so the page can move it. `rc_dhw` is
    rc_dhw_by_building(), fetched once by a caller that builds many entries:
    where the building's own controller has no hot-water reading, the Reliable
    Controls one is used. `found` is drivers(), fetched once the same way;
    without it each building is looked up on its own."""
    ctrl = next((c for c in b.controllers if c.is_active), None)
    if ctrl:
        e = _echo_entry(b.name, ctrl, now, now_dt)
    else:
        ba = found["bloom"].get(b.id) if found is not None else             BloomAsset.query.filter_by(building_id=b.id, is_active=True, deleted_at=None).order_by(BloomAsset.id).first()
        if ba:
            e = _bloom_entry(b.name, ba)
        else:
            lb = found["legacy"].get(b.id) if found is not None else                 LegacyBox.query.filter_by(building_id=b.id, is_active=True, deleted_at=None).order_by(LegacyBox.id).first()
            e = _legacy_entry(b.name, lb, now_dt) if lb else _blank(
                b.name, "—", None, "No controller is connected to this building yet")
    if e.get("dhw") is None and rc_dhw and rc_dhw.get(b.id) is not None:
        e["dhw"], e["dhw_src"] = rc_dhw[b.id], "Reliable Controls"
    e["building_id"] = b.id
    e["kind"] = "building"
    return e


# ── tree + unassigned ────────────────────────────────────────────────────────

def tree():
    """Owners -> properties -> building entries, for the page's left tree."""
    from sqlalchemy.orm import joinedload, selectinload
    from app.models import Controller, Property
    now, now_dt = _time.time(), _dt.datetime.utcnow()
    rc_dhw, found = rc_dhw_by_building(), drivers()
    owners = []
    clients = (Client.query.options(selectinload(Client.properties).selectinload(Property.buildings)
                                    .selectinload(Building.controllers).joinedload(Controller.status))
               .filter_by(is_active=True).order_by(Client.name).all())
    for cl in clients:
        props = []
        for p in sorted(cl.properties, key=lambda x: x.name.lower()):
            bs = [building_entry(b, now, now_dt, rc_dhw, found) for b in sorted(p.buildings, key=lambda x: x.name.lower())]
            props.append({"id": p.id, "name": p.name, "loc": p.address or "", "b": bs})
        owners.append({"id": cl.id, "name": cl.name, "props": props})
    return owners


def unassigned():
    """2010 oBIX boxes not yet filed into a property — the bucket to sort."""
    now_dt = _dt.datetime.utcnow()
    from sqlalchemy.orm import joinedload
    boxes = (LegacyBox.query.options(joinedload(LegacyBox.status))
             .filter(LegacyBox.building_id.is_(None), LegacyBox.is_active.is_(True),
                     LegacyBox.deleted_at.is_(None))
             .order_by(LegacyBox.name).all())
    return [_legacy_entry(bx.name, bx, now_dt) for bx in boxes]
