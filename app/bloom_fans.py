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
import hashlib
import json
import logging
import re

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


def until(bits, hour):
    """(on this hour, clock time of the schedule's next change — None if it never changes)."""
    on_now = bool(bits[hour % 24])
    for k in range(1, 25):
        if bool(bits[(hour + k) % 24]) != on_now:
            return on_now, _clock(hour + k)
    return on_now, None


# ── the plan: how the fans SHOULD run ──────────────────────────────────────────
#
# The operator's rule (2026-11-16, as he tightened it the same day), by outdoor
# temperature in whole degrees. The morning start was 8am until that night,
# when he moved it to 7am in both schedules after watching Berry stop at 10pm:
#   35° and below   off, except 7-10am and 6-8pm
#   36° to 55°      on 7am-12pm and 5-10pm, off otherwise
#   56° and above   on around the clock
# So the lines are at 36 and 56 (FAN_PLAN_MILD_FROM_F / FAN_PLAN_FREE_FROM_F):
# a reading of 35.9 is still "35 and below".
#
# Hysteresis (FAN_PLAN_HYST_F, 2°): he wants the schedule not to jump in and
# out when the temperature sits on a line. A building drops to the colder
# schedule the moment it crosses the line going down, and moves back up only
# once it is HYST above it — 38° to leave the cold schedule, 58° to run free.
# The lean is deliberate: the colder schedule is the one that saves heat, so it
# is quick to start and slow to end. That needs memory, so the poller keeps
# each building's band beside its raw slice (data["portal"]["band"], step_band).
#
# This is the yardstick the page measures against. It is NOT what Bloom is set
# to do — on the day this was written no building had a middle schedule at all —
# so the page also says where Bloom's own schedules differ from it (plan_gaps).

def _hours(*spans):
    bits = [0] * 24
    for a, b in spans:
        for h in range(a, b):
            bits[h % 24] = 1
    return bits


def plan(mild_from=36.0, free_from=56.0) -> list:
    """The bands, coldest first. Bounds are lo <= outdoor < hi."""
    def band(key, lo, hi, bits, label, short):
        return {"key": key, "lo": lo, "hi": hi, "bits": bits, "label": label, "short": short,
                "hours_on": sum(bits), "ranges": ranges(bits)}
    return [band("cold", None, mild_from, _hours((7, 10), (18, 20)),
                 f"{mild_from - 1:.0f}° and below", f"≤{mild_from - 1:.0f}°"),
            band("mild", mild_from, free_from, _hours((7, 12), (17, 22)),
                 f"{mild_from:.0f}° to {free_from - 1:.0f}°", f"{mild_from:.0f}–{free_from - 1:.0f}°"),
            band("free", free_from, None, [1] * 24, f"{free_from:.0f}° and above", f"{free_from:.0f}°+")]


def band_of(pl, outdoor):
    """The band the temperature falls in, with no memory."""
    for b in pl:
        if (b["lo"] is None or outdoor >= b["lo"]) and (b["hi"] is None or outdoor < b["hi"]):
            return b
    return None


def step_band(pl, prev_key, outdoor, hyst=0.0):
    """The band a building is in now, given the band it was in (its key, or
    None) — the hysteresis. Going colder happens at the line; going warmer
    needs `hyst` degrees above it. Returns the band, or None with no reading."""
    if outdoor is None:
        return None
    nominal = band_of(pl, outdoor)
    keys = [b["key"] for b in pl]
    if nominal is None or prev_key not in keys:
        return nominal
    i, n = keys.index(prev_key), keys.index(nominal["key"])
    if n <= i:
        return nominal                                   # same or colder: follow the temperature
    while i < n and outdoor >= pl[i + 1]["lo"] + hyst:   # warmer: one line at a time, each with its margin
        i += 1
    return pl[i]


def shared_bands(items, pl, hyst=0.0) -> dict:
    """One outdoor temperature and one plan band per property.

    The buildings' own outdoor sensors disagree — a sensor in the sun read 71°
    while its neighbours read 53° — so placing each building by its own reading
    put next-door buildings on different schedules. Instead every building of a
    property is placed by the MEDIAN of that property's live readings (the
    median shrugs off one or two bad sensors), which also gives a temperature
    to the buildings whose own sensor is dead.

    items: [{"key", "group", "out", "prev"}] — out is the building's own live
    reading or None, prev its remembered band; a building with no property is
    its own group. Returns {key: {"band", "out", "n"}}: the group's band key
    (None when no sensor in the group is live), the shared temperature, and how
    many sensors it came from."""
    groups = {}
    for it in items:
        groups.setdefault(it["group"], []).append(it)
    out = {}
    for members in groups.values():
        temps = sorted(m["out"] for m in members if m["out"] is not None)
        shared = None
        if temps:
            mid = len(temps) // 2
            shared = temps[mid] if len(temps) % 2 else (temps[mid - 1] + temps[mid]) / 2.0
        prevs = [m["prev"] for m in members if m["prev"]]
        prev = max(set(prevs), key=prevs.count) if prevs else None      # the band most of them remember
        band = step_band(pl, prev, shared, hyst)
        for m in members:
            out[m["key"]] = {"band": band["key"] if band else None, "out": shared, "n": len(temps)}
    return out


def expect(pl, outdoor, hour, minute=None, prev_key=None, hyst=0.0):
    """What the plan asks for right now: {band, on, until, settling, held,
    release}. `held` is true while hysteresis keeps the building on a colder
    schedule than the bare temperature would give; `release` is the reading at
    which it moves up. `settling` covers the first ten minutes after the plan
    changes state, when a building has not had time to follow yet."""
    b = step_band(pl, prev_key, outdoor, hyst)
    if b is None:
        return None
    nominal = band_of(pl, outdoor)
    held = nominal is not None and nominal["key"] != b["key"]
    on_now, nxt = until(b["bits"], hour)
    changed = bool(b["bits"][(hour - 1) % 24]) != on_now
    return {"band": b, "on": on_now, "until": nxt, "held": held,
            "release": (b["hi"] + hyst) if held else None,
            "settling": bool(changed and minute is not None and minute < 10)}


def plan_gaps(pl, groups, scheds) -> list:
    """Where Bloom's own schedules would not carry out the plan, as sentences.
    A band is covered when a schedule the groups follow is armed at the band's
    upper temperature with the same hours."""
    used = {g["sched"] for g in groups if g["sched"] in scheds}
    armed = [scheds[n] for n in sorted(used) if scheds[n]["sp_on"] and scheds[n]["sp"] is not None]

    # The portal's way of carrying the plan out: slots 3, 4 and 5 hold the
    # plan's hours with no outdoor gate, every group follows one of them, and
    # the portal moves the groups between them as the temperature changes.
    def holds(b):
        s = scheds.get(PLAN_SLOTS[b["key"]])
        return bool(s) and s["bits"] == b["bits"] and not (s["sp_on"] and s["sp"] is not None)
    if used and used <= set(PLAN_SLOTS.values()) and all(holds(b) for b in pl):
        return []
    # Same arrangement, but the plan has been changed since the slots were
    # loaded: say which slot is behind, not that Bloom has no such schedule.
    plan_scheds = [scheds.get(n) for n in PLAN_SLOTS.values()]
    if used and used <= set(PLAN_SLOTS.values()) and all(
            s and not (s["sp_on"] and s["sp"] is not None) for s in plan_scheds):
        return [f"Schedule {PLAN_SLOTS[b['key']]} ({b['label'].lower()}) holds {scheds[PLAN_SLOTS[b['key']]]['ranges'] or 'no hours'}; "
                f"the plan is now {b['ranges']} — send the schedules again."
                for b in pl if not holds(b)]
    out, matched = [], set()
    for b in pl:
        if b["hi"] is None:
            continue
        # Bloom words a setpoint as "active when outdoor falls below N"; the
        # plan's "35 and below" is a line at 36. Either number is the same
        # schedule for this purpose, so match within a degree.
        hit = next((s for s in armed if abs(s["sp"] - b["hi"]) <= 1.0), None)
        if hit is None:
            out.append(f"No schedule in Bloom for {b['label'].lower()} — the plan is {b['ranges']}.")
            continue
        matched.add(hit["n"])
        if hit["bits"] != b["bits"]:
            out.append(f"{b['label']}: Bloom runs the fans {hit['ranges']}; the plan is {b['ranges']}.")
    for s in armed:
        if s["n"] not in matched:
            out.append(f"Bloom has a schedule below {s['sp']:.0f}° ({s['ranges']}) that is not in the plan.")
    for n in sorted(used):                           # a schedule with no outdoor gate holds at every temperature
        s = scheds[n]
        if not (s["sp_on"] and s["sp"] is not None):
            out.append(f"Fan groups follow schedule {n} at every temperature ({s['ranges'] or 'never on'}) — "
                       f"the plan changes with the temperature.")
    return out


def _short(name, i):
    """'Exhaust Fan 10' -> '10': the number a fan or group is known by."""
    tail = str(name or "").rsplit(" ", 1)[-1]
    return tail if tail.isdigit() else str(i + 1)


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

TONE_ORDER = {"over": 0, "cold": 0, "blind": 1, "fault": 2, "under": 3, "dark": 4, "off": 5, "ok": 6}


def summary(raw: dict, now=None, cold_f=35.0, hour=None, minute=None, pl=None, hyst=0.0) -> dict:
    """Everything the Fans page shows for one building, derived at read time
    from the stored slice. `now` is naive UTC; `hour` / `minute` are local time
    (they mark the current column of each schedule and place the building in
    the plan); `pl` is plan() — without it nothing is judged against a plan —
    and `hyst` its hysteresis in degrees."""
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
        fail = (not on) and (cmd is True or (cmd is None and flag))
        # Running while its group is commanded OFF: the fan is not obeying the
        # controller — in practice its switch at the fan is in hand. First
        # seen 2026-11-16 at 1760 Park pl, the first time any group was ever
        # commanded off; no schedule will stop such a fan.
        hand = on and cmd is False
        return {"key": key, "name": names.get(key) or key, "on": on, "hand": hand,
                "num": _short(names.get(key) or str(key).replace("-", " "), 0),
                "state": "hand" if hand else ("on" if on else ("down" if fail else "off")),
                "fail": fail, "flagged": on and flag,
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
                       "short": "G" + _short(e.get("name") or str(e.get("key")).replace("-", " "), len(groups)),
                       "sched": assign.get(str(e.get("key"))),
                       "fans": [fan(k, cmd) for k in members]})
    loose = [k for k in fan_keys if k not in seen]
    if loose:
        groups.append({"key": None, "name": "Not in a group", "short": "—", "cmd": None, "sched": None,
                       "fans": [fan(k, None) for k in loose]})

    fans = [f for g in groups for f in g["fans"]]
    total, on = len(fans), sum(f["on"] for f in fans)
    fail = sum(f["fail"] for f in fans)
    hand_n = sum(f["hand"] for f in fans)
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

    # Against the plan: fans running in an hour it says off (heat going up the
    # shaft — the thing this page exists to catch), or held off in an hour it
    # says on. Judged only on a live outdoor reading, in a band the plan covers.
    # The temperature the plan is read at: the property's shared reading when
    # the poller has worked one out (shared_bands), else this building's own.
    portal = (raw or {}).get("portal") or {}
    held_band = portal.get("band")                                   # kept by the poller: the hysteresis memory
    shared = _num(portal.get("out")) if "out" in portal else None
    plan_out = shared if shared is not None else outdoor
    want = (expect(pl, plan_out, hour, minute, prev_key=held_band, hyst=hyst)
            if (pl and plan_out is not None and hour is not None) else None)
    over = under = 0
    if want and not want["settling"] and not stale:
        if want["on"] is False:
            over = on
        else:
            # held off = not running in a group commanded off (one running anyway is "ignoring stop")
            under = sum(1 for g in groups if g["cmd"] is False for f in g["fans"] if not f["on"])

    flags = []
    if stale:
        tone, status = "dark", f"Not reporting — last heard {age_words(heard_age)} ago"
    else:
        till = f" until {want['until']}" if want and want["until"] else ""
        if over:
            flags.append(("over", f"{over} fan{'s' if over != 1 else ''} running — plan says off{till} "
                                  f"({plan_out:.0f}° outside)"))
        if under:
            flags.append(("under", f"{under} fan{'s' if under != 1 else ''} held off — plan says on{till} "
                                   f"({plan_out:.0f}° outside)"))
        if cold and on and not pl:
            said = ""
            if cut_active and hour is not None:       # what the cold schedule itself says for this hour
                said = " — scheduled on this hour" if cut["bits"][hour % 24] else " — schedule says off"
            flags.append(("cold", f"{on} fan{'s' if on != 1 else ''} running at {outdoor:.0f}° outside{said}"))
        if hand_n:
            flags.append(("fault", f"{hand_n} fan{'s' if hand_n != 1 else ''} running while commanded off — "
                                   f"check {'its' if hand_n == 1 else 'their'} switch at the fan"))
        if outdoor is None and armed and any(g["sched"] == cut["n"] for g in groups):
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
    for s in scheds.values():                       # which groups follow each schedule
        s["used_by"] = [g["short"] for g in groups if g["sched"] == s["n"]]

    # One plain sentence on where the cold schedule stands right now.
    sched_now = None
    if cut and hour is not None:
        on_now, nxt = until(cut["bits"], hour)
        sched_now = {"on": on_now, "until": nxt}
    # Groups that have fans, and the schedules they follow that have NO outdoor
    # gate. Such a schedule's hours hold at every temperature — proved at 1760
    # Park pl on 2026-11-16 — which is how the plan's slots 3-5 work.
    real = [g for g in groups if g["key"] and g["fans"]] or [g for g in groups if g["key"]]

    def gated(s):
        return bool(s["sp_on"] and s["sp"] is not None)
    always = sorted({g["sched"] for g in real if g["sched"] in scheds and not gated(scheds[g["sched"]])})
    if not cut and len(always) == 1:
        s1 = scheds[always[0]]
        banner = ("active", f"The fans follow schedule {s1['n']} at every temperature: {s1['ranges'] or 'never on'}.")
    elif not cut and always:
        banner = ("active", f"The fan groups follow schedules {' and '.join(str(n) for n in always)}, "
                            f"each at every temperature.")
    elif not cut:
        banner = ("none", "No cold schedule is assigned to these fans.")
    elif outdoor is None:
        banner = ("blind", f"No live outdoor reading, so the cold schedule (below {cut['sp']:.0f}°) cannot engage.")
    elif cut_active:
        what = "ON" if sched_now and sched_now["on"] else "OFF"
        till = f" until {sched_now['until']}" if sched_now and sched_now["until"] else ""
        banner = ("active", f"{outdoor:.0f}° outside, below {cut['sp']:.0f}° — cold schedule ACTIVE: "
                            f"fans scheduled {what}{till}.")
    else:
        banner = ("standby", f"{outdoor:.0f}° outside, above {cut['sp']:.0f}° — cold schedule on standby. "
                             f"Below {cut['sp']:.0f}° it runs the fans {cut['ranges']} ({cut['hours_on']} h a day).")

    # What Bloom itself is set to do today, as one strip beside the plan's: its
    # cold schedule's hours while that is active, otherwise every hour — above
    # its setpoint (or with no outdoor reading of its own) Bloom has been seen
    # running the fans around the clock.
    # Worked out per fan group from the schedule it follows: a gated schedule's
    # hours hold only below its setpoint, an un-gated one's at every temperature.
    ALL_DAY = "Bloom has no schedule in force at this temperature, so the fans run all day"

    def set_for(g):
        s = scheds.get(g["sched"])
        if s is None:
            return [1] * 24, False, ALL_DAY
        if not gated(s):
            return s["bits"], True, f"Bloom's schedule {s['n']} applies at every temperature: {s['ranges'] or 'never on'}"
        if outdoor is None:
            return [1] * 24, False, "Bloom has no outdoor reading here, so its schedule cannot engage and the fans run all day"
        if outdoor < s["sp"]:
            return s["bits"], True, f"Bloom's schedule {s['n']} is active (below {s['sp']:.0f}°): {s['ranges']}"
        return [1] * 24, False, ALL_DAY

    sets = [(g, *set_for(g)) for g in real]
    if not sets:
        bloom_now = {"bits": [1] * 24, "active": False, "why": ALL_DAY}
    else:
        whys = list(dict.fromkeys(w for *_, w in sets))
        bloom_now = {"bits": [1 if any(b[h] for _, b, _, _ in sets) else 0 for h in range(24)],
                     "active": any(a for _, _, a, _ in sets),
                     "why": whys[0] if len(whys) == 1 else "; ".join(f"{g['name']}: {w}" for g, _, _, w in sets)}
    # On the plan's slots but not the one for this temperature: say which it should be.
    slots_now = sorted({g["sched"] for g in real if g["sched"]})
    slot_want = PLAN_SLOTS[want["band"]["key"]] if want else None
    slot_off = bool(slot_want and always and set(slots_now) <= set(PLAN_SLOTS.values()) and slots_now != [slot_want])
    if slot_off:
        bloom_now["why"] += f" — the plan at {plan_out:.0f}° is schedule {slot_want}"

    return {
        "plan_out": plan_out, "plan_shared": shared is not None, "plan_n": portal.get("n"),
        "out_dev": (outdoor - plan_out) if (shared is not None and outdoor is not None) else None,
        "bloom_now": bloom_now, "hand": hand_n,
        "slots_now": slots_now, "slot_want": slot_want, "slot_off": slot_off,
        "want": want, "over": over, "under": under,
        "gaps": plan_gaps(pl, groups, scheds) if pl else [],
        "banner": banner[1], "banner_tone": banner[0], "sched_now": sched_now,
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


# ── which property a fan building belongs to ───────────────────────────────────
#
# The portal already files these buildings under Client -> Property -> Building
# (they are 2010 boxes too), under the same names Bloom uses. So a fan site is
# placed by name at read time — no link column to keep in step. The five
# fan-only Bloom entries ("590 Gates Ave Exhaust Fans") have no building of
# their own; they land on the building whose address range holds their number
# ("590-600 Gates Ave").

def norm(s) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(s or "").lower()).split())


def building_index(pairs) -> dict:
    """pairs = [(building name, value)] -> a lookup for place()."""
    exact, spans = {}, []
    for name, value in pairs:
        n = norm(name)
        exact.setdefault(n, value)
        m = re.match(r"^(\d+) (\d+) (.+)$", n)                 # "590-600 Gates Ave"
        if m and int(m.group(1)) <= int(m.group(2)):
            spans.append((int(m.group(1)), int(m.group(2)), m.group(3), value))
    return {"exact": exact, "spans": spans}


def place(index, name, key=None):
    """The value filed for this fan site's building, or None."""
    for cand in (name, key):
        if cand and norm(cand) in index["exact"]:
            return index["exact"][norm(cand)]
    n = re.sub(r"\s*exhaust fans?$", "", norm(name))
    if n in index["exact"]:
        return index["exact"][n]
    m = re.match(r"^(\d+) (.+)$", n)
    if m:
        num, street = int(m.group(1)), m.group(2)
        for lo, hi, st, value in index["spans"]:
            if st == street and lo <= num <= hi:
                return value
    return None


def _natural(s):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", str(s or "").lower())]


UNFILED = "Not filed under a property"


def page(rows, show="all", q="", sort="property") -> dict:
    """Filter, group and total the Fans page. `rows` are summary() dicts that
    also carry `prop` / `client` (None when unfiled). Returns {groups, summary,
    show, sort}; a group is one property (or the whole list when sorting by
    status or name) with its own running / down / off / not-reporting totals."""
    def blind(r):
        return any("cannot engage" in f for f in r["flags"])

    def totals(rs):
        live = [r for r in rs if not r["stale"]]
        on, down = sum(r["on"] for r in live), sum(r["fail"] for r in live)
        fans = sum(r["total"] for r in live)
        outs = [r["outdoor"] for r in live if r["outdoor"] is not None]
        return {"buildings": len(rs), "fans": fans, "on": on, "down": down, "off": fans - on - down,
                "dark_fans": sum(r["total"] for r in rs if r["stale"]),
                "all_fans": sum(r["total"] for r in rs),
                "cold": sum(r["tone"] == "cold" for r in rs), "blind": sum(blind(r) for r in rs),
                "over": sum(1 for r in live if r.get("over")), "over_fans": sum(r.get("over") or 0 for r in live),
                "under": sum(1 for r in live if r.get("under")), "under_fans": sum(r.get("under") or 0 for r in live),
                "gaps": sum(1 for r in rs if r.get("gaps")),
                "hand": sum(1 for r in live if r.get("hand")), "hand_fans": sum(r.get("hand") or 0 for r in live),
                "bands": {k: sum(1 for r in live if r.get("want") and r["want"]["band"]["key"] == k)
                          for k in ("cold", "mild", "free")},
                "held": sum(1 for r in live if r.get("want") and r["want"]["held"]),
                "fault": sum(1 for r in live if r["fail"]), "dark": sum(r["stale"] for r in rs),
                "out_min": min(outs) if outs else None, "out_max": max(outs) if outs else None}

    summary = totals(rows)
    in_band = {}                                   # which properties sit in each plan band right now
    for r in rows:
        if r.get("plan_shared") and r.get("want") and r.get("prop"):
            in_band.setdefault(r["want"]["band"]["key"], set()).add(r["prop"])
    summary["band_props"] = {k: sorted(v, key=_natural) for k, v in in_band.items()}
    keep = {"cold": lambda r: r["tone"] == "cold", "blind": blind,
            "over": lambda r: not r["stale"] and r.get("over"), "under": lambda r: not r["stale"] and r.get("under"),
            "gaps": lambda r: bool(r.get("gaps")),
            "hand": lambda r: not r["stale"] and r.get("hand"),
            "fault": lambda r: not r["stale"] and r["fail"], "dark": lambda r: r["stale"],
            "running": lambda r: not r["stale"] and r["on"]}.get(show)
    if keep is None:
        show = "all"
    else:
        rows = [r for r in rows if keep(r)]
    q = (q or "").strip().lower()
    if q:
        rows = [r for r in rows if q in r["name"].lower() or q in (r.get("prop") or "").lower()]

    if sort == "status":
        rows = sorted(rows, key=lambda r: (TONE_ORDER.get(r["tone"], 9), _natural(r["name"])))
        groups = [{"name": None, "client": None, "rows": rows, **totals(rows)}] if rows else []
    else:
        sort = "property"
        by = {}
        for r in rows:
            by.setdefault(r.get("prop") or UNFILED, []).append(r)
        groups = []
        for name in sorted(by, key=lambda n: (n == UNFILED, _natural(n))):
            rs = sorted(by[name], key=lambda r: _natural(r["name"]))
            # A property has one temperature and so one place in the plan; take
            # it from any building that carries the shared reading.
            lead = next((r for r in rs if r.get("plan_shared") and r.get("want")), None) if name != UNFILED else None
            groups.append({"name": name, "client": rs[0].get("client"), "rows": rs, **totals(rs),
                           "plan_out": lead["plan_out"] if lead else None,
                           "plan_n": lead["plan_n"] if lead else None,
                           "want": lead["want"] if lead else None})
    return {"groups": groups, "summary": summary, "show": show, "sort": sort, "shown": len(rows)}


# ── pushing the plan's schedules into Bloom ────────────────────────────────────
#
# Stage 1 of putting the plan into the boxes (the operator, 2026-11-16): load
# the plan's three schedules into a building's Bloom schedule slots, after a
# person has seen exactly what will change and confirmed it.
#
# Bloom gives a building five schedule slots; a fan group follows one slot. The
# groups all sit on slot 1 today. The plan's schedules go into slots 3, 4 and 5
# — cold hours, middle hours, all day — which no group follows, so loading them
# changes nothing about how the fans run. Slots 1 and 2 and every group's
# assignment are left exactly as they are; putting a group ON one of the new
# slots is a later, separate step. A slot a group does follow is refused.
#
# The one exception (`revise`) is for changing the plan after the groups have
# been moved onto its slots: a followed slot's HOURS may then be rewritten,
# which changes how those fans run at once and is flagged `live` so the page
# can say so. The caller passes revise only for a building the portal itself
# loaded before (portal_loaded), and a followed slot behind an outdoor gate is
# still refused — the portal's own slots never have one.
#
# Each slot is written as its 24 hourly points (gen-sched "S::H") and, if it is
# on, its outdoor-setpoint switch (gen-sp-enabled "oatSp::S") is turned off so
# the hours are not gated by Bloom's own outdoor reading.

PLAN_SLOTS = {"cold": 3, "mild": 4, "free": 5}


def portal_loaded(writes) -> set:
    """The site ids whose schedule slots the portal has loaded before: an
    accepted write that set schedule hours. Those are the buildings where a
    followed slot's hours may be revised (plan_push revise=True)."""
    return {w.site_id for w in writes
            if w.ok and any((c or {}).get("type") == "gen-sched" for c in (w.changes or []))}


def plan_push(building: dict, pl, revise=False) -> dict:
    """What loading the plan into this building's slots would change, from the
    LIVE building object out of /api/assets (the full one: its points carry the
    ids Bloom needs). Pure. Returns:

      slots   one per plan band: slot number, the hours there now and after,
              which hours change, which groups follow it, and `blocked` (with
              the reason) when the slot must not be written, and `live` when
              fan groups follow the slot and its hours are being revised
              (revise=True) — the one case where a load changes how fans run
      points  the exact point objects to send — Bloom's own object with `pv`
              new and `originalPv` old — empty when nothing needs changing or
              any slot is blocked
      digest  a fingerprint of `points`, so the confirmed change can be checked
              against the previewed one
    """
    sensors = (building or {}).get("sensors") or []
    by = {}
    for s in sensors:
        by.setdefault((s.get("type"), str(s.get("name"))), s)
    assign = {}
    for s in sensors:
        if s.get("type") == "gen-sched-assign":
            try:
                assign.setdefault(int(s.get("pv")), []).append(str(s.get("name")))
            except (TypeError, ValueError):
                pass

    slots, points = [], []
    for band in pl:
        n = PLAN_SLOTS[band["key"]]
        hour_pts = [by.get(("gen-sched", f"{n}::{h}")) for h in range(24)]
        missing = [h for h, p in enumerate(hour_pts) if p is None or not p.get("_id")]
        now = [(1 if _on(p.get("pv")) else 0) if p else None for p in hour_pts]
        enabled = by.get(("gen-sp-enabled", f"oatSp::{n}"))
        used_by = sorted(assign.get(n, []), key=_natural)
        changes = [h for h in range(24) if now[h] is not None and now[h] != band["bits"][h]]
        gate_off = bool(enabled and _on(enabled.get("pv")))
        # A slot fans are running on is only refused when loading it would
        # CHANGE it; one that already holds the plan is simply left alone.
        why, live = None, False
        if missing:
            why = f"Bloom's feed has no point for {len(missing)} of this slot's 24 hours"
        elif used_by and changes and revise and not gate_off:
            live = True                              # the portal's own slot: new hours under the fans
        elif used_by and (changes or gate_off):
            why = (f"fan group{'s' if len(used_by) != 1 else ''} {', '.join(used_by)} "
                   f"follow{'s' if len(used_by) == 1 else ''} this slot")
        slot = {"n": n, "key": band["key"], "label": band["label"], "now": now, "new": band["bits"],
                "holds": not missing and not changes and not gate_off,
                "now_ranges": ranges([b or 0 for b in now]), "new_ranges": band["ranges"],
                "changes": changes, "gate_off": gate_off, "used_by": used_by, "blocked": why, "live": live,
                "sp_on": gate_off, "sp": _num((by.get(("oat-sp-gen", str(n))) or {}).get("pv"))}
        slots.append(slot)
        if why:
            continue
        for h in changes:
            points.append({**hour_pts[h], "pv": band["bits"][h], "originalPv": hour_pts[h].get("pv")})
        if gate_off:
            points.append({**enabled, "pv": 0, "originalPv": enabled.get("pv")})

    if any(s["blocked"] for s in slots):
        points = []                                  # all or nothing: never load half a plan
    digest = hashlib.sha256(json.dumps(
        sorted((str(p.get("_id")), p["pv"]) for p in points)).encode()).hexdigest()[:16] if points else ""
    return {"slots": slots, "points": points, "digest": digest,
            "n_changes": len(points), "blocked": any(s["blocked"] for s in slots),
            "live": [s["n"] for s in slots if s["live"]] if points else [],
            "log": [{"type": p.get("type"), "name": p.get("name"), "old": p.get("originalPv"), "new": p["pv"]}
                    for p in points]}


def plan_assign(building: dict, pl, target_key=None, slot=None) -> dict:
    """What pointing every fan group of this building at ONE schedule slot
    would change — the step that actually changes how the fans run. The target
    is the slot holding a plan band (`target_key`: "cold" / "mild" / "free") or
    an explicit `slot` number; slot 1 is where every group sat before the
    portal touched anything, so `slot=1` is the way back.

    From the LIVE building object, like plan_push. Refused (`blocked`) when the
    target is one of the plan's slots and that slot does not hold the plan's
    hours yet — a group is never pointed at a slot with the wrong hours in it.
    Returns rows (one per group: now -> to), the points to send, a digest."""
    sensors = (building or {}).get("sensors") or []
    points_by = {str(s.get("name")): s for s in sensors if s.get("type") == "gen-sched-assign"}
    target = int(slot) if slot is not None else PLAN_SLOTS[target_key]
    why = None
    if target in PLAN_SLOTS.values():
        held = {s["n"]: s for s in plan_push(building, pl)["slots"]}
        if not held[target]["holds"]:
            why = f"slot {target} does not hold the plan's hours yet — load the schedules first"
    rows, points = [], []
    for e in (building or {}).get("equipment") or []:
        if e.get("type") != "fanGroup":
            continue
        key = str(e.get("key"))
        pt = points_by.get(key)
        try:
            now = int(pt.get("pv")) if pt is not None else None
        except (TypeError, ValueError):
            now = None
        missing = pt is None or not pt.get("_id")
        rows.append({"key": key, "name": e.get("name") or key, "now": now, "to": target,
                     "change": not missing and now != target, "missing": missing})
        if not missing and now != target:
            points.append({**pt, "pv": target, "originalPv": pt.get("pv")})
    if why:
        points = []
    digest = hashlib.sha256(json.dumps(
        sorted((str(p.get("_id")), p["pv"]) for p in points)).encode()).hexdigest()[:16] if points else ""
    return {"target": target, "rows": rows, "points": points, "digest": digest, "n_changes": len(points),
            "blocked": why,
            "log": [{"type": p.get("type"), "name": p.get("name"), "old": p.get("originalPv"), "new": p["pv"]}
                    for p in points]}


def combined_digest(parts) -> str:
    """One fingerprint for a change that spans several buildings."""
    parts = sorted(p for p in parts if p)
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16] if parts else ""


AUTO_BY = "automatic"
AUTO_RETRY_MIN = 10            # after an automatic write to a building, wait this long before another


def auto_plan(building, stored, pl, on_props, recent, now) -> tuple:
    """What automatic switching does with ONE building this poll, as
    (verdict, assignment). Pure: `building` is the live object from Bloom,
    `stored` the site's stored slice (whose `portal` holds the property and
    its band), `on_props` the properties switched on, `recent` whether an
    automatic write went to this building in the last AUTO_RETRY_MIN.

      (None, None)    not this building's business: its property is not
                      switched on, or there is no live shared temperature
      ("inplace", a)  every group already follows the band's slot
      ("wait", a)     groups need moving but not now: the slots do not hold
                      the plan, the building is not reporting, or it was
                      written to a moment ago
      ("send", a)     send a["points"]
    """
    portal = (stored or {}).get("portal") or {}
    prop, band = portal.get("prop"), portal.get("band")
    if not prop or prop not in on_props or band not in PLAN_SLOTS:
        return None, None
    a = plan_assign(building, pl, target_key=band)
    if a["blocked"]:
        return "wait", a
    if not a["n_changes"]:
        return "inplace", a
    if recent or summary(stored, now=now)["stale"]:
        return "wait", a
    return "send", a


def auto_switch(app, account, session, token, building_list, now) -> int:
    """The portal as supervisor: for every property whose automatic switching
    is ON, point each building's fan groups at the slot for the property's
    current plan band (app context assumed; called from bloom.poll_account
    right after record(), so `data["portal"]` is this poll's).

    Deliberately narrow. It only ever changes which slot a group follows —
    never a schedule's hours. It acts only on a property a person has switched
    on, only from a live shared temperature (no band, no action), only on a
    building that is reporting and whose three slots hold the plan, and at most
    once per AUTO_RETRY_MIN per building, so a controller that does not take
    the change is not hammered. Every write is logged as "automatic"."""
    from app import bloom
    from app.extensions import db
    from app.models import BloomFanAuto, BloomFanSite, BloomFanWrite
    cfg = app.config
    if not cfg.get("FAN_AUTO_ENABLED", True):
        return 0
    on = {a.prop for a in BloomFanAuto.query.filter_by(enabled=True).all()}
    if not on:
        return 0
    pl = plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))
    sites = {s.asset_key: s for s in BloomFanSite.query.filter_by(account_id=account.id).all()}
    since = now - _dt.timedelta(minutes=AUTO_RETRY_MIN)
    recent = {}
    for w in (BloomFanWrite.query.filter(BloomFanWrite.by == AUTO_BY, BloomFanWrite.at >= since)
              .order_by(BloomFanWrite.at).all()):
        recent[w.site_id] = w
    sent = 0
    for b in building_list:
        site = sites.get(str(bloom.asset_key(b)))
        if site is None:
            continue
        portal = (site.data or {}).get("portal") or {}
        prop, band = portal.get("prop"), portal.get("band")
        last = recent.get(site.id)
        verdict, a = auto_plan(b, site.data, pl, on, last is not None, now)
        if verdict == "inplace" and last is not None and last.ok and last.landed is None:
            last.landed = True                           # the earlier automatic write is now in Bloom's feed
        if verdict != "send":
            continue
        label = next(x["label"] for x in pl if x["key"] == band)
        out = _num(portal.get("out"))
        at = f" at {out:.0f}°" if out is not None else ""
        note = f"Automatic: {prop}{at} → the '{label}' schedule (slot {a['target']})"
        row = BloomFanWrite(site_id=site.id, at=now, by=AUTO_BY, note=note, changes=a["log"], ok=False)
        try:
            data, token = bloom.put_points(account, session, token, site.asset_key, a["points"], note)
            row.ok, row.detail = reply_summary(data)
        except Exception as e:
            row.detail = str(e)[:500]
            log.warning("automatic fan switch failed for %s: %s", site.name, e)
        db.session.add(row)
        sent += 1
    db.session.commit()
    return sent


def reply_summary(data) -> tuple:
    """(accepted, one line) from Bloom's reply to a write. The reply seen on the
    first portal push (2026-11-16): {"success": true, "data": {"result": "The
    change has been sent to the controller.", "requestId": ..., "results":
    [{"pointId", "requestedValue", "status": "acknowledged", ...}, ...]}}."""
    if not isinstance(data, dict):
        return True, str(data or "")[:300]
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    text = str(inner.get("result") or data.get("result") or data.get("message") or "").strip()
    results = inner.get("results") if isinstance(inner.get("results"), list) else []
    if results:
        acked = sum(1 for r in results if isinstance(r, dict) and r.get("status") == "acknowledged")
        text = f"{text} {acked} of {len(results)} points acknowledged.".strip()
    accepted = data.get("success") is not False
    return accepted, (text or ("accepted" if accepted else "Bloom reported failure"))[:300]


def write_summary(changes) -> str:
    """A logged change in a few words: hours loaded into slots, outdoor gates
    turned off, fan groups moved between schedules."""
    ch = changes or []
    hours = [c for c in ch if c.get("type") == "gen-sched"]
    gates = [c for c in ch if c.get("type") == "gen-sp-enabled"]
    moves = [c for c in ch if c.get("type") == "gen-sched-assign"]
    other = len(ch) - len(hours) - len(gates) - len(moves)
    parts = []
    if hours:
        slots = sorted({str(c.get("name", "")).split("::")[0] for c in hours})
        parts.append(f"{len(hours)} hour{'s' if len(hours) != 1 else ''} in slot{'s' if len(slots) != 1 else ''} {', '.join(slots)}")
    if gates:
        slots = sorted({str(c.get("name", "")).split("::")[-1] for c in gates})
        parts.append(f"outdoor gate off on slot{'s' if len(slots) != 1 else ''} {', '.join(slots)}")
    if moves:
        parts.append(", ".join(
            f"{re.sub(r'^fg-', 'group ', str(c.get('name')))}: schedule {c.get('old')} → {c.get('new')}"
            for c in sorted(moves, key=lambda c: _natural(c.get("name")))))
    if other:
        parts.append(f"{other} other")
    return " + ".join(parts)


def tidy_detail(detail) -> str:
    """A stored reply shown in the log: rows written before reply_summary()
    hold Bloom's whole reply as text — show just its sentence."""
    s = str(detail or "")
    m = re.search(r"'result': '([^']*)'", s)
    return m.group(1) if (s.startswith("{") and m) else s


def push_landed(building: dict, pl, revise=False) -> bool:
    """True when the building's slots now hold the plan (nothing left to push)."""
    p = plan_push(building, pl, revise=revise)
    return not p["blocked"] and p["n_changes"] == 0


# ── the audit view: Bloom's raw values beside what the page makes of them ──────

def raw_view(raw: dict, now=None, tz=None) -> dict:
    """Every stored Bloom point for one building, laid out so a person can check
    the Fans page against it: each fan's raw readings next to the state the page
    shows, each group's command and schedule number, every schedule's 24 bits,
    setpoints and enable flags, and every outdoor reading with the one in use
    marked. Values are Bloom's own, untouched; only the layout is ours."""
    now = now or _dt.datetime.utcnow()
    sensors = (raw or {}).get("sensors") or []
    s = summary(raw, now=now)

    def when(v):
        ts = _ts(v)
        if ts is None:
            return {"at": "—", "age": "never", "stale": True}
        local = ts.replace(tzinfo=_dt.timezone.utc).astimezone(tz) if tz else ts
        age = _age_min(ts, now)
        fmt = "%b %d, %I:%M:%S %p" if ts.year == now.year else "%b %d %Y, %I:%M:%S %p"   # name the year when it is not this one
        return {"at": local.strftime(fmt).replace(" 0", " "), "age": age_words(age),
                "stale": age is None or age > STALE_MIN}

    used = set()                                     # ids of the sensor dicts placed in a table

    def pick(type_, key=None, name=None):
        for x in sensors:
            if x.get("type") != type_ or id(x) in used:
                continue
            a = x.get("assoc") or {}
            if key is not None and not (a.get("type") == "equipment" and a.get("key") == key):
                continue
            if name is not None and str(x.get("name")) != name:
                continue
            used.add(id(x))
            return {"pv": x.get("pv"), **when(x.get("pollTs"))}
        return None

    fans, groups = [], []
    for g in s["groups"]:
        if g["key"]:
            groups.append({"key": g["key"], "name": g["name"], "members": [f["key"] for f in g["fans"]],
                           "cmd": pick("fan-cmd", key=g["key"]), "assign": pick("gen-sched-assign", name=g["key"]),
                           "shown": "—" if g["cmd"] is None else ("START" if g["cmd"] else "STOP")})
        for f in g["fans"]:
            fans.append({"key": f["key"], "name": f["name"], "group": g["name"],
                         "rs": pick("fan-rs", key=f["key"]), "fail": pick("fan-run-fail", key=f["key"]),
                         "manual": pick("fan-run-manual", key=f["key"]), "rt": pick("fan-rt-day", key=f["key"]),
                         "shown": "NO DATA" if s["stale"] else ("ON" if f["on"] else ("DOWN" if f["fail"] else "OFF")),
                         "shown_rt": None if f["rt_h"] is None else round(f["rt_h"], 2), "rt_bad": f["rt_bad"]})

    scheds = []
    for sc in s["schedules"]:
        n = str(sc["n"])
        for x in sensors:                            # the 24 hourly points are shown as one row of bits
            if x.get("type") == "gen-sched" and str(x.get("name") or "").startswith(n + "::"):
                used.add(id(x))
        scheds.append({"n": sc["n"], "bits": "".join(str(b) for b in sc["bits"]), "ranges": sc["ranges"],
                       "hours_on": sc["hours_on"], "used_by": sc["used_by"],
                       "sp": pick("oat-sp-gen", name=n), "enabled": pick("gen-sp-enabled", name=f"oatSp::{n}")})

    best = None                                      # the outdoor reading the page uses: the freshest
    for x in sensors:
        if x.get("type") == "oat-act" and _num(x.get("pv")) is not None:
            if best is None or (_ts(x.get("pollTs")) or _dt.datetime.min) > (_ts(best.get("pollTs")) or _dt.datetime.min):
                best = x
    outdoor = []
    for x in sensors:
        if str(x.get("type") or "").startswith("oat-") and id(x) not in used:
            used.add(id(x))
            w = when(x.get("pollTs"))
            outdoor.append({"type": x.get("type"), "name": x.get("name"), "pv": x.get("pv"), **w,
                            "used": x is best and not w["stale"], "freshest": x is best})

    other = [{"type": x.get("type"), "name": x.get("name"),
              "on": (x.get("assoc") or {}).get("key"), "pv": x.get("pv"), **when(x.get("pollTs"))}
             for x in sensors if id(x) not in used]
    return {"summary": s, "fans": fans, "groups": groups, "schedules": scheds, "outdoor": outdoor,
            "other": other, "n_sensors": len(sensors), "equipment": (raw or {}).get("equipment") or []}


# ── history for the chart ──────────────────────────────────────────────────────

def _ms(t):
    return int(t.replace(tzinfo=_dt.timezone.utc).timestamp() * 1000)


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
    cfg = app.config
    pl = plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))
    hyst = cfg.get("FAN_PLAN_HYST_F", 2.0)
    # Which property each building is filed under, for the shared temperature.
    from sqlalchemy.orm import joinedload
    from app.models import Building
    index = building_index((bd.name, bd.property.name)
                           for bd in Building.query.options(joinedload(Building.property)).all() if bd.property)
    touched, items = [], []
    for b in building_list:
        if not has_fans(b):
            continue
        key = bloom.asset_key(b)
        if key is None:
            continue
        key = str(key)
        site = have.get(key) or BloomFanSite(account_id=account.id, asset_key=key)
        site.name = (b.get("name") or key)[:200]
        slice_ = trim(b)
        out_v, out_age = _outdoor(slice_["sensors"], now)
        live_out = out_v if (out_v is not None and out_age is not None and out_age <= STALE_MIN) else None
        prev = ((site.data or {}).get("portal") or {}).get("band") if site.data else None
        prop = place(index, site.name, key)
        items.append({"key": key, "group": prop or ("site", key), "out": live_out, "prev": prev})
        touched.append((site, slice_, prop))
    # One temperature and one plan band per property, carried from poll to poll
    # so the hysteresis has something to hold on to (shared_bands).
    bands = shared_bands(items, pl, hyst)
    sites = []
    for site, slice_, prop in touched:
        slice_["portal"] = {**bands[site.asset_key], "prop": prop}
        site.data = slice_
        site.polled_at = now
        db.session.add(site)
        sites.append(site)
    touched = sites
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
            # (history_sample keeps on/total/fail/out/rs; the band is not charted yet)
            db.session.add(BloomFanSample(site_id=site.id, ts=now, data=history_sample(summ)))
            added += 1
        if added:
            db.session.commit()
    return len(touched)
