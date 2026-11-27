"""app/abnormal.py — a 2010 box doing what its own settings cannot explain.
Pure: a status as the box sends it and a settings snapshot as the portal
stores it (flat "object.item" -> value)."""
import datetime as dt

from app import abnormal as ab

# 2026-11-26 (a Thursday) 15:30 UTC = 10:30 New York
NOW = dt.datetime(2026, 11, 26, 15, 30)


def status(tit=71, out=50, box_utc=NOW, hct=13, code="Circ Pump", avg=71):
    return {"read_at": NOW.isoformat(),
            "config": {"heatControlType": str(hct)},
            "status": {"targetInsideTemperature": str(tit), "systemTemperature0": str(out), "averageTemperature": str(avg),
                       "serverTime": box_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "callForHeat": code}}


def settings(**over):
    s = {}
    for day in ("weekday", "saturday", "sunday"):
        for h in range(24):
            s[f"DITs.{day}DITHour{h}"] = "71" if 5 <= h < 22 else "70"
    s.update({"emsSettings.useHolidayDITs": "0", "emsSettings.dayStartHour": "6", "emsSettings.ditMode": "1",
              "emsSettings.autoDITAdjHigh": "2", "emsSettings.autoDITAdjLow": "2", "emsSettings.autoDITTempHigh": "53",
              "emsSettings.autoDITTempLow": "45", "emsSettings.outsideDaySetpoint": "55", "emsSettings.outsideNightSetpoint": "50",
              "emsSettings.insideDaySetpoint": "72", "emsSettings.insideNightSetpoint": "71", "emsSettings.winterStartMonth": "9",
              "emsSettings.winterStartDay": "1", "emsSettings.summerStartMonth": "5", "emsSettings.summerStartDay": "15",
              "emsConfig.enableDITLookAhead": "false", "emsConfig.coolingModeSelected": "false",
              "vacuumSteam.dccCompensation": "0"})
    s.update({k.replace("__", "."): str(v) for k, v in over.items()})
    return s


kinds = lambda found: [o["kind"] for o in found]


def test_a_healthy_box_is_quiet():
    assert ab.checks(status(), settings(), NOW) == []
    assert ab.checks(status(tit=73, out=39), settings(), NOW) == []          # colder than 45: +2 is the box's own auto-DIT
    assert ab.checks(status(tit=69, out=60), settings(), NOW) == []          # warmer than 53: -2
    assert ab.checks(status(tit=72), settings(), NOW) == []                  # 1 degree of slack


def test_the_801_south_street_target_is_caught_and_explained():
    found = ab.checks(status(tit=152, out=39, hct=5, code="DTime Rem"), settings(vacuumSteam__dccCompensation=79), NOW)
    assert kinds(found) == ["dit"]
    assert "Indoor target is 152°" in found[0]["text"] and "give 73° now" in found[0]["text"]
    assert "DCC compensation (+79)" in found[0]["text"]
    # a target that is simply wrong, with no DCC to blame
    plain = ab.checks(status(tit=85), settings(), NOW)
    assert kinds(plain) == ["dit"] and "DCC" not in plain[0]["text"]


def test_the_hour_either_side_is_accepted():
    # 06:00 New York the weekday table steps 70 -> 71 at 05:00; a box still on the old hour is not flagged
    early = dt.datetime(2026, 11, 26, 10, 2)                                # 05:02 New York
    assert ab.checks(status(tit=70, box_utc=early), settings(), early) == []


def test_the_saturday_and_sunday_tables_are_used_when_the_box_says_so():
    sat = dt.datetime(2026, 11, 28, 15, 30)                                 # Saturday 10:30 New York
    s = settings(emsSettings__useHolidayDITs=1, **{f"DITs__saturdayDITHour{h}": 66 for h in range(24)})
    assert ab.checks(status(tit=66, box_utc=sat), s, sat) == []
    assert kinds(ab.checks(status(tit=66), s, NOW)) == ["dit"]             # Thursday: the weekday table, not Saturday's
    forced = settings(emsSettings__useHolidayDITs=2)
    assert kinds(ab.checks(status(), forced, NOW)) == ["holiday"]


def test_settings_that_make_a_box_heat_badly():
    assert kinds(ab.checks(status(), settings(DITs__weekdayDITHour3=99), NOW)) == ["dit_table"]
    assert "3 AM: 99°" in ab.checks(status(), settings(DITs__weekdayDITHour3=99), NOW)[0]["text"]
    assert kinds(ab.checks(status(), settings(emsSettings__outsideDaySetpoint=75), NOW)) == ["cutoff"]
    assert kinds(ab.checks(status(), settings(emsSettings__insideNightSetpoint=65), NOW)) == ["heatoff"]


def test_a_box_in_summer_mode_in_the_heating_season():
    late = settings(emsSettings__winterStartMonth=12, emsSettings__winterStartDay=15)
    assert kinds(ab.checks(status(), late, NOW)) == ["season"]
    jan = dt.datetime(2026, 1, 10, 15, 0)
    assert kinds(ab.checks(status(code="Summer", box_utc=jan), None, jan)) == ["season"]
    july = dt.datetime(2026, 7, 10, 15, 0)
    assert ab.checks(status(code="Summer", box_utc=july), settings(), july) == []      # summer in July is right


def test_the_clock_and_the_heating_system_type():
    off = ab.checks(status(box_utc=NOW + dt.timedelta(hours=1), tit=71), settings(), NOW)
    assert "clock" in kinds(off) and "60 min fast" in off[0]["text"]
    assert kinds(ab.checks(status(hct=3), settings(), NOW)) == ["hct"]


def test_dcc_matters_only_on_vacuum_steam():
    assert ab.checks(status(hct=13), settings(vacuumSteam__dccCompensation=-107), NOW) == []    # junk, unused here
    found = ab.checks(status(hct=5), settings(vacuumSteam__dccCompensation=26), NOW)
    assert kinds(found) == ["dcc"] and "outside the box's own -20 to 20" in found[0]["text"]


def test_cooling_and_missing_settings():
    assert ab.checks(status(tit=75), settings(emsConfig__coolingModeSelected="true"), NOW) == []
    assert ab.checks(status(tit=150), None, NOW) == []                       # no snapshot yet: nothing to compare with
