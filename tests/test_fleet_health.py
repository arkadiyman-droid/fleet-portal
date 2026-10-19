"""
tests/test_fleet_health.py — what the Fleet Overview colours mean

Pure tests (no database). app/fleet_health.evaluate() decides whether a
controller card is green, amber or red, and with which reasons. Getting
this wrong costs either a missed failing backup or an operator trained to
ignore red. The cases below are the ones that matter in a boiler room.
"""
import datetime as dt
import types

import app.fleet_health as F

NOW = 2_000_000_000.0
NOW_DT = dt.datetime(2033, 5, 18, 3, 33, 20)


def ctrl(ip="10.0.0.5", token="t"):
    return types.SimpleNamespace(tailscale_ip=ip, fleet_api_token=token)


def status(ok=True, polled_ago=30, last_ok_ago=30, raw=None, error=None, version="3.1.0 (abc1234)"):
    return types.SimpleNamespace(
        ok=ok, error=error, http_status=None, version=version, building_name="B",
        polled_at=NOW_DT - dt.timedelta(seconds=polled_ago),
        last_ok_at=None if last_ok_ago is None else NOW_DT - dt.timedelta(seconds=last_ok_ago),
        raw_json=raw)


def healthy_raw(**fh_over):
    fh = {
        "schema": 1, "hostname": "site-a", "uptimeSecs": 90000, "load1": 0.1, "cpuTempC": 50.0,
        "cpuPct": 2.0, "memUsedPct": 15.0, "diskUsedPct": 40.0, "diskFreeGb": 15.0,
        "undervoltageNow": False, "undervoltageSinceBoot": False, "throttledNow": False,
        "backup": {"enabled": True, "lastTs": NOW - 600, "lastOk": True, "lastError": None,
                   "bundleConfigured": True, "bundleTs": NOW - 2 * 86400, "bundleError": None,
                   "dbTs": NOW - 10 * 3600},
        "ota": {"currentSha": "abc1234def", "updateAvailable": False, "availableSha": "abc1234def",
                "lastCheckTs": NOW - 3600, "lastUpdateTs": None, "lastUpdateOk": True, "pendingConfirm": None},
        "provisioned": None,
    }
    for k, v in fh_over.items():
        if isinstance(v, dict) and isinstance(fh.get(k), dict):
            fh[k] = {**fh[k], **v}
        else:
            fh[k] = v
    return {"heatMode": "Auto", "outdoor": 34, "avgAptTemp": 69.0, "tit": 74, "heatDemand": True,
            "bypass": False, "recentAlarms": [], "fleetHealth": fh}


def ev(st, c=None):
    return F.evaluate(c or ctrl(), st, now=NOW, now_dt=NOW_DT)


def texts(e):
    return " | ".join(t for _, t in e["issues"])


class HealthyIsGreen:

    def test_everything_fine_is_ok_with_no_issues(self):
        e = ev(status(raw=healthy_raw()))
        assert e["level"] == "ok", texts(e)
        assert e["issues"] == []
        assert e["online"] is True
        assert e["backup"]["historyLevel"] == "ok" and e["backup"]["bundleLevel"] == "ok"


class UnknownIsNotRed:

    def test_unconfigured_and_never_polled_are_unknown(self):
        assert ev(None, c=ctrl(ip=None))["level"] == "unknown"
        assert ev(None)["level"] == "unknown"

    def test_old_controller_without_health_is_info_not_red(self):
        raw = healthy_raw(); raw.pop("fleetHealth")
        e = ev(status(raw=raw))
        assert e["level"] == "info"
        assert "predates" in texts(e)


class RedMeansActNow:

    def test_offline_says_how_long_in_plain_words(self):
        e = ev(status(ok=False, polled_ago=20, last_ok_ago=7200, raw=healthy_raw(),
                      error="HTTPConnectionPool(host='192.0.2.10', port=8061): ConnectTimeoutError ... timed out"))
        assert e["level"] == "crit"
        assert "Offline, last seen 2 h ago: no answer (timed out)" in texts(e)
        assert "HTTPConnectionPool" not in texts(e)      # the raw exception stays off the card
        assert e["seen"] == "last seen 2 h ago"

    def test_never_answered(self):
        e = ev(status(ok=False, last_ok_ago=None, raw=None, error="timed out"))
        assert e["seen"] == "never answered"

    def test_poll_errors_become_actions(self):
        assert "re-enter the Fleet API token" in F.plain_error(401, "401 Client Error")
        assert "not running" in F.plain_error(None, "Connection refused")
        assert "error (500)" in F.plain_error(500, "500 Server Error")
        assert "too old" in F.plain_error(404, "404")
        assert F.plain_error(None, "something new") == "no usable answer"

    def test_power_failing_now(self):
        e = ev(status(raw=healthy_raw(undervoltageNow=True)))
        assert e["level"] == "crit" and "under-voltage NOW" in texts(e)

    def test_backup_failed(self):
        e = ev(status(raw=healthy_raw(backup={"lastOk": False, "lastError": "network down"})))
        assert e["level"] == "crit" and "network down" in texts(e)

    def test_bundle_failed(self):
        e = ev(status(raw=healthy_raw(backup={"bundleError": "age is not installed"})))
        assert e["level"] == "crit"

    def test_history_three_days_old(self):
        e = ev(status(raw=healthy_raw(backup={"dbTs": NOW - 4 * 86400})))
        assert e["level"] == "crit" and e["backup"]["historyLevel"] == "crit"

    def test_disk_nearly_full(self):
        assert ev(status(raw=healthy_raw(diskUsedPct=93.0)))["level"] == "crit"

    def test_active_alarm_is_red_and_counted(self):
        raw = healthy_raw()
        raw["heatCtrlType"] = 1                                    # steam: an ignition failure matters
        # real EMS alarm types (events/event_logger.py): 1 ignition, 3 safety, 6 restart
        raw["recentAlarms"] = [{"type": 1, "label": "Ignition failure", "active": True},
                               {"type": 3, "label": "Safety", "active": True},
                               {"type": 6, "label": "Restart", "active": False}]
        e = ev(status(raw=raw))
        assert e["level"] == "crit"
        assert len(e["alarms"]) == 2
        assert "Ignition failure (+1 more)" in texts(e)


class AmberMeansLookSoon:

    def test_backup_off(self):
        e = ev(status(raw=healthy_raw(backup={"enabled": False})))
        assert e["level"] == "warn" and "cannot be restored" in texts(e)

    def test_bundle_nine_days_old(self):
        e = ev(status(raw=healthy_raw(backup={"bundleTs": NOW - 9 * 86400})))
        assert e["level"] == "warn" and e["backup"]["bundleLevel"] == "warn"

    def test_bundle_off(self):
        assert "Site bundle OFF" in texts(ev(status(raw=healthy_raw(backup={"bundleConfigured": False}))))

    def test_undervoltage_earlier(self):
        assert ev(status(raw=healthy_raw(undervoltageSinceBoot=True)))["level"] == "warn"

    def test_hot_cpu(self):
        assert ev(status(raw=healthy_raw(cpuTempC=74.0)))["level"] == "warn"

    def test_status_gone_quiet(self):
        e = ev(status(polled_ago=900, last_ok_ago=900, raw=healthy_raw()))
        assert e["level"] == "warn" and "poller" in texts(e)

    def test_bypass_on(self):
        raw = healthy_raw(); raw["bypass"] = True
        assert "Bypass is ON" in texts(ev(status(raw=raw)))


class WorstWinsAndOrder:

    def test_crit_beats_warn_and_issues_are_sorted_worst_first(self):
        e = ev(status(raw=healthy_raw(cpuTempC=74.0, undervoltageNow=True)))
        assert e["level"] == "crit"
        assert e["issues"][0][0] == "crit"

    def test_update_available_is_info_only(self):
        e = ev(status(raw=healthy_raw(ota={"updateAvailable": True, "availableSha": "fff0000aaa"})))
        assert e["level"] == "info" and "fff0000" in texts(e)


class Summary:

    def test_tiles_count_the_right_things(self):
        cards = [{"eval": ev(status(raw=healthy_raw()))},
                 {"eval": ev(status(ok=False, raw=healthy_raw(), error="x"))},
                 {"eval": ev(status(raw=healthy_raw(backup={"enabled": False})))},
                 {"eval": ev(None)}]
        s = F.summarize(cards)
        assert s == {"total": 4, "online": 2, "offline": 1, "attention": 2, "alarms": 0,
                     "backups": 1, "updates": 0, "unknown": 1}


class Wording:

    def test_ago_and_duration(self):
        assert F.ago(None) == "never" and F.ago(5) == "just now"
        assert F.ago(240) == "4 min ago" and F.ago(7200) == "2 h ago" and F.ago(3 * 86400) == "3 d ago"
        assert F.duration(90000) == "1 d 1 h" and F.duration(3700) == "1 h 1 min"


def test_supply_vs_target_cold_loop_only_under_80_while_calling():
    d = {"heatCtrlType": 20, "dSupTemp": 150, "supTemp": 120, "hwrTemp": 101, "heatDemand": True}
    warm = F.supply_vs_target(d)
    assert (warm["supplyTarget"], warm["supply"], warm["ret"], warm["cold"]) == (150, 120, 101, None)   # 30 F short, warm: fine
    cold = F.supply_vs_target(d | {"supTemp": 85, "hwrTemp": 72})
    assert cold["cold"] == "Hot water cold: return 72 F, under 80 F while calling for heat"
    assert cold["retCold"] and not cold["supplyCold"]
    assert F.supply_vs_target(d | {"supTemp": 70, "heatDemand": False})["cold"] is None           # not calling
    assert F.supply_vs_target(d | {"heatCtrlType": 1}) == {}                                       # steam

