"""
tests/test_server_health_db.py — the Server page and its minute-by-minute
history (Postgres tier).

The machine itself is not read here: collect() is replaced by a snapshot
from tests/test_server_health.snap(), so the page and the sampler are tested
on known numbers.
"""
import datetime as dt

import pytest

from tests.conftest import requires_postgres
from tests.test_server_health import snap

pytestmark = requires_postgres


@pytest.fixture
def fake(monkeypatch):
    from app import server_health as sh
    calls = []

    def collect(prev=None, with_db=True):
        calls.append(prev)
        s = snap()
        s["host"] = sh.HOST
        s["raw"] = {"t": s["t"]}
        return s
    monkeypatch.setattr(sh, "collect", collect)
    return calls


def _client(app, **who):
    app.config["WTF_CSRF_ENABLED"] = False
    c = app.test_client()
    with c.session_transaction() as s:
        s.update(who)
    return c


def test_one_sample_a_minute_however_many_workers_ask(app, session, fake, monkeypatch):
    from app import server_health as sh
    from app.models import ServerHost, ServerSample
    t0 = dt.datetime(2026, 11, 25, 15, 7, 3)
    monkeypatch.setattr(sh, "_utcnow", lambda: t0)
    tot = sh.sample_once(60, 30, collect_fn=sh.collect)
    assert tot["cpu_pct"] == 12.0 and tot["problems"] == 0
    assert sh.sample_once(60, 30, collect_fn=sh.collect) is None          # the other worker, same minute
    assert ServerSample.query.count() == 1 and len(fake) == 1
    row = session.get(ServerHost, sh.HOST)
    assert row.seen_at == t0 and row.snapshot["cpu"]["pct"] == 12.0

    monkeypatch.setattr(sh, "_utcnow", lambda: t0 + dt.timedelta(seconds=61))
    sh.sample_once(60, 30, collect_fn=sh.collect)
    assert ServerSample.query.count() == 2
    assert fake[-1]["cpu"]["pct"] == 12.0                                   # measured against the stored snapshot
    s = ServerSample.query.order_by(ServerSample.at).all()
    assert [x.at for x in s] == [dt.datetime(2026, 11, 25, 15, 7), dt.datetime(2026, 11, 25, 15, 8)]


def test_old_samples_are_dropped_once_an_hour(app, session, fake, monkeypatch):
    from app import server_health as sh
    from app.models import ServerSample
    session.add_all([ServerSample(host=sh.HOST, at=dt.datetime(2026, 10, 1, 0, 0), cpu_pct=1),
                     ServerSample(host=sh.HOST, at=dt.datetime(2026, 11, 20, 0, 0), cpu_pct=1)])
    session.commit()
    monkeypatch.setattr(sh, "_utcnow", lambda: dt.datetime(2026, 11, 25, 16, 0, 5))
    sh.sample_once(60, 30, collect_fn=sh.collect)
    assert sorted(x.at.month for x in ServerSample.query.all()) == [11, 11]


def test_history_is_averaged_into_buckets(app, session, monkeypatch):
    from app import server_health as sh
    from app.models import ServerSample
    now = dt.datetime(2026, 11, 25, 12, 0)
    monkeypatch.setattr(sh, "_utcnow", lambda: now)
    for i in range(20):                                   # 20 minutes, cpu 0..19
        session.add(ServerSample(host="beverly-pi", at=now - dt.timedelta(minutes=20 - i), cpu_pct=float(i),
                                 temp_c=50.0))
    session.add(ServerSample(host="other-pi", at=now - dt.timedelta(minutes=5), cpu_pct=99.0))
    session.commit()
    d = sh.history("beverly-pi", 24)
    assert d["bucket_s"] == 60 and len(d["t"]) == 20 and d["cpu_pct"][:3] == [0.0, 1.0, 2.0]
    assert d["net_rx_kbps"] == [None] * 20
    week = sh.history("beverly-pi", 168)                  # 10-minute buckets
    assert week["bucket_s"] == 600 and len(week["t"]) <= 3
    assert sum(week["cpu_pct"]) / len(week["cpu_pct"]) == pytest.approx(9.5, abs=3)


def test_the_page_is_for_the_administrator_only(app, session, fake):
    import app.models as m
    from werkzeug.security import generate_password_hash
    u = m.User(name="Pat", email="pat@example.test", login="pat", password_hash=generate_password_hash("pw-123456"))
    session.add(u)
    session.commit()
    pat = _client(app, user_id=u.id)
    assert pat.get("/server").status_code == 403
    assert pat.get("/server/history.json").status_code == 403
    assert "Server</a>" not in pat.get("/home").get_data(as_text=True)

    root = _client(app, superuser=True)
    r = root.get("/server")
    html = r.get_data(as_text=True)
    assert r.status_code == 200
    assert "All good" in html and "fleet.echothermcontrols.com" in html
    assert "If this server fails" in html and "ONLY on this same disk" in html
    assert "Wi-Fi only (ASUS_B)" in html
    assert 'href="/server"' in html                                         # in the menu
    j = root.get("/server/history.json?hours=6").get_json()
    assert j["t"] == [] and j["hours"] == 6


def test_a_problem_shows_at_the_top_of_the_page(app, session, monkeypatch):
    from app import server_health as sh

    def collect(prev=None, with_db=True):
        s = snap(heat__temp_c=83.0)
        s["websites"][0].update(ok=False, code=None, error="Connection refused")
        return s
    monkeypatch.setattr(sh, "collect", collect)
    html = _client(app, superuser=True).get("/server").get_data(as_text=True)
    assert "2 problems need attention now" in html
    assert "fleet.echothermcontrols.com did not answer (Connection refused)" in html
    assert "83 °C" in html


def test_another_server_is_shown_from_its_last_report(app, session, fake):
    from app.models import ServerHost
    other = snap()
    other["host"] = "standby-pi"
    session.add(ServerHost(host="standby-pi", seen_at=dt.datetime.utcnow(), snapshot=other))
    session.commit()
    root = _client(app, superuser=True)
    html = root.get("/server").get_data(as_text=True)
    assert "standby-pi" in html                                             # the picker appears
    r = root.get("/server?host=standby-pi")
    assert r.status_code == 200 and "Server — standby-pi" in r.get_data(as_text=True)
    assert "last report" in r.get_data(as_text=True)
    assert root.get("/server?host=nobody").status_code == 404
