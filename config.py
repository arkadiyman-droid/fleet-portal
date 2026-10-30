import os


class Config:
    def __init__(self):
        self.SQLALCHEMY_DATABASE_URI = _require_env("DATABASE_URL")
        self.SQLALCHEMY_TRACK_MODIFICATIONS = False
        self.SECRET_KEY = _require_env("SECRET_KEY")
        self.ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
        self.ADMIN_PASSWORD_HASH = _require_env("ADMIN_PASSWORD_HASH")
        self.PERMANENT_SESSION_LIFETIME = 60 * 60 * 24 * 30  # 30 days
        self.SESSION_COOKIE_HTTPONLY = True
        self.SESSION_COOKIE_SAMESITE = "Lax"

        # Fleet Portal status polling (Phase 2) — see app/fleet_poller.py.
        self.FLEET_TOKEN_ENCRYPTION_KEY = _require_env("FLEET_TOKEN_ENCRYPTION_KEY")
        self.FLEET_POLL_INTERVAL_SECONDS = int(os.environ.get("FLEET_POLL_INTERVAL_SECONDS", "60"))
        self.FLEET_POLL_TIMEOUT_SECONDS = int(os.environ.get("FLEET_POLL_TIMEOUT_SECONDS", "8"))
        self.FLEET_OBIX_PORT = int(os.environ.get("FLEET_OBIX_PORT", "8061"))
        self.FLEET_POLLER_ENABLED = os.environ.get("FLEET_POLLER_ENABLED", "1") not in ("0", "false", "False")

        # Tailscale for adopted modules — optional; see app/tailscale_api.py.
        # Unset = adoption polls the module's LAN address, as before.
        self.TAILSCALE_OAUTH_CLIENT_ID = os.environ.get("TAILSCALE_OAUTH_CLIENT_ID", "")
        self.TAILSCALE_OAUTH_CLIENT_SECRET = os.environ.get("TAILSCALE_OAUTH_CLIENT_SECRET", "")
        self.TAILSCALE_TAGS = os.environ.get("TAILSCALE_TAGS", "tag:controller")
        self.TAILSCALE_KEY_TTL_SECONDS = int(os.environ.get("TAILSCALE_KEY_TTL_SECONDS", "3600"))
        self.TAILSCALE_API_URL = os.environ.get("TAILSCALE_API_URL", "https://api.tailscale.com")

        # Fleet alerts — see app/alerts.py and app/notify.py. Alerts are always
        # decided and recorded; they are only SENT where a channel is set.
        self.ALERTS_ENABLED = os.environ.get("ALERTS_ENABLED", "1") not in ("0", "false", "False")
        # No nuisance alerts (the user, 2026-10-18: ~300 pushes a day, nearly all
        # "trip, then back to normal after 4 min", makes people switch it off):
        # a problem must last 10 minutes; "recovered" is sent only for problems
        # that lasted RECOVERED_MIN and says what cleared; a building with
        # FLAP_OPENS alerts in FLAP_HOURS gets one "keeps tripping" message and
        # is paused; no per-item reminders: one morning digest at DIGEST_HOUR.
        self.ALERT_AFTER_SECONDS = int(os.environ.get("ALERT_AFTER_SECONDS", "600"))
        self.ALERT_REPEAT_HOURS = float(os.environ.get("ALERT_REPEAT_HOURS", "0"))
        self.ALERT_RECOVERED_MIN_SECONDS = int(os.environ.get("ALERT_RECOVERED_MIN_SECONDS", "1800"))
        self.ALERT_FLAP_OPENS = int(os.environ.get("ALERT_FLAP_OPENS", "3"))
        self.ALERT_FLAP_HOURS = float(os.environ.get("ALERT_FLAP_HOURS", "6"))
        self.ALERT_DIGEST_HOUR = int(os.environ.get("ALERT_DIGEST_HOUR", "8"))       # New York time; -1 = off
        self.ALERT_NTFY_SERVER = os.environ.get("ALERT_NTFY_SERVER", "https://ntfy.sh")
        self.ALERT_NTFY_TOPIC = os.environ.get("ALERT_NTFY_TOPIC", "")
        self.ALERT_NTFY_TOKEN = os.environ.get("ALERT_NTFY_TOKEN", "")
        self.ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", "")
        self.SMTP_HOST = os.environ.get("SMTP_HOST", "")
        self.SMTP_PORT = os.environ.get("SMTP_PORT", "587")
        self.SMTP_USER = os.environ.get("SMTP_USER", "")
        self.SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
        self.SMTP_FROM = os.environ.get("SMTP_FROM", "")
        self.PORTAL_PUBLIC_URL = os.environ.get("PORTAL_PUBLIC_URL", "")

        # Installed 2010 gateways, read view-only for /live (app/legacy.py).
        self.LEGACY_POLLER_ENABLED = os.environ.get("LEGACY_POLLER_ENABLED", "1") not in ("0", "false", "False")
        self.LEGACY_POLL_INTERVAL_SECONDS = int(os.environ.get("LEGACY_POLL_INTERVAL_SECONDS", "300"))
        self.LEGACY_POLL_WORKERS = int(os.environ.get("LEGACY_POLL_WORKERS", "16"))   # boxes read side by side
        # Their alerts (app/alerts.py check_legacy): red on two polls in a row,
        # dark after LEGACY_DARK_MINUTES; when LEGACY_OUTAGE_SHARE of the boxes
        # go dark together it is the portal's own connection: one message.
        self.LEGACY_ALERTS_ENABLED = os.environ.get("LEGACY_ALERTS_ENABLED", "1") not in ("0", "false", "False")
        self.LEGACY_ALERT_AFTER_SECONDS = int(os.environ.get("LEGACY_ALERT_AFTER_SECONDS", "540"))   # 3 reads in a row
        self.LEGACY_DARK_MINUTES = int(os.environ.get("LEGACY_DARK_MINUTES", "30"))
        self.LEGACY_OUTAGE_SHARE = float(os.environ.get("LEGACY_OUTAGE_SHARE", "0.3"))
        # Their history (app/history.py): every read kept this many days (whole
        # months are dropped); hourly and daily summaries are kept for good.
        self.LEGACY_HISTORY_ENABLED = os.environ.get("LEGACY_HISTORY_ENABLED", "1") not in ("0", "false", "False")
        self.LEGACY_RAW_DAYS = int(os.environ.get("LEGACY_RAW_DAYS", "90"))

        # Bloom / Empowered Buildings, read view-only for the Skyline wall
        # (app/bloom.py). Credentials live per-account in the DB (BloomAccount),
        # encrypted with FLEET_TOKEN_ENCRYPTION_KEY above — no secret env here.
        self.BLOOM_POLLER_ENABLED = os.environ.get("BLOOM_POLLER_ENABLED", "1") not in ("0", "false", "False")
        self.BLOOM_POLL_INTERVAL_SECONDS = int(os.environ.get("BLOOM_POLL_INTERVAL_SECONDS", "60"))
        self.BLOOM_POLL_TIMEOUT_SECONDS = int(os.environ.get("BLOOM_POLL_TIMEOUT_SECONDS", "20"))

        # Billing (Phase 4) — see app/billing.py.
        self.BILLING_SCHEDULER_ENABLED = os.environ.get("BILLING_SCHEDULER_ENABLED", "1") not in ("0", "false", "False")
        self.BILLING_SCHEDULER_INTERVAL_SECONDS = int(os.environ.get("BILLING_SCHEDULER_INTERVAL_SECONDS", "86400"))

        # Stripe payment processor (Phase 5) — optional; the whole feature
        # degrades to manual/pay-by-hand invoicing when unset. See
        # app/stripe_billing.py.
        self.STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
        self.STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set (check .env)")
    return val
