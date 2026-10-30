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

from app.models import BloomAsset, Building, Client, LegacyBox

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

def _blank(name, source, url=None):
    return {"name": name, "source": source, "status": "dark", "out": None, "sup": None, "ret": None,
            "rt": None, "dhw": None, "spt": None, "mode": None, "alarms": [], "url": url}


def _bloom_entry(name, ba):
    from app import bloom
    url = f"/skyline/{ba.id}"
    st = ba.status
    if not (st and st.ok and st.data):
        return _blank(name, "Bloom", url)
    c = bloom.card(st.data, ba.view, ba.plant_tag)
    return {"name": name, "source": "Bloom", "status": c["tone"], "out": c["outside_f"],
            "sup": c["supply_f"], "ret": c["return_f"], "rt": _hhmm(c["heat_rt"]),
            "dhw": c["dhw_supply_f"], "spt": c["setpoint_f"], "mode": c["mode"],
            "alarms": c["alarms"], "url": url}


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
    return {"name": name, "source": "EchoTherm", "status": tone, "out": heating.get("outdoor"),
            "sup": heating.get("supply"), "ret": heating.get("ret"), "rt": None,
            "dhw": None, "spt": heating.get("target"), "mode": heating.get("mode"),
            "alarms": alarms, "url": f"/fleet/{ctrl.id}"}


def _legacy_entry(name, box, now_dt):
    from app import legacy
    st = box.status
    c = legacy.card(st.data) if st and st.data else None
    last_ok = st.last_ok_at if st else None
    if box.identity == "other" or (not last_ok) or (now_dt - last_ok) > legacy.STALE or not c:
        e = _blank(name, "2010", f"/live/{box.id}")
    else:
        tone = {"idle": "ok", "heat": "heat", "ok": "ok", "warn": "warn", "crit": "crit"}.get(c.get("tone"), "ok")
        e = {"name": name, "source": "2010", "status": tone, "out": c.get("out"),
             "sup": c.get("supp"), "ret": c.get("retn"),
             "rt": legacy.hhmm(c["heat_rt"]) if c.get("heat_rt") is not None else None,
             "dhw": None, "spt": c.get("dit"), "mode": None,
             "alarms": [], "url": f"/live/{box.id}"}
    e["id"] = box.id
    e["kind"] = "legacy"
    return e


def building_entry(b, now, now_dt):
    """One building -> a unified status entry, by whatever drives it. A managed
    EchoTherm controller wins, then a Bloom asset, then a linked 2010 box.
    Carries building_id + kind="building" so the page can move it."""
    ctrl = next((c for c in b.controllers if c.is_active), None)
    if ctrl:
        e = _echo_entry(b.name, ctrl, now, now_dt)
    else:
        ba = BloomAsset.query.filter_by(building_id=b.id, is_active=True, deleted_at=None).first()
        if ba:
            e = _bloom_entry(b.name, ba)
        else:
            lb = LegacyBox.query.filter_by(building_id=b.id, is_active=True, deleted_at=None).first()
            e = _legacy_entry(b.name, lb, now_dt) if lb else dict(_blank(b.name, "—", None), status="dark")
    e["building_id"] = b.id
    e["kind"] = "building"
    return e


# ── tree + unassigned ────────────────────────────────────────────────────────

def tree():
    """Owners -> properties -> building entries, for the page's left tree."""
    now, now_dt = _time.time(), _dt.datetime.utcnow()
    owners = []
    for cl in Client.query.filter_by(is_active=True).order_by(Client.name).all():
        props = []
        for p in sorted(cl.properties, key=lambda x: x.name.lower()):
            bs = [building_entry(b, now, now_dt) for b in sorted(p.buildings, key=lambda x: x.name.lower())]
            props.append({"id": p.id, "name": p.name, "loc": p.address or "", "b": bs})
        owners.append({"id": cl.id, "name": cl.name, "props": props})
    return owners


def unassigned():
    """2010 oBIX boxes not yet filed into a property — the bucket to sort."""
    now_dt = _dt.datetime.utcnow()
    boxes = (LegacyBox.query
             .filter(LegacyBox.building_id.is_(None), LegacyBox.is_active.is_(True),
                     LegacyBox.deleted_at.is_(None))
             .order_by(LegacyBox.name).all())
    return [_legacy_entry(bx.name, bx, now_dt) for bx in boxes]
