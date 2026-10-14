#!/usr/bin/env bash
#
# deploy/set-tailscale-oauth.sh — give the portal its Tailscale OAuth client.
#
# With it, every module the portal adopts as a NEW site joins the fleet's
# tailnet by itself (app/tailscale_api.py). Make the client in the Tailscale
# admin console (Settings -> OAuth clients -> Generate OAuth client): scope
# "Auth Keys" with Write, tag tag:controller. Tailscale shows the secret
# once. Then run this as pi (no sudo) and paste the two values; nothing is
# echoed, and nothing lands in a command line or shell history:
#
#     bash ~/fleet-portal/deploy/set-tailscale-oauth.sh && sudo systemctl restart fleet-portal
#
# Run it again to replace them; enter an empty client id to switch the
# feature off again.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR" >&2; exit 1; }
touch .env && chmod 600 .env
.venv/bin/python - <<'PY'
import getpass, pathlib, re, sys
cid = getpass.getpass("OAuth client ID (paste, then Enter): ").strip()
env = pathlib.Path(".env")
keep = [l for l in env.read_text().splitlines()
        if not l.startswith(("TAILSCALE_OAUTH_CLIENT_ID=", "TAILSCALE_OAUTH_CLIENT_SECRET="))]
if not cid:
    env.write_text("\n".join(keep) + "\n")
    sys.exit("No client id: the Tailscale settings were removed. Modules are polled on their LAN address.")
if not re.fullmatch(r"[A-Za-z0-9_-]{4,100}", cid):
    sys.exit("That does not look like an OAuth client ID. Nothing changed.")
sec = getpass.getpass("OAuth client secret (paste, then Enter): ").strip()
if not re.fullmatch(r"tskey-client-[A-Za-z0-9_-]{8,200}", sec):
    sys.exit("That does not look like an OAuth client secret (tskey-client-...). Nothing changed.")
tmp = env.with_suffix(".tmp")
tmp.write_text("\n".join(keep + [f"TAILSCALE_OAUTH_CLIENT_ID='{cid}'", f"TAILSCALE_OAUTH_CLIENT_SECRET='{sec}'"]) + "\n")
tmp.chmod(0o600); tmp.replace(env)
print("Saved. Restart the portal to use them: sudo systemctl restart fleet-portal")
PY
