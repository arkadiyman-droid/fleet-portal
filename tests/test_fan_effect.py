"""app/fan_effect.py — what switching the roof fans does, on synthetic
buildings whose answer is known."""
import datetime as dt

from app import fan_effect as fe

T0 = dt.datetime(2026, 11, 16, 5, 0)          # midnight New York, as UTC
STEP = dt.timedelta(minutes=5)


def building(days=10, off_days=(2, 5, 8), off_at=15, off_for=3, cool=0.3, burner_on=0.8, burner_off=0.4, ramp=False):
    """Fans on all day, except on `off_days` from hour `off_at` for `off_for`
    hours. While on they cool the apartments by `cool` F an hour and the
    burner runs `burner_on` of each minute (else `burner_off`)."""
    fan, reads, temp, heat = [], [], 72.0, 0
    t = T0
    while t < T0 + dt.timedelta(days=days):
        day, hour = (t - T0).days, (t - T0).seconds / 3600
        off = day in off_days and off_at <= hour < off_at + off_for
        share = 0.0 if off else 1.0
        if ramp and day in off_days and off_at - 1 / 12 <= hour < off_at + 1 / 12:
            share = 0.4                                  # one reading part-way while the groups stop
        fan.append((t, share))
        temp += (0.05 if off else -cool / 12) + (0.0 if off else cool / 12 - 0.0)   # steady with fans on
        if off:
            temp += 0.0
        heat += 5 * (burner_off if off else burner_on)
        reads.append((t, round(temp + (0.4 if off else 0) * min(1, (hour - off_at) / 2) if off else temp), 40, int(heat) % 1440))
        t += STEP
    return {"site": 1, "name": "Test St", "prop": "P", "fan": fan, "reads": reads}


def test_a_switch_is_found_with_the_hour_before_and_the_window_after():
    b = building()
    ev = fe.events(b["fan"])
    # off at 15:00 on days 2, 5 and 8, and back on three hours later
    assert [((t - T0).days, round((t - T0).seconds / 3600, 2), d) for t, d in ev] ==         [(day, h, d) for day in (2, 5, 8) for h, d in ((15.0, "off"), (18.0, "on"))]


def test_a_ramped_switch_counts_and_is_timed_from_the_first_move():
    b = building(ramp=True)
    offs = [t for t, d in fe.events(b["fan"]) if d == "off"]
    assert len(offs) == 3
    assert all(14.9 < (t - T0).seconds / 3600 < 15 for t in offs)   # timed from the part-way reading


def test_switching_off_saves_burner_and_warms_the_apartments():
    res = fe.analyse([building()])
    off = res["off"]
    assert off["n"] == 3 and off["buildings"] == 1
    assert off["burner"] < -30                                       # 2 h at 0.4 instead of 0.8 of each minute: -48
    assert abs(off["burner_event"] - 48) < 6 and abs(off["burner_control"] - 96) < 6
    assert off["effect"] > 0.2                                       # warmer than the same hours with the fans on
    # switching back on at 18:00: on every other day the fans were already on then, so no comparison
    assert res["on"] is None


def test_a_switch_with_no_comparable_day_is_left_out():
    # fans switch off every single day at the same hour: there is never a day they stayed on then
    b = building(off_days=tuple(range(10)))
    assert fe.analyse([b])["off"] is None


def test_verdict_and_chart():
    assert fe.verdict(None) == "few" and fe.verdict({"n": 4, "effect": 1, "se": 0.1}) == "few"
    assert fe.verdict({"n": 40, "effect": 0.3, "se": 0.1}) == "clear"
    assert fe.verdict({"n": 40, "effect": 0.1, "se": 0.1}) == "unclear"
    prof = [{"min": m, "event": m / 600, "control": -m / 600} for m in range(-60, 120, 15)]
    ch = fe.chart(prof)
    assert ch and len(ch["event"].split()) == len(prof) and ch["ticks"][2][1] == "switch"
    assert fe.chart([]) is None


def test_apartment_tone_and_the_property_average():
    a = lambda avg, tgt, live=True, ns=False, box=1: {"avg": avg, "target": tgt, "live": live, "no_sensors": ns, "box_id": box}
    assert [fe.tone(a(77, 71)), fe.tone(a(73, 71)), fe.tone(a(71, 71)), fe.tone(a(68, 71))] == ["hot", "warm", "ok", "cool"]
    assert fe.tone(a(77, 71, live=False)) == "none" and fe.tone(a(65, 71, ns=True)) == "none" and fe.tone(None) == "none"
    rows = [{"apts": a(74, 70, box=1)}, {"apts": a(74, 70, box=1)}, {"apts": a(70, 72, box=2)}, {"apts": None},
            {"apts": a(90, 70, live=False, box=3)}]
    assert fe.group_average(rows) == {"avg": 72.0, "target": 71.0, "n": 2}     # each box once, dead ones left out
