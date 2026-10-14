"""
tests/test_rollouts_db.py — software rollouts from the portal (Postgres tier)

The engine (app/rollouts.step) is driven poll by poll with a fake clock and
a fake controller endpoint; what each controller reports (fleetHealth.ota in
its status) is set by hand between polls, the way a real controller's
updater would change it. EMS's side is tests/test_fleet_software.py there.
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

OLD = "1" * 40
NEW = "d61237276d6eff8cfa8620ac2f412463422bd8e1"
T0 = dt.datetime(2026, 10, 14, 15, 0, 0)


def at(minutes):
    return T0 + dt.timedelta(minutes=minutes)


@pytest.fixture
def client(app):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def notes(monkeypatch):
    import app.notify as n
    sent = []
    monkeypatch.setattr(n, "send", lambda title, body, **kw: sent.append((title, body, kw)) or "ntfy ok")
    return sent


@pytest.fixture
def fleet(session):
    """Three healthy controllers on OLD, all seeing NEW on their release branch."""
    import app.models as m
    c = m.Client(name="Acme"); session.add(c); session.flush()
    p = m.Property(client_id=c.id, name="Main St"); session.add(p); session.flush()
    b = m.Building(property_id=p.id, name="12 Main St"); session.add(b); session.flush()
    out = []
    for i, name in enumerate(("A test site", "B", "C")):
        k = m.Controller(building_id=b.id, name=name, tailscale_ip=f"100.64.0.{i + 1}", fleet_api_token="x" * 64)
        session.add(k); session.flush()
        out.append(k)
        report(session, k, current=OLD)
    session.commit()
    return out


def report(session, ctrl, current, pending=False, ok=True, online=True, update_ts=None, when=T0):
    """Set what the controller's /api/status says about its software."""
    import app.models as m
    row = session.get(m.ControllerStatus, ctrl.id) or m.ControllerStatus(controller_id=ctrl.id)
    row.ok, row.polled_at = online, when
    row.last_ok_at = when if online else T0 - dt.timedelta(hours=1)
    row.error = None if online else "timed out"
    row.raw_json = {"fleetHealth": {"ota": {
        "currentSha": current, "availableSha": NEW, "updateAvailable": current != NEW,
        "pendingConfirm": {"sha": current, "secsRemaining": 300} if pending else None,
        "lastUpdateOk": ok, "lastUpdateTs": update_ts}}}
    session.add(row)
    session.commit()


def fake_controllers():
    """Stands in for POST /api/fleet/software on each controller (a function,
    not a class: pytest here collects every class). .calls records what was
    asked; .answers maps a controller name to (status, body)."""
    def send(app, ctrl, sha):
        send.calls.append((ctrl.name, sha))
        return send.answers.get(ctrl.name, (200, {"ok": True, "queued": True}))
    send.calls, send.answers = [], {}
    return send


@pytest.fixture
def ctl():
    return fake_controllers()


def step(app, ctl, minutes):
    from app import rollouts
    rollouts.step(app, now=at(minutes), sender=ctl)


def states(r):
    return {t.controller.name: t.state for t in r.targets}


def start(fleet, batch=1, soak=30):
    from app import rollouts
    return rollouts.create(NEW, fleet, fleet[0].id, batch_size=batch, soak_minutes=soak, by="test")


class Rolling:

    def test_test_site_first_then_one_batch_at_a_time(self, app, session, fleet, ctl, notes):
        a, b, c = fleet
        r = start(fleet, batch=1, soak=30)
        assert [(t.controller.name, t.wave) for t in r.targets] == [("A test site", 0), ("B", 1), ("C", 2)]

        step(app, ctl, 0)
        assert ctl.calls == [("A test site", NEW)] and states(r)["A test site"] == "requested"
        report(session, a, NEW, pending=True); step(app, ctl, 2)
        assert states(r)["A test site"] == "confirming"
        report(session, a, NEW); step(app, ctl, 12)
        assert states(r)["A test site"] == "done" and r.wave == 0
        step(app, ctl, 30)
        assert r.wave == 0 and len(ctl.calls) == 1                 # still watching the test site
        step(app, ctl, 43)                                         # 31 min after it finished
        assert r.wave == 1
        step(app, ctl, 44)
        assert ctl.calls[-1] == ("B", NEW)
        report(session, b, NEW); step(app, ctl, 50)
        step(app, ctl, 81); step(app, ctl, 82)
        report(session, c, NEW); step(app, ctl, 90)
        step(app, ctl, 121)
        assert r.state == "done" and set(states(r).values()) == {"done"}
        assert notes[-1][0] == f"Software rollout complete ({NEW[:7]})" and notes[-1][2]["good_news"]

    def test_a_rollback_on_the_test_site_stops_everything(self, app, session, fleet, ctl, notes):
        a = fleet[0]
        r = start(fleet)
        step(app, ctl, 0)
        report(session, a, NEW, pending=True); step(app, ctl, 2)
        report(session, a, OLD, ok=False, update_ts=at(1).timestamp()); step(app, ctl, 5)
        assert states(r)["A test site"] == "failed" and r.state == "halted"
        assert "rolled itself back" in r.reason
        step(app, ctl, 60)
        assert [n for n, _ in ctl.calls] == ["A test site"]         # B and C never asked
        assert notes[-1][0].startswith("Software rollout HALTED") and notes[-1][2]["urgent"]

    def test_turning_red_after_updating_stops_it_too(self, app, session, fleet, ctl, notes):
        a = fleet[0]
        r = start(fleet)
        step(app, ctl, 0)
        report(session, a, NEW); step(app, ctl, 3)
        assert states(r)["A test site"] == "done"
        report(session, a, NEW, online=False, when=at(20))          # offline during the watch
        step(app, ctl, 34)
        assert r.state == "halted" and "turned red after updating" in r.reason
        assert [n for n, _ in ctl.calls] == ["A test site"]

    def test_never_finishing_is_a_failure(self, app, session, fleet, ctl, notes):
        r = start(fleet)
        step(app, ctl, 0)
        step(app, ctl, 31)
        assert states(r)["A test site"] == "failed" and "30 minutes" in r.targets[0].detail and r.state == "halted"

    def test_the_release_moving_halts_and_installs_nothing(self, app, session, fleet, ctl, notes):
        ctl.answers["A test site"] = (409, {"ok": False, "code": "release_moved", "error": "now at fffffff"})
        r = start(fleet)
        step(app, ctl, 0)
        assert r.state == "halted" and "moved" in r.reason

    def test_sites_that_cannot_take_it_are_skipped_not_failed(self, app, session, fleet, ctl, notes):
        a, b, c = fleet
        ctl.answers["B"] = (404, {})
        ctl.answers["C"] = (403, {"code": "remote_update_off"})
        r = start(fleet, batch=2, soak=0)
        step(app, ctl, 0)
        report(session, a, NEW); step(app, ctl, 3); step(app, ctl, 4); step(app, ctl, 5)
        assert states(r) == {"A test site": "done", "B": "skipped", "C": "skipped"}
        step(app, ctl, 6)
        assert r.state == "done"
        assert "too old" in next(t.detail for t in r.targets if t.controller.name == "B")

    def test_offline_is_retried_for_an_hour_then_skipped(self, app, session, fleet, ctl, notes):
        ctl.answers["A test site"] = (None, {"error": "unreachable (ConnectTimeout)"})
        r = start(fleet)
        for m in (0, 10, 30, 59):
            step(app, ctl, m)
        assert states(r)["A test site"] == "pending" and len(ctl.calls) == 4
        step(app, ctl, 61)
        assert states(r)["A test site"] == "skipped"

    def test_busy_confirming_an_earlier_update_waits(self, app, session, fleet, ctl, notes):
        ctl.answers["A test site"] = (409, {"ok": False, "code": "pending_confirm", "reason": "still proving itself"})
        r = start(fleet)
        step(app, ctl, 0)
        assert states(r)["A test site"] == "pending" and "proving" in r.targets[0].detail
        ctl.answers.pop("A test site")
        step(app, ctl, 2)
        assert states(r)["A test site"] == "requested"

    def test_paused_does_nothing(self, app, session, fleet, ctl, notes):
        from app.extensions import db
        r = start(fleet)
        r.state = "paused"; db.session.commit()
        step(app, ctl, 0)
        assert ctl.calls == []


class Creating:

    def test_rules(self, app, session, fleet):
        from app import rollouts
        with pytest.raises(ValueError, match="pick a release"):
            rollouts.create("main", fleet, fleet[0].id)
        report(session, fleet[0], NEW)
        with pytest.raises(ValueError, match="test controller"):
            rollouts.create(NEW, fleet, fleet[0].id)              # it is already on it
        rollouts.create(NEW, fleet, fleet[1].id)
        with pytest.raises(ValueError, match="another rollout"):
            rollouts.create(NEW, fleet, fleet[2].id)
        for k in fleet:
            report(session, k, NEW)
        with pytest.raises(ValueError, match="already on that release"):
            rollouts.create(NEW, fleet, fleet[0].id)


class Pages:

    def test_updates_page_and_starting_from_it(self, app, session, fleet, client):
        import app.models as m
        page = client.get("/updates").data
        assert b"What the fleet runs" in page and NEW[:7].encode() in page and b"Start rollout to 3 controllers" in page
        r = client.post("/updates/start", data={"sha": NEW, "test_controller_id": fleet[1].id,
                                                "batch_size": "2", "soak_minutes": "15"})
        assert r.status_code == 302
        ro = m.Rollout.query.one()
        assert (ro.batch_size, ro.soak_minutes, ro.targets[0].controller_id) == (2, 15, fleet[1].id)
        assert b"Release " + NEW[:7].encode() in client.get("/updates").data
        client.post(f"/updates/{ro.id}/cancel")
        session.expire_all()
        assert m.Rollout.query.one().state == "cancelled"

    def test_update_one_controller_from_its_page(self, app, session, fleet, client):
        import app.models as m
        page = client.get(f"/fleet/{fleet[2].id}").data
        assert b"Update this controller now" in page
        client.post(f"/fleet/{fleet[2].id}/update")
        ro = m.Rollout.query.one()
        assert [t.controller_id for t in ro.targets] == [fleet[2].id] and ro.soak_minutes == 0

    def test_a_controller_that_never_reported_its_software_is_left_out(self, app, session, fleet, client):
        import app.models as m
        from app import rollouts
        row = session.get(m.ControllerStatus, fleet[2].id)
        row.raw_json = {}
        session.commit()
        r = rollouts.create(NEW, fleet, fleet[0].id)
        assert [t.controller.name for t in r.targets] == ["A test site", "B"]
        assert b"left out of rollouts: C" in client.get("/updates").data

    def test_asking_the_fleet_to_check_for_a_release(self, app, session, fleet):
        from app import rollouts
        answers = {"A test site": {"ok": True, "availableSha": NEW}, "B": {"ok": True, "availableSha": NEW}, "C": None}
        res = rollouts.ask_to_check(app, fleet, sender=lambda a, c: answers[c.name])
        assert res == {"asked": 3, "answered": 2, "seen": {NEW: 2}}
