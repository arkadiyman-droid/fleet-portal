"""
app/abnormal.py — a 2010 box doing something its own settings cannot explain

The Live wall already flags what is plainly wrong (dark, alarms, a cold loop,
no heat overnight, bad sensors). This catches the quieter kind: a box running
on a value that does not come from its settings, or settings that make it heat
badly. Found 2026-11-26: the two 801 South St vacuum-steam boxes reported an
indoor target of 150 / 97 for weeks — their hourly tables said 70-71, and a
stray `vacuumSteam.dccCompensation` (79 / 26, outside the box's own -20..20)
was being added — so they never reached "Inside Satisfied" and overheated the
apartments. Nothing on the wall said so.

  checks(status, settings, now)   [{kind, text}] for one box, pure: its live
                                  status (LegacyStatus.data) and its stored
                                  settings (SettingSnapshot.data, flat
                                  "object.item" -> value, read hourly)
  overheating(box_ids, now)       {box_id: text}: apartments well over target
                                  for hours while the burner kept running
  settings_for(box_ids)           {box_id: {key: value}}, only the keys the
                                  checks read (a whole snapshot is ~440 keys)

The firmware's indoor target (PIC FC_HeatCtrl.c): the hour's value from the
weekday table — or the Saturday / Sunday table when useHolidayDITs says so —
lowered to the next hour's in the second half of the hour (look-ahead), then
with ditMode 1 moved by autoDITAdjHigh / autoDITAdjLow when it is warmer /
colder outside than autoDITTempHigh / autoDITTempLow. The box keeps time by
its own clock, so the hour either side is accepted too.
"""
import datetime as _dt
import threading
import time

DIT_SLACK = 1                  # degrees: the reported target may sit this far from every allowed value
DIT_LO, DIT_HI = 60, 80        # an hourly target outside this is a typo, not a choice
CUTOFF_LO, CUTOFF_HI = 40, 70  # outdoor cutoffs: heat locked out above this outside
CLOCK_MIN = 15                 # minutes the box's clock may be off before its schedule runs at the wrong hours
DCC_LO, DCC_HI = -20, 20       # the box's own range for vacuumSteam.dccCompensation
HOT_HOURS = 3                  # overheating: the window looked at
HOT_EXCESS = 5                 # ...apartments this far over target on average
HOT_MIN_EXCESS = 3             # ...and never closer than this
HOT_BURNER_MIN = 30            # ...while the burner ran at least this long
KNOWN_HCT = {1, 5, 7, 9, 11, 13, 15}

_DAYS = ("weekday", "saturday", "sunday")
KEYS = ([f"DITs.{d}DITHour{h}" for d in _DAYS for h in range(24)]
        + [f"emsSettings.{k}" for k in ("useHolidayDITs", "dayStartHour", "ditMode", "autoDITAdjHigh", "autoDITAdjLow",
                                         "autoDITTempHigh", "autoDITTempLow", "outsideDaySetpoint", "outsideNightSetpoint",
                                         "insideDaySetpoint", "insideNightSetpoint", "winterStartMonth", "winterStartDay",
                                         "summerStartMonth", "summerStartDay")]
        + ["emsConfig.enableDITLookAhead", "emsConfig.coolingModeSelected", "vacuumSteam.dccCompensation"])


def _int(v):
    try:
        f = float(str(v).strip())
        return int(f) if f == int(f) else None
    except (TypeError, ValueError):
        return None


def _local():
    from app import legacy
    return legacy.LOCAL


def _box_time(status):
    """The box's own clock, local time, or None."""
    try:
        t = _dt.datetime.fromisoformat(((status or {}).get("status") or {}).get("serverTime", "").replace("Z", "+00:00"))
        return t.astimezone(_local())
    except (ValueError, TypeError, AttributeError):
        return None


def _table(s, when, g):
    """Which hourly table the firmware uses at local time `when`."""
    use = g("emsSettings.useHolidayDITs") or 0
    dow = when.isoweekday() % 7 + 1                      # the firmware's 1 = Sunday .. 7 = Saturday
    if when.hour < (g("emsSettings.dayStartHour") or 0):
        dow = dow - 1 or 7                               # its days run start-hour to start-hour
    if (dow == 7 and use == 1) or use == 2:
        return "saturday"
    if (dow == 1 and use == 1) or use == 3:
        return "sunday"
    return "weekday"


def expected_targets(settings, times):
    """Every indoor target the firmware could be using at any of `times`
    (local datetimes), the hour either side included. Empty when the tables
    are not known."""
    s = settings or {}
    g = lambda k: _int(s.get(k))
    out = set()
    for t in times:
        for dh in (-1, 0, 1):
            tt = t + _dt.timedelta(hours=dh)
            v = g(f"DITs.{_table(s, tt, g)}DITHour{tt.hour}")
            if v is not None:
                out.add(v)
    if out and g("emsSettings.ditMode") == 1:
        hi, lo = g("emsSettings.autoDITAdjHigh") or 0, g("emsSettings.autoDITAdjLow") or 0
        out |= {v - hi for v in out} | {v + lo for v in out}
    return out


def checks(status, settings, now=None) -> list:
    """[{kind, text}] — what this box is doing that its settings do not
    explain, or settings that make it heat badly. `now` is naive UTC (the
    read time when omitted). Settings may be None (not read yet): only the
    status-only checks run then."""
    from app import legacy
    st = (status or {}).get("status") or {}
    s = settings or {}
    g = lambda k: _int(s.get(k))
    out = []

    def add(kind, text):
        out.append({"kind": kind, "text": text})

    if now is None:
        try:
            now = _dt.datetime.fromisoformat((status or {}).get("read_at", ""))
        except (TypeError, ValueError):
            now = _dt.datetime.utcnow()
    real = now.replace(tzinfo=_dt.timezone.utc).astimezone(_local())
    box = _box_time(status)
    hct = legacy.heat_code(status)

    if box is not None and abs((box - real).total_seconds()) >= CLOCK_MIN * 60:
        off = round((box - real).total_seconds() / 60)
        add("clock", f"The box's clock is {abs(off)} min {'fast' if off > 0 else 'slow'} "
                     f"({box:%I:%M %p} at the box, {real:%I:%M %p} really): its hourly schedule runs at the wrong times".replace(" 0", " "))
    if hct is not None and hct not in KNOWN_HCT:
        add("hct", f"Unknown heating-system type {hct}: the box may not be running the control it should")

    tit = _int(st.get("targetInsideTemperature"))
    cooling = str(s.get("emsConfig.coolingModeSelected", "")).lower() == "true"
    if s and tit is not None and not cooling:
        allowed = expected_targets(s, {real} | ({box} if box else set()))
        if allowed and min(abs(v - tit) for v in allowed) > DIT_SLACK:
            near = min(allowed, key=lambda v: abs(v - tit))
            why = ""
            dcc = g("vacuumSteam.dccCompensation")
            if hct == 5 and dcc and abs(tit - near - dcc) <= DIT_SLACK:
                why = f" It is adding the vacuum-steam DCC compensation ({dcc:+d}) to the target."
            add("dit", f"Indoor target is {tit}°, but the box's own hourly tables give {near}° now"
                       f" (allowed {min(allowed)}-{max(allowed)}°): it is heating to a target its settings do not set.{why}")
    if s:
        dcc = g("vacuumSteam.dccCompensation")
        if hct == 5 and dcc and not any(o["kind"] == "dit" for o in out):
            add("dcc", f"Vacuum-steam DCC compensation is {dcc:+d}"
                       + (f", outside the box's own {DCC_LO} to {DCC_HI}" if not DCC_LO <= dcc <= DCC_HI else "")
                       + ": it is added to the indoor target")
        use = g("emsSettings.useHolidayDITs") or 0
        tables = ["weekday"] + (["saturday", "sunday"] if use else [])
        if use == 2:
            tables = ["saturday"]
        elif use == 3:
            tables = ["sunday"]
        bad = [(t, h, g(f"DITs.{t}DITHour{h}")) for t in tables for h in range(24)
               if g(f"DITs.{t}DITHour{h}") is not None and not DIT_LO <= g(f"DITs.{t}DITHour{h}") <= DIT_HI]
        if bad:
            t, h, v = bad[0]
            add("dit_table", f"{len(bad)} hour{'s' if len(bad) != 1 else ''} of the indoor-target table "
                             f"{'are' if len(bad) != 1 else 'is'} outside {DIT_LO}-{DIT_HI}° "
                             f"(for example {t} {(h % 12) or 12} {'AM' if h < 12 else 'PM'}: {v}°)")
        if use in (2, 3):
            add("holiday", f"Every day uses the {'Saturday' if use == 2 else 'Sunday / holiday'} target table "
                           f"(useHolidayDITs {use}), not the weekday one")
        for k, label in (("outsideDaySetpoint", "day"), ("outsideNightSetpoint", "night")):
            v = g(f"emsSettings.{k}")
            if v is not None and not CUTOFF_LO <= v <= CUTOFF_HI:
                add("cutoff", f"Outdoor cutoff ({label}) is {v}°: heat is locked out whenever it is warmer than that outside")
        # the highest SENSIBLE hourly target (a typo in the table is flagged above, not here too)
        top = max([v for t in tables for h in range(24) for v in [g(f"DITs.{t}DITHour{h}")]
                   if v is not None and DIT_LO <= v <= DIT_HI] or [0])
        for k, label in (("insideDaySetpoint", "day"), ("insideNightSetpoint", "night")):
            v = g(f"emsSettings.{k}")
            if v is not None and top and v < top - 1:
                add("heatoff", f"Heat-off point ({label}) is {v}°, below the {top}° target: heat stops before the apartments get there")
        wm, wd, sm, sd = (g(f"emsSettings.{k}") for k in ("winterStartMonth", "winterStartDay", "summerStartMonth", "summerStartDay"))
        if wm and sm and 1 <= wm <= 12 and 1 <= sm <= 12:
            m, d, wd1, sd1 = real.month, real.day, wd or 1, sd or 1
            # the firmware's own test (FC_HeatCtrl.c): winter before the summer start or from the winter start
            winter = m < sm or (m == sm and d < sd1) or m > wm or (m == wm and d >= wd1)
            if not winter and real.month in (11, 12, 1, 2, 3):
                add("season", f"By its own calendar the box is in summer mode now (heating season {wm}/{wd or 1} to {sm}/{sd or 1}): it will not heat")
    if (st.get("callForHeat") or "").strip().startswith("Summer") and real.month in (11, 12, 1, 2, 3) and not any(o["kind"] == "season" for o in out):
        add("season", "The box says Summer: heating is off for the season")
    return out


SETTINGS_TTL_S = 300            # the snapshots are re-read hourly; the Live wall asks every minute
_cache, _cache_lock = {}, threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def settings_for(box_ids) -> dict:
    """{box_id: {key: value}} from the stored snapshots, only KEYS. Reused
    for SETTINGS_TTL_S (0.2 s on the Pi for the whole fleet)."""
    if not box_ids:
        return {}
    key, t = tuple(sorted(box_ids)), time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and t - hit[0] < SETTINGS_TTL_S:
            return {k: dict(v) for k, v in hit[1].items()}
    out = _settings_for(box_ids)
    with _cache_lock:
        if len(_cache) > 16:
            _cache.clear()
        _cache[key] = (t, out)
    return {k: dict(v) for k, v in out.items()}


def _settings_for(box_ids) -> dict:
    from sqlalchemy import text
    from app.extensions import db
    out = {}
    for sid, k, v in db.session.execute(text(
            "SELECT s.source_id, e.key, e.value FROM setting_snapshots s, jsonb_each_text(s.data) e "
            "WHERE s.source = 'legacy' AND s.source_id = ANY(:ids) AND e.key = ANY(:keys)"),
            {"ids": list(box_ids), "keys": KEYS}):
        out.setdefault(sid, {})[k] = v
    return out


def overheating(box_ids, now=None) -> dict:
    """{box_id: text} for boxes whose apartments stayed well over target for
    the last HOT_HOURS while the burner kept running — heating a building
    that is already warm. From the stored reads, one query."""
    from sqlalchemy import text
    from app.extensions import db
    if not box_ids:
        return {}
    now = now or _dt.datetime.utcnow()
    rows = db.session.execute(text("""
        WITH r AS (
          SELECT box_id, ts, inside, dit,
                 coalesce(heat1,0)+coalesce(heat2,0)+coalesce(heat3,0)+coalesce(heat4,0) AS heat
          FROM legacy_readings WHERE box_id = ANY(:ids) AND ts > :since AND ts <= :now),
        d AS (SELECT box_id, inside, dit, heat - lag(heat) OVER (PARTITION BY box_id ORDER BY ts) AS dh FROM r)
        SELECT box_id,
               avg(inside - dit) FILTER (WHERE inside > 0 AND dit BETWEEN 55 AND 85) AS excess,
               min(inside - dit) FILTER (WHERE inside > 0 AND dit BETWEEN 55 AND 85) AS least,
               count(*) FILTER (WHERE inside > 0 AND dit BETWEEN 55 AND 85) AS n,
               sum(dh) FILTER (WHERE dh BETWEEN 0 AND 15) AS burner
        FROM d GROUP BY box_id"""),
        {"ids": list(box_ids), "since": now - _dt.timedelta(hours=HOT_HOURS), "now": now}).all()
    out = {}
    for bid, excess, least, n, burner in rows:
        if excess is None or n < HOT_HOURS * 6 or burner is None:
            continue                     # too few reads with real apartment temperatures to judge
        if float(excess) >= HOT_EXCESS and least >= HOT_MIN_EXCESS and burner >= HOT_BURNER_MIN:
            out[bid] = (f"Overheating: the apartments averaged {float(excess):.0f}° over target for the last {HOT_HOURS} h "
                        f"(never under {least}° over) while the burner ran {int(burner)} min")
    return out
