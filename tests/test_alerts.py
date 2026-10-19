"""
tests/test_alerts.py — when an alert goes out, and what it looks like (no database)

app/alerts.decide() is driven with a fake clock; app/notify.py with the
network stubbed at requests.post and smtplib. The database half (state kept
across polls, the pages, muting) is tests/test_alerts_db.py.
"""
import datetime as dt
from unittest import mock

import flask
import pytest

from app import alerts, notify

T0 = dt.datetime(2026, 10, 14, 12, 0, 0)
CFG = {"ALERT_AFTER_SECONDS": 300, "ALERT_REPEAT_HOURS": 24}
OFFLINE = ["Offline for 3 min: no answer (timed out)"]


def fresh():
    return {"alerting": False, "first_bad_at": None, "opened_at": None, "notified_at": None, "muted_until": None}


def run(steps, st=None, cfg=CFG):
    """steps: [(minutes after T0, level, crit list)] -> list of (minute, event kind or None, send)."""
    st = st or fresh()
    out = []
    for m, level, crit in steps:
        st, ev = alerts.decide(st, level, crit, T0 + dt.timedelta(minutes=m), cfg)
        out.append((m, ev and ev["kind"], ev and ev["send"]))
    return st, out


def kinds(out):
    return [(m, k) for m, k, _ in out if k]


def test_red_must_last_five_minutes_before_anyone_is_paged():
    _, out = run([(0, "crit", OFFLINE), (2, "crit", OFFLINE), (4, "crit", OFFLINE), (5, "crit", OFFLINE), (6, "crit", OFFLINE)])
    assert kinds(out) == [(5, "open")]                    # once, at five minutes, not again at six


def test_a_blip_pages_nobody():
    st, out = run([(0, "crit", OFFLINE), (1, "crit", OFFLINE), (2, "ok", []), (3, "crit", OFFLINE), (7, "crit", OFFLINE)])
    assert kinds(out) == []                               # the clock restarted at minute 3
    assert st["first_bad_at"] == T0 + dt.timedelta(minutes=3)


def test_recovered_says_what_cleared_and_how_long_it_lasted():
    st, _ = run([(0, "crit", OFFLINE), (5, "crit", OFFLINE)])
    st["issue"] = "Status: Safety."
    st, ev = alerts.decide(st, "ok", [], T0 + dt.timedelta(minutes=95), CFG)
    assert ev["kind"] == "recovered" and ev["send"] is True
    assert ev["text"] == "Safety: cleared after 1 h 30 min."                # not "back to normal" from what?
    assert not st["alerting"] and st["first_bad_at"] is None


def test_a_short_problem_closes_quietly():
    """~300 pushes a day were "trip ... back to normal after 4 min" (2026-10-18):
    the alert already went out; its all-clear is not worth a second push."""
    st, _ = run([(0, "crit", OFFLINE), (5, "crit", OFFLINE)])
    st, ev = alerts.decide(st, "ok", [], T0 + dt.timedelta(minutes=20), {**CFG, "ALERT_RECOVERED_MIN_SECONDS": 1800})
    assert ev["kind"] == "recovered" and ev["send"] is False and "closed quietly" in ev["quiet"]


def test_a_reminder_each_day_while_still_red():
    steps = [(0, "crit", OFFLINE), (5, "crit", OFFLINE)] + [(m, "crit", OFFLINE) for m in (60, 23 * 60, 24 * 60 + 5, 30 * 60, 48 * 60 + 5)]
    _, out = run(steps)
    assert kinds(out) == [(5, "open"), (24 * 60 + 5, "reminder"), (48 * 60 + 5, "reminder")]


def test_reminders_can_be_switched_off():
    _, out = run([(0, "crit", OFFLINE), (5, "crit", OFFLINE), (3000, "crit", OFFLINE)], cfg={**CFG, "ALERT_REPEAT_HOURS": 0})
    assert kinds(out) == [(5, "open")]


def test_yellow_never_pages():
    _, out = run([(m, "warn", []) for m in range(0, 120, 5)])
    assert kinds(out) == []


def test_crit_without_a_crit_issue_is_not_red():
    _, out = run([(0, "crit", []), (10, "crit", [])])
    assert kinds(out) == []


def test_muted_means_quiet_until_the_mute_ends():
    st = fresh() | {"muted_until": T0 + dt.timedelta(hours=4)}
    st, out = run([(0, "crit", OFFLINE), (30, "crit", OFFLINE), (239, "crit", OFFLINE), (241, "crit", OFFLINE)], st=st)
    assert kinds(out) == [(241, "open")]                  # still red after the visit: now it pages


def test_recovering_during_a_mute_closes_quietly():
    st, _ = run([(0, "crit", OFFLINE), (5, "crit", OFFLINE)])
    st["muted_until"] = T0 + dt.timedelta(hours=2)
    st, ev = alerts.decide(st, "ok", [], T0 + dt.timedelta(minutes=30), CFG)
    assert ev["kind"] == "recovered" and ev["send"] is False and not st["alerting"]


def test_no_reminder_while_muted():
    st, _ = run([(0, "crit", OFFLINE), (5, "crit", OFFLINE)])
    st["muted_until"] = T0 + dt.timedelta(days=3)
    st, ev = alerts.decide(st, "crit", OFFLINE, T0 + dt.timedelta(hours=30), CFG)
    assert ev is None


# ── what the message says ────────────────────────────────────────────────

def _ctrl():
    client = mock.Mock(); client.name = "Acme Housing"
    prop = mock.Mock(client=client); prop.name = "Main St"
    b = mock.Mock(property=prop); b.name = "12 Main St"
    c = mock.Mock(building=b, id=7); c.name = "Boiler room"
    return c


def test_message_names_the_place_and_the_problem():
    title, body = alerts.message("open", _ctrl(), "Offline for 6 min: no answer")
    assert title == "ALERT: Boiler room (12 Main St)"
    assert body.startswith("Offline for 6 min") and "Acme Housing · Main St · 12 Main St" in body
    assert alerts.message("recovered", _ctrl(), "Back to normal after 20 min.")[0].startswith("Recovered: ")


# ── the channels ─────────────────────────────────────────────────────────

@pytest.fixture
def ctx():
    a = flask.Flask("t")
    a.config.update(ALERT_NTFY_TOPIC="echotherm-abc123", ALERT_NTFY_SERVER="https://ntfy.example",
                    PORTAL_PUBLIC_URL="https://fleet.example.com/")
    with a.app_context():
        yield a


def test_ntfy_payload(ctx):
    with mock.patch.object(notify.requests, "post", return_value=mock.Mock(status_code=200)) as post:
        assert notify.send("ALERT: x", "Offline", urgent=True, path="/fleet/7") == "ntfy ok"
    url, kw = post.call_args[0][0], post.call_args[1]
    assert url == "https://ntfy.example"
    assert kw["json"] == {"topic": "echotherm-abc123", "title": "ALERT: x", "message": "Offline", "priority": 5,
                          "tags": ["rotating_light"], "click": "https://fleet.example.com/fleet/7"}
    assert "Authorization" not in kw["headers"]


def test_good_news_is_quieter(ctx):
    with mock.patch.object(notify.requests, "post", return_value=mock.Mock(status_code=200)) as post:
        notify.send("Recovered: x", "ok", urgent=False, good_news=True)
    assert post.call_args[1]["json"]["priority"] == 3 and post.call_args[1]["json"]["tags"] == ["white_check_mark"]


def test_a_failing_channel_is_reported_not_raised(ctx):
    with mock.patch.object(notify.requests, "post", return_value=mock.Mock(status_code=429)):
        assert notify.send("t", "b", urgent=True) == "ntfy failed: ntfy answered 429"


def test_email(ctx):
    ctx.config.update(ALERT_NTFY_TOPIC="", SMTP_HOST="smtp.example.com", SMTP_PORT="587", SMTP_USER="alerts@example.com",
                      SMTP_PASSWORD="pw", ALERT_EMAIL_TO="a@example.com, b@example.com")
    with mock.patch.object(notify.smtplib, "SMTP") as smtp:
        assert notify.send("ALERT: x", "Offline", urgent=True, path="/fleet/7") == "email ok"
    s = smtp.return_value
    s.starttls.assert_called_once()
    s.login.assert_called_once_with("alerts@example.com", "pw")
    msg = s.send_message.call_args[0][0]
    assert msg["Subject"] == "ALERT: x" and msg["To"] == "a@example.com, b@example.com"
    assert "https://fleet.example.com/fleet/7" in msg.get_content()
    assert s.send_message.call_args[1]["to_addrs"] == ["a@example.com", "b@example.com"]


def test_nothing_configured(ctx):
    ctx.config.update(ALERT_NTFY_TOPIC="")
    assert notify.channels() == {"ntfy": False, "email": False}
    assert notify.send("t", "b", urgent=True) == "no channel configured"
