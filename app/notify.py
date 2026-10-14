"""
app/notify.py — where fleet alerts go: phone push (ntfy) and email

Both channels are optional and independent; app/alerts.py calls send() and
records what each channel answered. Nothing here decides WHEN to alert.

ntfy (https://ntfy.sh, or a self-hosted server): the portal publishes to a
topic; anyone whose ntfy phone app is subscribed to that topic gets a push
notification. No account is needed. The topic name is the only secret, so it
is long and random (generated on the Alerts page's setup instructions);
ntfy.sh can read what passes through it, so messages carry building and
controller names and the problem, never tokens or addresses.

Email: any SMTP account (STARTTLS on 587, or TLS on 465).

Settings (.env): ALERT_NTFY_TOPIC, ALERT_NTFY_SERVER (default https://ntfy.sh),
ALERT_NTFY_TOKEN (optional, for a protected server); ALERT_EMAIL_TO (comma
list), SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, SMTP_FROM;
PORTAL_PUBLIC_URL for the link in each message.
"""
import smtplib
import ssl
from email.message import EmailMessage

import requests
from flask import current_app

TIMEOUT = 10


def _cfg(name, default=""):
    return current_app.config.get(name, default) or default


def channels() -> dict:
    """Which channels are configured: {"ntfy": bool, "email": bool}."""
    return {"ntfy": bool(_cfg("ALERT_NTFY_TOPIC")),
            "email": bool(_cfg("SMTP_HOST") and _cfg("ALERT_EMAIL_TO"))}


def link(path: str) -> str:
    base = _cfg("PORTAL_PUBLIC_URL").rstrip("/")
    return base + path if base else ""


def _ntfy(title, body, priority, tags, click):
    server = _cfg("ALERT_NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    payload = {"topic": _cfg("ALERT_NTFY_TOPIC"), "title": title, "message": body,
               "priority": priority, "tags": tags}
    if click:
        payload["click"] = click
    headers = {}
    if _cfg("ALERT_NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + _cfg("ALERT_NTFY_TOKEN")
    r = requests.post(server, json=payload, headers=headers, timeout=TIMEOUT)
    if r.status_code >= 300:
        raise RuntimeError(f"ntfy answered {r.status_code}")


def _email(title, body, click):
    msg = EmailMessage()
    msg["Subject"] = title
    msg["From"] = _cfg("SMTP_FROM") or _cfg("SMTP_USER")
    to = [a.strip() for a in _cfg("ALERT_EMAIL_TO").split(",") if a.strip()]
    msg["To"] = ", ".join(to)
    msg.set_content(body + (f"\n\nOpen it: {click}\n" if click else "\n"))
    host, port = _cfg("SMTP_HOST"), int(_cfg("SMTP_PORT", "587"))
    ctx = ssl.create_default_context()
    if port == 465:
        s = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT, context=ctx)
    else:
        s = smtplib.SMTP(host, port, timeout=TIMEOUT)
        s.starttls(context=ctx)
    try:
        if _cfg("SMTP_USER"):
            s.login(_cfg("SMTP_USER"), _cfg("SMTP_PASSWORD"))
        s.send_message(msg, to_addrs=to)
    finally:
        try:
            s.quit()
        except Exception:
            pass


def send(title: str, body: str, *, urgent: bool, good_news: bool = False, path: str = "") -> str:
    """Send on every configured channel. Returns one line saying what each
    did ("ntfy ok; email failed: ..."), or "no channel configured"."""
    ch = channels()
    click = link(path) if path else ""
    out = []
    if ch["ntfy"]:
        try:
            _ntfy(title, body, 5 if urgent else 3,
                  ["white_check_mark"] if good_news else (["rotating_light"] if urgent else ["warning"]), click)
            out.append("ntfy ok")
        except Exception as e:
            out.append(f"ntfy failed: {str(e)[:120]}")
    if ch["email"]:
        try:
            _email(title, body, click)
            out.append("email ok")
        except Exception as e:
            out.append(f"email failed: {type(e).__name__}: {str(e)[:120]}")
    return "; ".join(out) or "no channel configured"
