#!/usr/bin/env bash
#
# deploy/backup-db.sh — nightly dump of the Fleet Portal database.
#
# Run by fleet-portal-backup.timer as pi (03:30, after Beverly's 03:15).
# Writes a custom-format pg_dump to ~/fleet-portal-backups, chmod 600, and
# keeps the newest 14. The database holds every controller's polling token
# (Fernet-encrypted with FLEET_TOKEN_ENCRYPTION_KEY from .env), so a dump
# alone cannot poll anything, but a dump plus .env can: keep both private.
# This is a LOCAL copy only; an off-site copy is still to be decided.
#
# Restore:  pg_restore --clean --if-exists -d "<DATABASE_URL without +psycopg>" <file>
set -euo pipefail
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="${FLEET_BACKUP_DIR:-$HOME/fleet-portal-backups}"
KEEP="${FLEET_BACKUP_KEEP:-14}"

url="$(sed -n "s/^DATABASE_URL=['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}$/\1/p" "$APP_DIR/.env" | tail -1)"
[[ -n "$url" ]] || { echo "no DATABASE_URL in $APP_DIR/.env" >&2; exit 1; }
url="${url/postgresql+psycopg:/postgresql:}"   # SQLAlchemy dialect -> libpq URL

umask 077
mkdir -p "$OUT_DIR"
file="$OUT_DIR/fleetportal_$(date +%Y-%m-%d_%H%M).dump"
pg_dump --format=custom --no-owner --file="$file.part" "$url"
mv "$file.part" "$file"
echo "wrote $file ($(du -h "$file" | cut -f1))"

# Keep the newest $KEEP, never touch anything else in the folder.
ls -1t "$OUT_DIR"/fleetportal_*.dump 2>/dev/null | tail -n +"$((KEEP + 1))" | xargs -r rm -f --
