import datetime as dt

from app import heat_overnight as ho

START = dt.datetime(2026, 11, 8, 2, 0)   # naive UTC, inside the overnight window


class _HRow:
    """A stand-in for a LegacyReading over the window."""
    __test__ = False
    def __init__(self, ts, supp=None, retn=None, out=None, code=None, h=0):
        self.ts, self.supp, self.retn, self.out, self.code = ts, supp, retn, out, code
        self.heat1, self.heat2, self.heat3, self.heat4 = h, 0, 0, 0


def seq(specs):
    return [_HRow(START + dt.timedelta(minutes=10 * i), **s) for i, s in enumerate(specs)]


# ── the run test itself ───────────────────────────────────────────────
def test_warm_loop_requires_both_pipes():
    assert ho._warm_loop(120, 95, 80) is True       # both above 80 — a run
    assert ho._warm_loop(120, 70, 80) is False       # return never cleared 80
    assert ho._warm_loop(70, 120, 80) is False       # supply never cleared 80
    assert ho._warm_loop(120, None, 80) is True       # only supply reports, and it is hot
    assert ho._warm_loop(None, None, 80) is None       # no loop sensors: can't tell


# ── counter path (EchoTherm / Bloom) ──────────────────────────────────
def test_counter_both_cold_is_no_heat():
    # cold out, the whole loop stayed under 80 -> a real miss
    v = ho._verdict_counter(0, 72, 2, 70, 2, 20, False, "cold")
    assert v["state"] == "no-heat"


def test_counter_run_when_both_hot():
    assert ho._verdict_counter(90, 140, 60, 110, 40, 20, False, "cold")["state"] == "ran"


def test_counter_one_pipe_hot_is_not_a_miss():
    # supply 140 / return 70: the loop got hot somewhere, so not a dead-cold miss
    v = ho._verdict_counter(90, 140, 60, 70, 2, 20, False, "cold")
    assert v["state"] == "idle" and "sensor" in v["detail"]


def test_counter_boiler_pct_with_cold_loop_is_not_a_run():
    # Bloom boiler % on, but the whole loop stayed under 80 -> not a run
    assert ho._verdict_counter(None, 75, 5, 72, 3, 20, True, "cold")["state"] == "no-heat"


def test_counter_sensorless_trusts_the_run_counter():
    assert ho._verdict_counter(90, None, None, None, None, 20, False, "cold")["state"] == "ran"


def test_counter_mild_night_is_idle():
    # loop cold but it was mild out: nothing needed heat
    assert ho._verdict_counter(0, 70, 0, 68, 0, 55, False, "cold")["state"] == "idle"


# ── legacy path (2010 oBIX) ───────────────────────────────────────────
def test_legacy_both_pipes_hot_is_ran():
    rows = seq([{"supp": 120, "retn": 95, "out": 30}] * 4)
    assert ho.verdict_legacy(rows, START)["state"] == "ran"


def test_legacy_supply_hot_return_cold_is_not_ran_but_not_a_miss():
    # return reads 70 while supply hit 140: the loop got hot, so a sensor flag, not "no heat"
    rows = seq([{"supp": 140, "retn": 70, "out": 25, "code": "Aqua Call"}] * 4)
    v = ho.verdict_legacy(rows, START)
    assert v["state"] == "idle" and "sensor" in v["detail"]


def test_legacy_called_but_whole_loop_cold_is_no_heat():
    rows = seq([{"supp": 72, "retn": 68, "out": 20, "code": "Aqua Call"}] * 4)
    v = ho.verdict_legacy(rows, START)
    assert v["state"] == "no-heat" and "supply and return" in v["detail"]


def test_legacy_idle_when_mild_and_no_call():
    rows = seq([{"supp": 70, "retn": 68, "out": 55}] * 4)
    assert ho.verdict_legacy(rows, START)["state"] == "idle"
