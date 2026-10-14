#!/usr/bin/env bash
#
# deploy/set-admin-password.sh — set the portal's break-glass admin password.
#
# Asks for the password twice (nothing is echoed) and writes only its hash
# into .env as ADMIN_PASSWORD_HASH, replacing any earlier one. Nobody else
# needs to know the password, and it never appears on a command line or in
# a shell history. Run as the app user (pi), no sudo:
#
#     bash ~/fleet-portal/deploy/set-admin-password.sh
#
# Restart the service afterwards if it is already running:
#     sudo systemctl restart fleet-portal
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR — run the unprivileged half first" >&2; exit 1; }
touch .env && chmod 600 .env
.venv/bin/python - <<'PY'
import getpass, pathlib, sys
from werkzeug.security import generate_password_hash
pw = getpass.getpass("New admin password: ")
if len(pw) < 12:
    sys.exit("Use at least 12 characters.")
if getpass.getpass("Again: ") != pw:
    sys.exit("The two did not match. Nothing changed.")
env = pathlib.Path(".env")
lines = [l for l in env.read_text().splitlines() if not l.startswith("ADMIN_PASSWORD_HASH=")]
# Single-quoted: the hash contains "$", which a shell sourcing .env would
# expand. systemd, python-dotenv and bash all strip single quotes literally.
lines.append("ADMIN_PASSWORD_HASH='" + generate_password_hash(pw) + "'")
env.write_text("\n".join(lines) + "\n")
print("Admin password set (only its hash is stored, in .env).")
PY
