#!/usr/bin/env bash
#
# deploy/set-tailscale-oauth.sh — give the portal its Tailscale OAuth client.
#
# With it, every module the portal adopts as a NEW site joins the fleet's
# tailnet by itself (app/tailscale_api.py). Make the client in the Tailscale
# admin console (Settings -> Trust credentials -> + Credential -> OAuth):
# scope "Auth Keys" with Write, tag tag:controller. Tailscale shows the
# secret once. Then run this as pi (no sudo) and paste the two values. The
# client id is shown as you paste it (it is not secret); the secret is not,
# but the helper says how many characters it received. Nothing lands in a
# command line or shell history.
#
#     bash ~/fleet-portal/deploy/set-tailscale-oauth.sh
#     bash ~/fleet-portal/deploy/set-tailscale-oauth.sh --off    # remove them
#
# Then reload the portal: sudo systemctl restart fleet-portal (or, as pi,
# kill -HUP the gunicorn master: new workers read .env).
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR" >&2; exit 1; }
touch .env && chmod 600 .env
MODE="${1:-}" .venv/bin/python - <<'PY'
import getpass, os, pathlib, re, sys
env = pathlib.Path(".env")
keep = [l for l in env.read_text().splitlines()
        if not l.startswith(("TAILSCALE_OAUTH_CLIENT_ID=", "TAILSCALE_OAUTH_CLIENT_SECRET="))]

def save(lines):
    tmp = env.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n"); tmp.chmod(0o600); tmp.replace(env)

if os.environ.get("MODE") == "--off":
    save(keep)
    sys.exit("Tailscale settings removed. Adopted modules are polled on their LAN address.")

def shown_input(prompt):
    # stdin is this script (python - <<PY), so read the terminal directly,
    # the way getpass does for the hidden one
    with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
        out.write(prompt); out.flush()
        return inp.readline()

def ask(prompt, check, hidden, what):
    for _ in range(5):
        v = (getpass.getpass(prompt) if hidden else shown_input(prompt)).strip()
        if not v:
            print("  Nothing arrived. Copy it again in Tailscale, click here, paste (Ctrl+V or right-click), then Enter.")
            continue
        if not check(v):
            print(f"  That does not look like the {what} ({len(v)} characters received). Try again.")
            continue
        print(f"  Got it ({len(v)} characters).")
        return v
    sys.exit("Giving up after 5 tries. Nothing changed.")

cid = ask("OAuth client ID (paste, then Enter): ",
          lambda v: re.fullmatch(r"[A-Za-z0-9_-]{4,100}", v), False, "client ID")
sec = ask("OAuth client secret (hidden; paste, then Enter): ",
          lambda v: re.fullmatch(r"tskey-client-[A-Za-z0-9_-]{8,200}", v), True, "client secret (tskey-client-...)")
save(keep + [f"TAILSCALE_OAUTH_CLIENT_ID='{cid}'", f"TAILSCALE_OAUTH_CLIENT_SECRET='{sec}'"])
print("Saved. The portal picks them up when it is reloaded.")
PY
