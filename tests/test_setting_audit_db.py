"""
tests/test_setting_audit_db.py — the settings ledger (Postgres tier; its pure
half first, which needs no database but shares the fixtures' file)

Who changed which setting of which building: a change sent through the portal
is recorded with the person and the reason; a change made anywhere else is
caught by comparing each poll's settings with the poll before.
"""
import datetime as dt

import pytest

from app import setting_audit as sa_
from tests.conftest import requires_postgres
from tests.test_bloom_settings import FORM, building
from tests.test_bloom_settings_db import REASON, _confirm, _url, feed      # noqa: F401  (fixture)
from tests.test_viewer_access_db import pat, root, world      # noqa: F401  (fixtures)

NOW = dt.datetime(2026, 11, 20, 23, 0, 0)


def _with(b, **extra):
    """A building plus watch-only points."""
    for t, v in extra.items():
        b["sensors"].append({"_id": "x-" + t, "type": t.replace("_", "-"), "pv": v})
    return b


# ── pure: what counts as a changed setting ───────────────────────────────────

def test_only_settings_are_compared_never_readings():
    snap = sa_.bloom_snapshot(_with(building(), dhw_supply_t_lo_sp=100, ht_supply_t=141.5))
    assert len(snap) == 14 and snap["ind-sp-day"] == 72 and snap["dhw-supply-t-lo-sp"] == 100
    assert "oat-act" not in snap and "avg-space-t" not in snap and "ht-supply-t" not in snap
    warm = building()
    next(s for s in warm["sensors"] if s["type"] == "oat-act")["pv"] = 20.3       # the weather is not a setting
    assert sa_.bloom_changes(building(), warm) == []


def test_a_changed_setting_reads_old_to_new_in_plain_words():
    ch = sa_.bloom_changes(building(), building(**{"ind-sp-night": 68, "stop-tm-day": 2250}))
    assert [(c["key"], c["label"], c["old_text"], c["new_text"]) for c in ch] == [
        ("ind-sp-night", "Indoor setpoint, night", "72°", "68°"),
        ("stop-tm-day", "Night period starts", "10:00 PM", "10:30 PM")]
    assert ch[0]["old"] == 72 and ch[0]["new"] == 68
    dhw = sa_.bloom_changes(_with(building(), dhw_supply_t_lo_sp=100), _with(building(), dhw_supply_t_lo_sp=110))
    assert [(c["label"], c["old_text"], c["new_text"]) for c in dhw] == [("Hot water too-cold alarm limit", "100°", "110°")]


def test_what_cannot_be_compared_is_not_a_change():
    assert sa_.bloom_changes(building(**{"ind-sp-day": 72.0}), building()) == []          # 72.0 is 72
    gone = building()
    gone["sensors"] = [s for s in gone["sensors"] if s["type"] != "ind-sp-day"]            # dropped out of one poll
    assert sa_.bloom_changes(building(), gone) == [] and sa_.bloom_changes(gone, building()) == []
    assert sa_.bloom_changes(building(), building(**{"ind-sp-day": None})) == []            # no value: not "set to nothing"
    twice = building(**{"space-min-sp-day": 66})
    twice["sensors"].append({"_id": "dup", "type": "space-min-sp-day", "pv": 68})            # listed twice: which one?
    assert sa_.bloom_changes(building(), twice) == []
    assert sa_.bloom_changes(None, building()) == [] and sa_.bloom_changes({}, {}) == []


def test_a_threshold_reads_in_the_terms_in_force_at_the_time():
    ch = sa_.bloom_changes(building(), building(**{"space-min-threshold-type": 0, "space-min-threshold": 3}))
    by = {c["key"]: c for c in ch}
    assert by["space-min-threshold"]["old_text"] == "20%" and by["space-min-threshold"]["new_text"] == "3 apartments"


# ── the ledger ───────────────────────────────────────────────────────────────



def _rows():
    import app.models as m
    return m.SettingChange.query.order_by(m.SettingChange.id).all()


@requires_postgres
def test_a_change_made_outside_the_portal_is_caught_by_the_poll(world, session):
    ch = sa_.bloom_changes(building(), building(**{"ind-sp-night": 68}))
    assert sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", ch, NOW) == 1
    session.commit()
    r, = _rows()
    assert (r.source, r.source_id, r.device, r.key, r.origin, r.by) == ("bloom", world["asset_mine"], "440 Berry St",
                                                                        "ind-sp-night", "outside", None)
    assert (r.old, r.new, r.old_text, r.new_text, r.at, r.seen_at) == (72, 68, "72°", "68°", NOW, NOW)
    assert "outside the portal" in sa_.who(r)
    assert sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", [], NOW) == 0       # nothing changed: nothing written
    session.commit()
    assert len(_rows()) == 1


@requires_postgres
def test_a_portal_change_arriving_at_the_controller_is_not_written_twice(world, session):
    ch = sa_.bloom_changes(building(), building(**{"ind-sp-day": 70}))
    sa_.record_portal("bloom", world["asset_mine"], None, "440 Berry St", ch, "Pat Viewer", "Too hot", landed=False, now=NOW)
    session.commit()
    r, = _rows()
    assert (r.origin, r.by, r.note, r.seen_at) == ("portal", "Pat Viewer", "Too hot", None) and sa_.who(r) == "Pat Viewer"

    later = NOW + dt.timedelta(seconds=50)                    # the next poll sees 72 -> 70
    assert sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", ch, later) == 0
    session.commit()
    r, = _rows()
    assert r.seen_at == later and r.origin == "portal"

    # the same setting moved AGAIN, by someone else, to another value: that one is new
    again = sa_.bloom_changes(building(**{"ind-sp-day": 70}), building(**{"ind-sp-day": 75}))
    assert sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", again, later + dt.timedelta(minutes=3)) == 1
    # ...and the same value on ANOTHER building is not the portal's change either
    assert sa_.observe("bloom", world["asset_other"], None, "9 Elsewhere Ave", ch, later) == 1
    # ...nor is the same value an hour later
    assert sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", ch, NOW + dt.timedelta(hours=1)) == 1
    session.commit()
    assert [r.origin for r in _rows()] == ["portal", "outside", "outside", "outside"]


@requires_postgres
def test_a_poll_that_beats_the_portals_own_record_still_ends_as_one_row_with_the_name(world, session):
    ch = sa_.bloom_changes(building(), building(**{"ind-sp-day": 70, "ind-sp-night": 68}))
    sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", ch[:1], NOW)              # the poll got there first
    session.commit()
    sa_.record_portal("bloom", world["asset_mine"], None, "440 Berry St", ch, "the administrator", "Why", landed=False,
                      now=NOW + dt.timedelta(seconds=4))
    session.commit()
    rows = _rows()
    assert len(rows) == 2 and all(r.origin == "portal" and r.by == "the administrator" and r.note == "Why" for r in rows)
    assert rows[0].seen_at == NOW and rows[1].seen_at is None
    sa_.mark_seen("bloom", world["asset_mine"], ["ind-sp-day", "ind-sp-night"], now=NOW + dt.timedelta(seconds=9))
    session.commit()
    assert all(r.seen_at is not None for r in _rows())


@requires_postgres
def test_the_poller_compares_each_building_with_the_poll_before(world, session, app, monkeypatch):
    import app.models as m
    from app import bloom
    acct = session.get(m.BloomAsset, world["asset_mine"]).account
    acct.token = "t"
    session.commit()
    live = building()
    live["name"], live["locid"] = "440 Berry St", "440-Berry-St"
    monkeypatch.setattr(bloom, "fetch_json", lambda account, s, token, path, timeout=20:
                        (({"buildings": [live]} if path == "/api/assets" else []), token))

    bloom.poll_account(app, acct)                             # first sight: a baseline, nothing to compare with
    assert _rows() == []
    bloom.poll_account(app, acct)                             # nothing touched
    assert _rows() == []
    next(s for s in live["sensors"] if s["type"] == "oat-sp-day")["pv"] = 60      # someone, somewhere, moves the cutoff
    next(s for s in live["sensors"] if s["type"] == "oat-act")["pv"] = 31.0        # and the weather turns
    bloom.poll_account(app, acct)
    r, = _rows()
    assert (r.device, r.label, r.old_text, r.new_text, r.origin) == ("440 Berry St", "Outdoor cutoff, day", "55°", "60°", "outside")
    assert r.building_id == session.get(m.BloomAsset, world["asset_mine"]).building_id and r.building_id is not None
    bloom.poll_account(app, acct)                             # and it is recorded once, not every poll
    assert len(_rows()) == 1


@requires_postgres
def test_a_change_sent_from_the_settings_page_lands_in_the_ledger_with_who_and_why(root, world, feed):
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "70", "stop-tm-day": "22:30"}
                        ).get_data(as_text=True)
    root.post(_url(world), data=_confirm(preview))
    rows = _rows()
    assert sorted((r.label, r.old_text, r.new_text) for r in rows) == [
        ("Indoor setpoint, day", "72°", "70°"), ("Night period starts", "10:00 PM", "10:30 PM")]
    assert all(r.origin == "portal" and r.by == "the administrator" and r.note == REASON and r.seen_at for r in rows)
    assert all(r.source == "bloom" and r.source_id == world["asset_mine"] and r.building_id for r in rows)

    feed["mode"] = "down"                                     # a change Bloom does not take changes nothing: no row
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "68", "stop-tm-day": "22:30"}
                        ).get_data(as_text=True)
    root.post(_url(world), data=_confirm(preview))
    assert len(_rows()) == 2

    feed["mode"] = "slow"                                     # accepted, not in place yet: recorded, marked unseen
    preview = root.post(_url(world), data={**FORM, "step": "preview", "ind-sp-day": "68", "stop-tm-day": "22:30"}
                        ).get_data(as_text=True)
    root.post(_url(world), data=_confirm(preview))
    assert len(_rows()) == 3 and _rows()[-1].seen_at is None
    page = root.get(f"/skyline/{world['asset_mine']}").get_data(as_text=True)
    assert "Settings changes" in page and "Indoor setpoint, day: 72° → <b>70°</b>" in page
    assert "not yet seen at the controller" in page and REASON in page


@requires_postgres
def test_the_changes_page_answers_who_what_where_when(root, pat, world, session):
    ch = sa_.bloom_changes(building(), building(**{"ind-sp-night": 68}))
    now = dt.datetime.utcnow()
    sa_.observe("bloom", world["asset_mine"], None, "440 Berry St", ch, now - dt.timedelta(hours=2))
    sa_.record_portal("bloom", world["asset_other"], None, "9 Elsewhere Ave",
                      sa_.bloom_changes(building(), building(**{"oat-sp-day": 60})), "Pat Viewer", "Warm spell",
                      landed=True, now=now - dt.timedelta(days=10))
    session.commit()

    assert pat.get("/changes").status_code == 403
    page = root.get("/changes").get_data(as_text=True)
    assert ">Changes</a>" in page                             # in the administrator's menu
    for text in ("440 Berry St", "Indoor setpoint, night", "72°", "<b>68°</b>", "outside the portal",
                 "9 Elsewhere Ave", "Outdoor cutoff, day", "<b>60°</b>", "Pat Viewer", "Warm spell"):
        assert text in page, text
    assert "Everyone <b>2</b>" in page and "Through the portal <b>1</b>" in page and "Outside the portal <b>1</b>" in page
    assert f'href="/skyline/{world["asset_mine"]}"' in page
    assert page.index("440 Berry St") < page.index("9 Elsewhere Ave")       # newest first

    week = root.get("/changes?days=7").get_data(as_text=True)
    assert "440 Berry St" in week and "9 Elsewhere Ave" not in week
    out = root.get("/changes?by=outside").get_data(as_text=True)
    assert "440 Berry St" in out and "Warm spell" not in out
    mine = root.get("/changes?q=warm").get_data(as_text=True)
    assert "9 Elsewhere Ave" in mine and "Indoor setpoint, night" not in mine
    assert "No settings changed" in root.get("/changes?q=zzz").get_data(as_text=True)
    assert "Changes</a>" not in pat.get("/home").get_data(as_text=True)
