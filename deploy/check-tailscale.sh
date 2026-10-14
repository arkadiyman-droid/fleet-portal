#!/usr/bin/env bash
#
# deploy/check-tailscale.sh — are the portal's Tailscale credentials good?
#
# Exchanges whatever is in .env for an access token and stops there. It
# does NOT mint an auth key, so it touches nothing in the tailnet and is
# safe to run whenever. Prints only the credential's prefix and length,
# never the secret.
#
#     bash ~/fleet-portal/deploy/check-tailscale.sh
#
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR" >&2; exit 1; }

.venv/bin/python - <<'PY'
import pathlib, re, sys

vals = {}
for line in pathlib.Path(".env").read_text().splitlines():
    if line.startswith(("TAILSCALE_OAUTH_CLIENT_ID=", "TAILSCALE_OAUTH_CLIENT_SECRET=")):
        k, v = line.split("=", 1)
        vals[k] = v.strip().strip('"').strip("'")

cid, sec = vals.get("TAILSCALE_OAUTH_CLIENT_ID"), vals.get("TAILSCALE_OAUTH_CLIENT_SECRET")
if not cid or not sec:
    sys.exit("Not configured: no Tailscale OAuth client in .env. "
             "Adoption still works; adopted modules are polled on their LAN address.")


def shape(v):
    m = re.match(r"(tskey-[a-z]+-)", v)
    return f"{m.group(1)}… ({len(v)} chars)" if m else f"{v[:4]}… ({len(v)} chars)"


print(f"client id : {cid}")
print(f"secret    : {shape(sec)}")

import requests
r = requests.post("https://api.tailscale.com/api/v2/oauth/token",
                  data={"client_id": cid, "client_secret": sec}, timeout=15)
if r.status_code == 200 and "access_token" in r.json():
    print("result    : OK — Tailscale accepted these credentials")
else:
    print(f"result    : REJECTED (HTTP {r.status_code}) {r.text[:160]}")
    print("\nThe portal needs an OAuth client with 'Auth Keys' WRITE scope and")
    print("tag tag:controller (Settings -> Trust credentials -> + Credential).")
    print("An API token or an auth key will be refused here.")
    sys.exit(1)
PY
