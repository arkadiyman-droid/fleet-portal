"""
tests/test_env_loading.py — who wins: the running environment or .env

On the server (FLEET_ENV_FILE_WINS=1 in .env) the file must win, so that an
edited .env takes effect on a reload; everywhere else the environment wins,
so a test's own DATABASE_URL is never replaced by the developer's .env.
"""
import os

from app import load_env


def test_the_environment_wins_by_default(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("PROBE_SETTING=from-file" + chr(10))
    monkeypatch.setenv("PROBE_SETTING", "from-environment")
    load_env(str(f))
    assert os.environ["PROBE_SETTING"] == "from-environment"


def test_on_the_server_the_file_wins(tmp_path, monkeypatch):
    f = tmp_path / ".env"
    f.write_text("FLEET_ENV_FILE_WINS=1" + chr(10) + "PROBE_SETTING=from-file" + chr(10))
    monkeypatch.setenv("PROBE_SETTING", "stale-from-the-last-restart")
    monkeypatch.delenv("FLEET_ENV_FILE_WINS", raising=False)
    load_env(str(f))
    assert os.environ["PROBE_SETTING"] == "from-file"
    monkeypatch.delenv("FLEET_ENV_FILE_WINS", raising=False)
