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
# 3. HISTORY of the 2010 boxes (app/history.py) is too big to copy nightly:
#    every 5-minute read (legacy_readings*, kept 90 days anyway) is not
#    backed up at all, and the hourly summaries (legacy_hourly, ~0.5 GB a
#    year) go off-site ONCE PER FINISHED MONTH as
#    history/legacy_hourly_<YYYY-MM>.csv.gz.age, written once and never
#    rotated. The daily summaries and the event log are small and stay in
#    the nightly dump. A restore loses at most the current month's hourly
#    rows (the daily rows for it survive).
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
# Schema of everything, but not the data of the big history tables (see 3.).
pg_dump --format=custom --no-owner \
  --exclude-table-data='public.legacy_readings*' --exclude-table-data='public.legacy_hourly' \
  --file="$file.part" "$url"
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

`history/legacy_hourly_<YYYY-MM>.csv.gz.age`: the 2010 boxes' hourly summaries, one
file per finished month (not in the nightly dumps). After restoring the dump:

    age -d -i master.key history/legacy_hourly_2026-11.csv.gz.age | gunzip > h.csv
    psql "$DATABASE_URL" -c "\copy legacy_hourly FROM 'h.csv' WITH CSV HEADER"
MD
fi
ls -1 "$CLONE"/backups/fleetportal_*.tar.age | sort -r | tail -n +"$((OFFSITE_KEEP + 1))" | xargs -r rm -f --

# Hourly history: one file per finished month, written once (see 3.).
first="$(psql "$url" -At -c "SELECT to_char(min(hour), 'YYYY-MM') FROM legacy_hourly" 2>/dev/null || true)"
if [[ "$first" =~ ^[0-9]{4}-[0-9]{2}$ ]]; then
  mkdir -p "$CLONE/history"
  this="$(date -u +%Y-%m)"
  m="$first"
  while [[ "$m" < "$this" ]]; do
    next="$(date -u -d "$m-01 +1 month" +%Y-%m)"
    arch="$CLONE/history/legacy_hourly_$m.csv.gz.age"
    if [[ ! -f "$arch" ]]; then
      psql "$url" -At -c "\copy (SELECT * FROM legacy_hourly WHERE hour >= '$m-01' AND hour < '$next-01' ORDER BY hour, box_id) TO STDOUT WITH CSV HEADER" \
        | gzip -9 | "$APP_DIR/.venv/bin/python" "$APP_DIR/deploy/age_encrypt.py" -r "$RCPT" > "$arch.part"
      mv "$arch.part" "$arch"
      echo "history: archived $(basename "$arch") ($(du -h "$arch" | cut -f1))"
    fi
    m="$next"
  done
fi
git_ add -A
git_ commit --quiet -m "backup $stamp"
git_ push --quiet origin HEAD
echo "off-site copy: pushed $(basename "$out") ($(du -h "$out" | cut -f1)) to $REPO"
