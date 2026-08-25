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


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(f"Required environment variable {name} is not set (check .env)")
    return val
