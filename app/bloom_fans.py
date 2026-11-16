"""
app/bloom_fans.py — roof exhaust fans on Bloom buildings, read view-only.

Many Bloom buildings carry roof exhaust fans beside (or instead of) a heating
plant. They run on a schedule, and on a very cold day they pull the heat out of
the building — so the operator wants one page that shows, for every building,
whether the fans are running, what the cold-weather schedule would do, and
whether that schedule can engage at all. This module turns one building object
from Bloom's /api/assets into that picture. Nothing here writes to Bloom.

The feed, decoded against the live "omni" account 2026-11-16 (50 buildings, 243
fans):

  equipment   {type:"fan", key:"exhaust-3", name:"Exhaust Fan 3"}
              {type:"fanGroup", key:"fg-1", name:..., fans:["exhaust-1", ...]}
  fan-rs          per fan      1 = running
  fan-run-fail    per fan      1 = Bloom's own "did not run when commanded" flag
  fan-run-manual  per fan      1 = in hand / manual
  fan-rt-day      per fan      run time today, SECONDS (some counters never
                               reset and read thousands of hours — shown as bad)
  fan-cmd         per group    1 = START
  gen-sched-assign  name="fg-1"     pv = which schedule (1-5) the group follows
  gen-sched         name="S::H"     pv = 1 if schedule S runs the fans in hour H
  oat-sp-gen        name="S"        schedule S's outdoor setpoint (deg F)
  gen-sp-enabled    name="oatSp::S" 1 = schedule S only applies BELOW its setpoint
  oat-act           outdoor temperature; a building can carry several, some
                    years stale — the freshest by pollTs is the real one

Every sensor has its own pollTs. A building whose fan points have not been
polled for hours is not reporting, however healthy its last values look, so
age is part of the answer everywhere (six Gates Ave buildings were 65 days
stale on the day this was written).

What is NOT decoded: how the controller behaves when a schedule's outdoor
setpoint is enabled but it is warmer than the setpoint. Observed: the fans run
around the clock. That is reported as seen (command + schedule), never used to
predict or judge a fan.
"""
import datetime as _dt
import logging

log = logging.getLogger(__name__)

STALE_MIN = 120            # fan / outdoor points older than this are not live
DAY_SECONDS = 86400

_KEEP_PREFIX = ("fan-", "gen-sched", "gen-sp-enabled", "oat-")
_KEEP_FIELDS = ("type", "pv", "assoc", "name", "pollTs", "unit")


# ── raw feed -> the trimmed slice we store ─────────────────────────────────────

def has_fans(building) -> bool:
    return any((s.get("type") == "fan-rs") for s in (building.get("sensors") or []))


def trim(building) -> dict:
    """The fan-related slice of one /api/assets building: raw, so a Bloom-side
    change never needs a migration, but without the ~50 KB of heating points."""
    return {
        "name": building.get("name"),
        "locid": building.get("locid"),
        "equipment": [e for e in (building.get("equipment") or [])
                      if e.get("type") in ("fan", "fanGroup")],
        "sensors": [{k: s.get(k) for k in _KEEP_FIELDS if s.get(k) is not None}
                    for s in (building.get("sensors") or [])
                    if str(s.get("type") or "").startswith(_KEEP_PREFIX)],
    }


# ── helpers ────────────────────────────────────────────────────────────────────

def _ts(v):
    """Bloom's ISO pollTs -> naive UTC datetime, or None."""
    if not v:
        return None
    try:
        t = _dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t.astimezone(_dt.timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _on(v) -> bool:
    return v in (1, True, "1", "On", "on", "ON")


def _age_min(ts, now):
    return None if ts is None else max(0.0, (now - ts).total_seconds() / 60.0)


def age_words(minutes):
    """'12 min' / '5 h' / '65 d' for a data age."""
    if minutes is None:
        return "never"
    if minutes < 90:
        return f"{int(round(minutes))} min"
    if minutes < 48 * 60:
        return f"{int(round(minutes / 60))} h"
    return f"{int(round(minutes / 1440))} d"


def _clock(h):
    h %= 24
    return f"{12 if h % 12 == 0 else h % 12}{'am' if h < 12 else 'pm'}"


def ranges(bits) -> str:
    """24 hourly on/off bits -> '8am–11am, 6pm–9pm'; 'off all day' / 'all day'."""
    on = [bool(b) for b in bits]
    if not any(on):
        return "off all day"
    if all(on):
        return "all day"
    out, h = [], 0
    while h < 24:
        if on[h]:
            start = h
            while h < 24 and on[h]:
                h += 1
            out.append(f"{_clock(start)}–{_clock(h)}")
        else:
            h += 1
    return ", ".join(out)


def _schedules(sensors) -> dict:
    """{n: {bits[24], hours_on, ranges, sp, sp_on}} for every schedule present."""
    bits, sp, sp_on = {}, {}, {}
    for s in sensors:
        t, name = s.get("type"), str(s.get("name") or "")
        try:
            if t == "gen-sched" and "::" in name:
                a, h = name.split("::", 1)
                bits.setdefault(int(a), [0] * 24)[int(h) % 24] = 1 if _on(s.get("pv")) else 0
            elif t == "oat-sp-gen":
                sp[int(name)] = _num(s.get("pv"))
            elif t == "gen-sp-enabled" and "::" in name:
                sp_on[int(name.split("::", 1)[1])] = _on(s.get("pv"))
        except (ValueError, IndexError):
            continue
    out = {}
    for n in sorted(set(bits) | set(sp) | set(sp_on)):
        b = bits.get(n, [0] * 24)
        out[n] = {"n": n, "bits": b, "hours_on": sum(b), "ranges": ranges(b),
                  "sp": sp.get(n), "sp_on": bool(sp_on.get(n))}
    return out


def _outdoor(sensors, now):
    """(value, age_min) of the freshest outdoor reading, or (None, None)."""
    best = None
    for s in sensors:
        if s.get("type") != "oat-act" or _num(s.get("pv")) is None:
            continue
        ts = _ts(s.get("pollTs"))
        if best is None or (ts or _dt.datetime.min) > (best[1] or _dt.datetime.min):
            best = (s.get("pv"), ts)
    if best is None:
        return None, None
    return best[0], _age_min(best[1], now)


# ── the picture of one building ────────────────────────────────────────────────

TONE_ORDER = {"cold": 0, "blind": 1, "fault": 2, "dark": 3, "off": 4, "ok": 5}


def summary(raw: dict, now=None, cold_f=35.0, hour=None) -> dict:
    """Everything the Fans page shows for one building, derived at read time
    from the stored slice. `now` is naive UTC; `hour` is the local hour (0-23)
    used only to mark the current column of each schedule."""
    now = now or _dt.datetime.utcnow()
    sensors = (raw or {}).get("sensors") or []
    equipment = (raw or {}).get("equipment") or []

    per = {}                                    # equipment key -> {sensor type: pv}
    heard = None                                # freshest fan-rs poll
    for s in sensors:
        a = s.get("assoc") or {}
        if a.get("type") == "equipment" and a.get("key"):
            per.setdefault(a["key"], {})[s.get("type")] = s.get("pv")
        if s.get("type") == "fan-rs":
            ts = _ts(s.get("pollTs"))
            if ts and (heard is None or ts > heard):
                heard = ts
    assign = {}
    for s in sensors:
        if s.get("type") == "gen-sched-assign" and s.get("name"):
            try:
                assign[str(s["name"])] = int(s.get("pv"))
            except (TypeError, ValueError):
                pass

    scheds = _schedules(sensors)
    names = {e.get("key"): e.get("name") for e in equipment if e.get("type") == "fan"}
    fan_keys = [e.get("key") for e in equipment if e.get("type") == "fan"]
    fan_keys += sorted(k for k, v in per.items() if "fan-rs" in v and k not in names)

    def fan(key, cmd):
        v = per.get(key, {})
        on = _on(v.get("fan-rs"))
        rt = _num(v.get("fan-rt-day"))
        rt_bad = rt is not None and rt > DAY_SECONDS      # a counter that never resets
        flag = _on(v.get("fan-run-fail"))
        # A fault is a fan that is OFF when it should be on: its group is
        # commanded on, or (no group command known) Bloom's run-fail flag is
        # set. A fan whose group is commanded off is simply off. The flag also
        # sits latched on a handful of fans that are running; those are shown
        # as "flagged", not counted as down.
        return {"key": key, "name": names.get(key) or key, "on": on,
                "fail": (not on) and (cmd is True or (cmd is None and flag)), "flagged": on and flag,
                "manual": _on(v.get("fan-run-manual")),
                "rt_h": None if (rt is None or rt_bad) else rt / 3600.0, "rt_bad": rt_bad}

    groups, seen = [], set()
    for e in equipment:
        if e.get("type") != "fanGroup":
            continue
        gv = per.get(e.get("key"), {})
        cmd = _on(gv["fan-cmd"]) if "fan-cmd" in gv else None
        members = [k for k in (e.get("fans") or []) if k in fan_keys]
        seen.update(members)
        groups.append({"key": e.get("key"), "name": e.get("name") or e.get("key"), "cmd": cmd,
                       "sched": assign.get(str(e.get("key"))),
                       "fans": [fan(k, cmd) for k in members]})
    loose = [k for k in fan_keys if k not in seen]
    if loose:
        groups.append({"key": None, "name": "Not in a group", "cmd": None, "sched": None,
                       "fans": [fan(k, None) for k in loose]})

    fans = [f for g in groups for f in g["fans"]]
    total, on = len(fans), sum(f["on"] for f in fans)
    fail = sum(f["fail"] for f in fans)
    rts = ([f["rt_h"] for f in fans if f["on"] and f["rt_h"] is not None]
           or [f["rt_h"] for f in fans if f["rt_h"] is not None and not any(x["rt_bad"] for x in fans)])

    out_v, out_age = _outdoor(sensors, now)
    out_stale = out_v is not None and (out_age is None or out_age > STALE_MIN)
    outdoor = None if (out_v is None or out_stale) else out_v
    heard_age = _age_min(heard, now)
    stale = heard_age is None or heard_age > STALE_MIN

    # The cold-weather cutback: the schedule(s) the groups follow that only
    # apply below an outdoor setpoint. One line when every group agrees (the
    # normal case), else the warmest setpoint — the first to engage.
    used = [scheds[g["sched"]] for g in groups if g["sched"] in scheds]
    armed = [s for s in used if s["sp_on"] and s["sp"] is not None]
    cut = max(armed, key=lambda s: s["sp"]) if armed else None
    cut_active = bool(cut and outdoor is not None and outdoor < cut["sp"])
    all_off = bool(groups) and all(g["cmd"] is False for g in groups if g["key"])
    cold = outdoor is not None and outdoor <= cold_f

    flags = []
    if stale:
        tone, status = "dark", f"Not reporting — last heard {age_words(heard_age)} ago"
    else:
        if cold and on:
            said = ""
            if cut_active and hour is not None:       # what the cold schedule itself says for this hour
                said = " — scheduled on this hour" if cut["bits"][hour % 24] else " — schedule says off"
            flags.append(("cold", f"{on} fan{'s' if on != 1 else ''} running at {outdoor:.0f}° outside{said}"))
        if outdoor is None and armed:
            why = (f"outdoor reading is {age_words(out_age)} old" if out_stale else "no outdoor reading")
            flags.append(("blind", f"Cold schedule cannot engage — {why}"))
        if fail:
            flags.append(("fault", f"{fail} fan{'s' if fail != 1 else ''} not running when commanded"))
        if flags:
            tone = min((t for t, _ in flags), key=TONE_ORDER.get)
            status = flags[0][1] if tone == flags[0][0] else next(m for t, m in flags if t == tone)
        elif all_off:
            tone, status = "off", "Fans commanded off"
        elif cut_active:
            tone, status = "ok", "On the cold schedule"
        else:
            tone, status = "ok", "Running" if on else "Idle"

    if hour is not None:
        for s in scheds.values():
            s["now_on"] = bool(s["bits"][hour % 24])

    return {
        "name": (raw or {}).get("name"), "locid": (raw or {}).get("locid"),
        "tone": tone, "status": status, "flags": [m for _, m in flags],
        "total": total, "on": on, "fail": fail, "manual": sum(f["manual"] for f in fans),
        "outdoor": outdoor, "outdoor_last": out_v, "outdoor_age": out_age, "outdoor_stale": out_stale,
        "heard_age": heard_age, "stale": stale,
        "cut": cut, "cut_active": cut_active, "cold": cold, "all_off": all_off,
        "rt_max_h": max(rts) if rts else None, "rt_bad": any(f["rt_bad"] for f in fans),
        "groups": groups, "schedules": [scheds[n] for n in sorted(scheds)], "hour": hour,
    }


def history_sample(s: dict) -> dict:
    """The few numbers worth keeping over time, from a summary()."""
    return {"on": s["on"], "total": s["total"], "fail": s["fail"], "out": s["outdoor"],
            "cut": s["cut_active"],
            "rs": "".join("1" if f["on"] else "0" for g in s["groups"] for f in g["fans"])}


# ── history for the chart ──────────────────────────────────────────────────────

def _ms(t):
    return int(t.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)


def _short(name, i):
    """'Exhaust Fan 10' -> '10' for the chart's row label."""
    tail = str(name or "").rsplit(" ", 1)[-1]
    return tail if tail.isdigit() else str(i + 1)


def series_from(samples, fans, sp, days, now) -> dict:
    """Chart data from [(ts, sample), ...] oldest first: outdoor temperature,
    fans running, and one on/off row per fan. `fans` is the current fan list
    ([{name}, ...]) in the order history_sample() writes its `rs` bits. Raw
    samples up to 7 days; hourly averages beyond, where a fan's value becomes
    the fraction of the hour it ran."""
    start = now - _dt.timedelta(days=days)
    n = len(fans)

    def bits(d):
        rs = str((d or {}).get("rs") or "")
        return [(1 if rs[i] == "1" else 0) if i < len(rs) else None for i in range(n)]

    out = {"range": [_ms(start), _ms(now)], "days": days, "sp": sp, "note": None,
           "fans": [{"name": f["name"], "label": _short(f["name"], i), "data": []} for i, f in enumerate(fans)]}
    if days > 7 and samples:
        buckets = {}
        for ts, d in samples:
            buckets.setdefault(ts.replace(minute=0, second=0, microsecond=0), []).append(d or {})
        hs = sorted(buckets)

        def avg(vals, nd=1):
            vals = [v for v in vals if v is not None]
            return round(sum(vals) / len(vals), nd) if vals else None

        out["res"] = "hourly averages"
        out["t"] = [_ms(h) for h in hs]
        out["out"] = [avg([d.get("out") for d in buckets[h]]) for h in hs]
        out["on"] = [avg([d.get("on") for d in buckets[h]]) for h in hs]
        out["fail"] = [avg([d.get("fail") for d in buckets[h]]) for h in hs]
        per = [[bits(d) for d in buckets[h]] for h in hs]
        for i in range(n):
            out["fans"][i]["data"] = [avg([b[i] for b in hour], 2) for hour in per]
    else:
        out["res"] = "every read"
        out["t"] = [_ms(ts) for ts, _ in samples]
        out["out"] = [(d or {}).get("out") for _, d in samples]
        out["on"] = [(d or {}).get("on") for _, d in samples]
        out["fail"] = [(d or {}).get("fail") for _, d in samples]
        per = [bits(d) for _, d in samples]
        for i in range(n):
            out["fans"][i]["data"] = [b[i] for b in per]
    if not samples:
        out["note"] = "No history in this range yet — it is recorded from the first poll after the fans page was installed."
    return out


def series(site, days, now=None) -> dict:
    """series_from() for one BloomFanSite (app context assumed)."""
    from app.models import BloomFanSample
    now = now or _dt.datetime.utcnow()
    rows = (BloomFanSample.query
            .filter(BloomFanSample.site_id == site.id, BloomFanSample.ts >= now - _dt.timedelta(days=days))
            .order_by(BloomFanSample.ts).all())
    s = summary(site.data, now=now)
    fans = [f for g in s["groups"] for f in g["fans"]]
    return series_from([(r.ts, r.data) for r in rows], fans, s["cut"]["sp"] if s["cut"] else None, days, now)


# ── poller hook (called from bloom.poll_account, app context assumed) ──────────

def record(app, account, building_list, now) -> int:
    """Upsert a BloomFanSite for every building in this account's feed that has
    fans, and append a throttled history sample. Returns the number of sites."""
    from sqlalchemy import func
    from app.extensions import db
    from app.models import BloomFanSample, BloomFanSite
    from app import bloom

    have = {s.asset_key: s for s in BloomFanSite.query.filter_by(account_id=account.id).all()}
    touched = []
    for b in building_list:
        if not has_fans(b):
            continue
        key = bloom.asset_key(b)
        if key is None:
            continue
        key = str(key)
        site = have.get(key) or BloomFanSite(account_id=account.id, asset_key=key)
        site.name = (b.get("name") or key)[:200]
        site.data = trim(b)
        site.polled_at = now
        db.session.add(site)
        touched.append(site)
    db.session.commit()

    every = app.config.get("BLOOM_SAMPLE_MIN_SECONDS", 300)
    ids = [s.id for s in touched]
    if ids:
        last = dict(db.session.query(BloomFanSample.site_id, func.max(BloomFanSample.ts))
                    .filter(BloomFanSample.site_id.in_(ids)).group_by(BloomFanSample.site_id).all())
        added = 0
        for site in touched:
            lt = last.get(site.id)
            if lt is not None and (now - lt).total_seconds() < every:
                continue
            summ = summary(site.data, now=now)
            if summ["stale"]:
                continue                      # nothing new was heard; don't record a flat line
            db.session.add(BloomFanSample(site_id=site.id, ts=now, data=history_sample(summ)))
            added += 1
        if added:
            db.session.commit()
    return len(touched)
