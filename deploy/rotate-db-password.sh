#!/usr/bin/env bash
#
# deploy/rotate-db-password.sh — give the portal's database role a new password.
#
# Run as pi, no sudo. The role changes its own password over its existing
# connection, so the new password never appears on a command line, in sudo's
# log or in `ps`. .env is then rewritten with the new DATABASE_URL.
# The running service still holds the old one, so restart it straight after:
#
#     bash ~/fleet-portal/deploy/rotate-db-password.sh && sudo systemctl restart fleet-portal
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR" >&2; exit 1; }
.venv/bin/python - <<'PY'
import pathlib, re, secrets, sys
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

env = pathlib.Path(".env")
lines = env.read_text().splitlines()
cur = [l for l in lines if l.startswith("DATABASE_URL=")]
if not cur:
    sys.exit("no DATABASE_URL in .env")
old = make_url(cur[-1].split("=", 1)[1].strip().strip("'\""))
new_pw = secrets.token_hex(24)

def connect(pw):
    return psycopg.connect(host=old.host, port=old.port or 5432, dbname=old.database,
                           user=old.username, password=pw, connect_timeout=5)

with connect(old.password) as c:
    c.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(
        sql.Identifier(old.username), sql.Literal(new_pw)))
with connect(new_pw) as c:                       # prove the new one works
    c.execute("select 1")

new_url = old.set(password=new_pw).render_as_string(hide_password=False)
out = [l for l in lines if not l.startswith("DATABASE_URL=")] + ["DATABASE_URL=" + new_url]
tmp = env.with_suffix(".tmp")
tmp.write_text("\n".join(out) + "\n"); tmp.chmod(0o600); tmp.replace(env)
print(f"Database password for role {old.username} rotated; .env updated.")
print("Restart now:  sudo systemctl restart fleet-portal")
PY
