#!/usr/bin/env bash
#
# deploy/setup-beverly-pi.sh — the privileged half of a beverly-pi deploy.
#
# Everything in here needs root: creating the Postgres role and database,
# installing the systemd unit, and adding the tunnel hostname. The
# unprivileged half (code, virtualenv, .env, migrations) is done
# separately and is assumed to have run already — see deploy/README.md.
#
# Run it as:   sudo bash deploy/setup-beverly-pi.sh
#
# It is idempotent: re-running it will not duplicate the role, the
# database, the systemd unit or the ingress entry.

set -euo pipefail

APP_DIR=/home/pi/fleet-portal
APP_USER=pi
DB_NAME=fleetportal
DB_USER=fleetportal
PORT=8090
HOSTNAME_FQDN="${FLEET_HOSTNAME:-fleet.echothermcontrols.com}"
# SKIP_TUNNEL=1: install and start the portal on 127.0.0.1 only, leave the
# Cloudflare tunnel config and the cloudflared service untouched. Publish
# later, once Cloudflare Access (and its /api/enroll/* bypass) is ready:
# re-run without it. Restarting cloudflared also blips every other hostname
# on this tunnel, so it is worth doing once, deliberately.
SKIP_TUNNEL="${SKIP_TUNNEL:-}"
CF_CONFIG=/etc/cloudflared/config.yml

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/setup-beverly-pi.sh" >&2
  exit 1
fi

if [[ ! -d "$APP_DIR" ]]; then
  echo "$APP_DIR does not exist — run the unprivileged half first." >&2
  exit 1
fi

echo "==> 1/4  Postgres role and database"
# The password is generated here and never leaves this host: it is written
# straight into the app's .env, which is chmod 600 and owned by pi.
if sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$DB_USER'" | grep -q 1; then
  echo "    role $DB_USER already exists — leaving its password alone"
  DB_PASS=""
else
  DB_PASS="$(head -c 32 /dev/urandom | base64 | tr -d '/+=' | head -c 32)"
  sudo -u postgres psql -qc "CREATE ROLE $DB_USER LOGIN PASSWORD '$DB_PASS';"
  echo "    created role $DB_USER"
fi

if sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$DB_NAME'" | grep -q 1; then
  echo "    database $DB_NAME already exists"
else
  sudo -u postgres createdb -O "$DB_USER" "$DB_NAME"
  echo "    created database $DB_NAME owned by $DB_USER"
fi

if [[ -n "$DB_PASS" ]]; then
  # Replace any existing DATABASE_URL line rather than appending a second.
  touch "$APP_DIR/.env"
  sed -i '/^DATABASE_URL=/d' "$APP_DIR/.env"
  echo "DATABASE_URL=postgresql+psycopg://$DB_USER:$DB_PASS@127.0.0.1:5432/$DB_NAME" >> "$APP_DIR/.env"
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
  chmod 600 "$APP_DIR/.env"
  echo "    wrote DATABASE_URL into $APP_DIR/.env (chmod 600)"
else
  echo "    NOTE: role already existed, so DATABASE_URL was NOT rewritten."
  echo "          If the app cannot authenticate, set that line by hand."
fi

echo "==> 2/4  systemd unit"
install -m 0644 "$APP_DIR/deploy/fleet-portal.service" /etc/systemd/system/fleet-portal.service
systemctl daemon-reload
systemctl enable fleet-portal.service >/dev/null
echo "    installed and enabled fleet-portal.service"

echo "==> 3/4  Cloudflare tunnel ingress"
if [[ -n "$SKIP_TUNNEL" ]]; then
  echo "    SKIP_TUNNEL set: tunnel config and cloudflared left untouched (local only)"
elif grep -q "$HOSTNAME_FQDN" "$CF_CONFIG"; then
  echo "    $HOSTNAME_FQDN already present in $CF_CONFIG"
else
  BACKUP="$CF_CONFIG.bak.$(date +%Y%m%d%H%M%S)"
  cp "$CF_CONFIG" "$BACKUP"
  # Read from the backup, write the original — the new entry goes before
  # the catch-all 404 rule, which cloudflared requires to stay last.
  awk -v host="$HOSTNAME_FQDN" -v port="$PORT" '
    /- service: http_status:404/ && !inserted {
      print "  - hostname: " host
      print "    service: http://localhost:" port
      inserted = 1
    }
    { print }
  ' "$BACKUP" > "$CF_CONFIG"
  if ! grep -q "$HOSTNAME_FQDN" "$CF_CONFIG"; then
    # No catch-all rule found, so nothing was inserted — put the original
    # back rather than leaving a config we did not actually change.
    cp "$BACKUP" "$CF_CONFIG"
    echo "    WARNING: no 'http_status:404' catch-all found in $CF_CONFIG;" >&2
    echo "             left it untouched. Add the ingress entry by hand." >&2
  else
    echo "    added $HOSTNAME_FQDN -> http://localhost:$PORT (backup: $BACKUP)"
  fi
fi

echo "==> 4/4  start"
systemctl restart fleet-portal.service
[[ -n "$SKIP_TUNNEL" ]] || systemctl restart cloudflared.service
sleep 3
systemctl --no-pager --lines=0 status fleet-portal.service | head -4 || true

cat <<EOF

Done on the host.${SKIP_TUNNEL:+ LOCAL ONLY (SKIP_TUNNEL): nothing is published yet; to publish, re-run without SKIP_TUNNEL.}
Two things remain and neither can be scripted here:

  1. DNS — point the hostname at this tunnel, as the pi user:
         cloudflared tunnel route dns 97f95f60-3dee-4861-aaee-70d7cb5f8afb $HOSTNAME_FQDN

  2. Cloudflare Access — add an application for $HOSTNAME_FQDN in the
     Cloudflare dashboard, as was done for the other hostnames on this
     tunnel. Until that is in place the portal's own login page is the
     only thing in front of it.

Check it locally first:   curl -sI http://localhost:$PORT/ | head -1
EOF
