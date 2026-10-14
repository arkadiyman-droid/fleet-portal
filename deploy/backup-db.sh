#!/usr/bin/env bash
#
# deploy/backup-db.sh — nightly backup of the Fleet Portal: local, then off-site.
#
# Run by fleet-portal-backup.timer as pi (03:30, after Beverly's 03:15).
#
# 1. LOCAL: a custom-format pg_dump in ~/fleet-portal-backups (chmod 600),
#    newest 14 kept. The database holds every controller's polling token,
#    Fernet-encrypted with FLEET_TOKEN_ENCRYPTION_KEY from .env, so a dump
#    plus .env can poll the whole fleet: keep both private.
#
# 2. OFF-SITE (when FLEET_OFFSITE_REPO and FLEET_OFFSITE_RECIPIENT are set in
#    .env): the dump AND .env, tarred, encrypted to the fleet's master age
#    PUBLIC key (deploy/age_encrypt.py), committed to a private git repo and
#    pushed with a write deploy key for that repo alone
#    (FLEET_OFFSITE_SSH_KEY, default ~/.ssh/fleet_portal_backup). Only the
#    master PRIVATE key, kept in the vault, opens a copy; the repo, GitHub
#    and anyone who steals the Pi's key see ciphertext. The newest 30 stay in
#    the repo's tree; git history keeps the rest.
#
# Restore: deploy/README.md, "Restoring the portal".
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${FLEET_BACKUP_DIR:-$HOME/fleet-portal-backups}"
KEEP="${FLEET_BACKUP_KEEP:-14}"

envval() {   # a value from .env, quotes stripped (the file is never sourced)
  sed -n "s/^$1=['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}$/\1/p" "$APP_DIR/.env" | tail -1
}

url="$(envval DATABASE_URL)"
[[ -n "$url" ]] || { echo "no DATABASE_URL in $APP_DIR/.env" >&2; exit 1; }
url="${url/postgresql+psycopg:/postgresql:}"   # SQLAlchemy dialect -> libpq URL
# Password out of the URL and into PGPASSWORD: anything on a command line can
# be read by every local user through /proc, an environment cannot.
if [[ "$url" =~ ^(postgresql://[^:/@]+):([^@]*)@(.*)$ ]]; then
  export PGPASSWORD="${BASH_REMATCH[2]}"
  url="${BASH_REMATCH[1]}@${BASH_REMATCH[3]}"
fi

umask 077
mkdir -p "$OUT_DIR"
stamp="$(date +%Y-%m-%d_%H%M)"
file="$OUT_DIR/fleetportal_$stamp.dump"
pg_dump --format=custom --no-owner --file="$file.part" "$url"
mv "$file.part" "$file"
echo "wrote $file ($(du -h "$file" | cut -f1))"

# Keep the newest $KEEP, never touch anything else in the folder.
ls -1t "$OUT_DIR"/fleetportal_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --

# ── off-site ─────────────────────────────────────────────────────────────────
REPO="${FLEET_OFFSITE_REPO:-$(envval FLEET_OFFSITE_REPO)}"
RCPT="${FLEET_OFFSITE_RECIPIENT:-$(envval FLEET_OFFSITE_RECIPIENT)}"
SSHKEY="${FLEET_OFFSITE_SSH_KEY:-$(envval FLEET_OFFSITE_SSH_KEY)}"
SSHKEY="${SSHKEY:-$HOME/.ssh/fleet_portal_backup}"
CLONE="${FLEET_OFFSITE_DIR:-$HOME/fleet-portal-offsite}"
OFFSITE_KEEP="${FLEET_OFFSITE_KEEP:-30}"
if [[ -z "$REPO" || -z "$RCPT" ]]; then
  echo "off-site copy: not configured (FLEET_OFFSITE_REPO / FLEET_OFFSITE_RECIPIENT), local copy only"
  exit 0
fi
export GIT_SSH_COMMAND="ssh -i $SSHKEY -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes"
git_() { git -C "$CLONE" -c user.name="Fleet Portal backup ($(hostname))" -c user.email="noreply@echothermcontrols.com" "$@"; }

if [[ ! -d "$CLONE/.git" ]]; then
  git clone --quiet "$REPO" "$CLONE"
fi
git_ pull --quiet --ff-only 2>/dev/null || true      # an empty repo has nothing to pull

stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
cp "$file" "$stage/fleetportal.dump"
cp "$APP_DIR/.env" "$stage/env"
printf 'host=%s\ncreated=%s\nmigrations=%s\n' "$(hostname)" "$(date -Is)" \
  "$(ls "$APP_DIR"/migrations/versions/*.py 2>/dev/null | wc -l)" > "$stage/MANIFEST"
mkdir -p "$CLONE/backups"
out="$CLONE/backups/fleetportal_$stamp.tar.age"
tar -C "$stage" -cz fleetportal.dump env MANIFEST \
  | "$APP_DIR/.venv/bin/python" "$APP_DIR/deploy/age_encrypt.py" -r "$RCPT" > "$out.part"
mv "$out.part" "$out"

if [[ ! -f "$CLONE/README.md" ]]; then
  cat > "$CLONE/README.md" <<'MD'
# Fleet Portal backups (encrypted)

Nightly, from beverly-pi's `deploy/backup-db.sh`: each `backups/fleetportal_<date>.tar.age`
is the portal's database dump plus its `.env`, encrypted to the fleet's master age
public key. Nothing here is readable without the master PRIVATE key from the vault.

    age -d -i master.key backups/fleetportal_<date>.tar.age | tar -xz
    # -> fleetportal.dump, env, MANIFEST

Then follow "Restoring the portal" in the fleet-portal repo's deploy/README.md.
MD
fi
ls -1 "$CLONE"/backups/fleetportal_*.tar.age | sort -r | tail -n +"$((OFFSITE_KEEP + 1))" | xargs -r rm -f --
git_ add -A
git_ commit --quiet -m "backup $stamp"
git_ push --quiet origin HEAD
echo "off-site copy: pushed $(basename "$out") ($(du -h "$out" | cut -f1)) to $REPO"
