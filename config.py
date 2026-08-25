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


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set (check .env)")
    return val
