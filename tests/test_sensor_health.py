import datetime as dt

from app import sensor_health as sh

SUPP = next(c for c in sh.CHANNELS if c[0] == "supp")
STACK = next(c for c in sh.CHANNELS if c[0] == "stack")
NOW = dt.datetime(2026, 11, 6, 12, 0)


def times(hours, step=5):
    n = hours * 60 // step
    return [NOW - dt.timedelta(minutes=step * (n - i)) for i in range(n)]


def test_healthy_sensor_is_silent():
    t = times(48)
    vals = [120 + (i % 5) - 2 for i in range(len(t))]      # wiggling 118-122
    assert sh.fault(t, vals, SUPP, NOW) is None


def test_stuck_high_open_probe():
    t = times(48)
    f = sh.fault(t, [255] * len(t), SUPP, NOW)
    assert f and f["kind"] == "stuck" and "255" in f["evidence"]


def test_frozen_hot_sensor():
    t = times(48)
    vals = [180 if ti >= NOW - dt.timedelta(hours=10) else 120 + (i % 5) for i, ti in enumerate(t)]
    f = sh.fault(t, vals, SUPP, NOW)
    assert f and f["kind"] == "frozen" and "180" in f["evidence"]


def test_idle_loop_at_ambient_is_not_frozen():
    # a loop sitting flat at ~75 F for days is idle, not a stuck sensor
    t = times(72)
    assert sh.fault(t, [75] * len(t), SUPP, NOW) is None


def test_stuck_low_shorted_probe():
    t = times(48)
    f = sh.fault(t, [12] * len(t), SUPP, NOW)
    assert f and f["kind"] == "stucklow" and "12" in f["evidence"]


def test_dropped_out_sensor():
    t = times(48)
    vals = [0 if ti >= NOW - dt.timedelta(hours=10) else 120 for ti in t]   # 0 = absent
    f = sh.fault(t, vals, SUPP, NOW)
    assert f and f["kind"] == "dropped"


def test_noisy_sensor():
    t = times(48)
    vals = [120 if i % 2 == 0 else 190 for i in range(len(t))]    # ±70 every read
    f = sh.fault(t, vals, SUPP, NOW)
    assert f and f["kind"] == "noisy"


def test_stack_only_rails():
    t = times(48)
    assert sh.fault(t, [300] * len(t), STACK, NOW) is None        # frozen/flat doesn't apply to stack
    f = sh.fault(t, [2000] * len(t), STACK, NOW)
    assert f and f["kind"] == "stuck"                            # but railing does


def test_no_sensor_is_not_a_fault():
    t = times(48)
    assert sh.fault(t, [0] * len(t), SUPP, NOW) is None          # all 0 = never fitted, not dropped
