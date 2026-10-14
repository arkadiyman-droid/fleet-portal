#!/usr/bin/env bash
#
# deploy/set-tailscale-oauth.sh — give the portal its Tailscale OAuth client.
#
# With it, every module the portal adopts as a NEW site joins the fleet's
# tailnet by itself (app/tailscale_api.py).
#
# Make the credential in the Tailscale admin console:
#   Settings -> Trust credentials -> + Credential -> OAuth client
#   scope "Auth Keys" with WRITE, tag tag:controller
# Tailscale shows the secret once.
#
#     bash ~/fleet-portal/deploy/set-tailscale-oauth.sh
#     bash ~/fleet-portal/deploy/set-tailscale-oauth.sh --off    # remove them
#
# THIS SCRIPT DOES NOT JUDGE WHAT YOU PASTE. An earlier version matched the
# secret against a regex and refused anything else, which meant a format it
# had not been told about looked identical to a failed paste -- five
# rejections and "nothing changed", with no way to override. Tailscale
# issues several credential types and changes them over time, so the only
# check worth making is the real one: exchange the credentials with
# Tailscale and see if an access token comes back. That happens below,
# right after saving, and it is what actually tells you whether the
# rotation worked.
#
# Nothing lands in a command line or in shell history.
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$APP_DIR"
[[ -x .venv/bin/python ]] || { echo "no .venv in $APP_DIR" >&2; exit 1; }
[[ -r /dev/tty ]] || {
  echo "This needs an interactive terminal (it reads /dev/tty so nothing" >&2
  echo "reaches your shell history). Log in with plain 'ssh pi@...' and" >&2
  echo "run it there -- not piped, and not via ssh host 'bash ...'." >&2
  exit 1
}
touch .env && chmod 600 .env

MODE="${1:-}" .venv/bin/python - <<'PY'
import getpass, os, pathlib, sys

env = pathlib.Path(".env")
KEYS = ("TAILSCALE_OAUTH_CLIENT_ID=", "TAILSCALE_OAUTH_CLIENT_SECRET=")
keep = [l for l in env.read_text().splitlines() if not l.startswith(KEYS)]


def save(lines):
    tmp = env.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.chmod(0o600)
    tmp.replace(env)


if os.environ.get("MODE") == "--off":
    save(keep)
    sys.exit("Tailscale settings removed. Adopted modules are polled on their LAN address.")


def tty_read(prompt):
    # stdin is this script (python - <<PY), so talk to the terminal directly.
    with open("/dev/tty", "w") as out, open("/dev/tty") as inp:
        out.write(prompt)
        out.flush()
        return inp.readline()


def ask(prompt, hidden):
    """Accept anything non-empty. The only rejection is an empty line,
    which means the paste genuinely did not arrive."""
    for _ in range(5):
        v = (getpass.getpass(prompt) if hidden else tty_read(prompt)).strip()
        # A paste that brought quotes or a stray newline along is a typing
        # accident, not a different format — strip those rather than refuse.
        v = v.strip('"').strip("'").strip()
        if v:
            print(f"  Got it ({len(v)} characters).")
            return v
        print("  Nothing arrived. Copy it again in Tailscale, click this window,")
        print("  paste (Ctrl+Shift+V in most terminals, or right-click), then Enter.")
    sys.exit("Nothing pasted after 5 tries. Nothing changed.")


print("Paste the two values from the Tailscale admin console.")
print("The ID is shown as you type; the secret is hidden.\n")
cid = ask("OAuth client ID     : ", hidden=False)
sec = ask("OAuth client secret : ", hidden=True)
save(keep + [f"TAILSCALE_OAUTH_CLIENT_ID='{cid}'",
             f"TAILSCALE_OAUTH_CLIENT_SECRET='{sec}'"])
print("\nSaved to .env (chmod 600). Checking them against Tailscale...\n")

# ── the only check that means anything ──────────────────────────────────
os.environ["TAILSCALE_OAUTH_CLIENT_ID"] = cid
os.environ["TAILSCALE_OAUTH_CLIENT_SECRET"] = sec
try:
    import requests
    r = requests.post("https://api.tailscale.com/api/v2/oauth/token",
                      data={"client_id": cid, "client_secret": sec},
                      timeout=15)
except Exception as e:
    sys.exit(f"Could not reach Tailscale ({type(e).__name__}). The values are saved; "
             f"re-check with deploy/check-tailscale.sh once the network is back.")

if r.status_code == 200 and "access_token" in r.json():
    print("  OK — Tailscale accepted these credentials.")
    print("\nNow reload the portal:   sudo systemctl restart fleet-portal")
    print("Then revoke the OLD credential in the Tailscale console.")
else:
    body = r.text[:200].replace("\n", " ")
    print(f"  REJECTED by Tailscale (HTTP {r.status_code}): {body}")
    print("\n  The values are saved, but they do not work yet. Almost always this")
    print("  means the credential is the wrong KIND. The portal needs an")
    print("  OAuth client from:")
    print("      Settings -> Trust credentials -> + Credential -> OAuth client")
    print("      scope 'Auth Keys' with WRITE, tag tag:controller")
    print("  An API access token, an auth key (tskey-auth-...), or an OAuth")
    print("  client without Auth Keys write will all be refused here.")
    print("\n  Re-run this script with the right credential. The portal keeps")
    print("  using whatever is in .env, so fix it before restarting the service.")
    sys.exit(1)
PY
