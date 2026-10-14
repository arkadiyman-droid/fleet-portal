# Deploying to beverly-pi

The deploy is split in two halves because only one of them needs root, and
keeping them apart means the privileged part is short enough to read before
running it.

**Target host:** beverly-pi (`192.168.1.99`). It already runs the Beverly
compliance platform on `:8000`, the TurboGen dashboard on `:8080`, PostgreSQL 17
natively, and a Cloudflare tunnel (`97f95f60-3dee-4861-aaee-70d7cb5f8afb`)
serving three hostnames. The portal takes **`:8090`**, bound to `127.0.0.1` so
the tunnel is the only way in.

It uses the **existing** Postgres instance, not the `docker-compose.yml` in this
repo — that file is for local development. A second database engine on a Pi
already running three services would be waste.

---

## 1. Unprivileged half — code, virtualenv, config, schema

Run as `pi`. No root needed.

```bash
# from a machine with the repo
tar --exclude=.git --exclude=.venv --exclude=__pycache__ -cz . \
  | ssh pi@192.168.1.99 'mkdir -p ~/fleet-portal && tar xz -C ~/fleet-portal'

ssh pi@192.168.1.99
cd ~/fleet-portal
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt gunicorn
```

Create `.env` (chmod 600). `DATABASE_URL` is written by the privileged half, so
leave it out here:

```bash
umask 077
cat > .env <<EOF
SECRET_KEY=$(python3 -c "import secrets;print(secrets.token_hex(32))")
FLEET_TOKEN_ENCRYPTION_KEY=$(.venv/bin/python -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())")
ADMIN_USERNAME=admin
ADMIN_PASSWORD_HASH=
FLASK_APP=wsgi.py
EOF
```

**Set the admin password yourself** — nobody else should know it. The helper
asks twice, echoes nothing, and writes only the hash into `.env`:

```bash
bash ~/fleet-portal/deploy/set-admin-password.sh
```

Run it again any time to change the password, then
`sudo systemctl restart fleet-portal`.

Then, **after** the privileged half has created the database and written
`DATABASE_URL`:

```bash
set -a; . ./.env; set +a
.venv/bin/flask db upgrade
```

## 2. Privileged half

```bash
sudo bash ~/fleet-portal/deploy/setup-beverly-pi.sh
```

Creates the Postgres role and database, writes `DATABASE_URL` into `.env`,
installs and enables `fleet-portal.service`, adds the tunnel ingress entry
(backing up `config.yml` first), and restarts both services. It is idempotent.

Override the hostname with `FLEET_HOSTNAME=... sudo -E bash ...`; the default is
`fleet.echothermcontrols.com`.

**Local only first.** `sudo SKIP_TUNNEL=1 bash ~/fleet-portal/deploy/setup-beverly-pi.sh`
does everything except the tunnel: `config.yml` and `cloudflared` are left
alone, so the Beverly, PATHSCO and TurboGen hostnames do not blip, and the
portal is reachable only on the Pi itself (or through an SSH tunnel:
`ssh -N -L 8090:127.0.0.1:8090 pi@192.168.1.99`, then http://localhost:8090).
Re-run without `SKIP_TUNNEL` when DNS and Cloudflare Access are ready.

## 3. What the script cannot do

- **DNS.** As `pi`:
  `cloudflared tunnel route dns 97f95f60-3dee-4861-aaee-70d7cb5f8afb fleet.echothermcontrols.com`
- **Cloudflare Access.** Add an application for the hostname in the Cloudflare
  dashboard, as was done for the other hostnames on this tunnel. Until then the
  portal's own login page is the only thing in front of it, on a public name.
- **Let blank controllers call home.** Access in front of the whole hostname
  would also stop new modules reaching `/api/enroll/hello`, `/status` and
  `/complete`, and nothing could ever be adopted. Add a **Bypass** policy for
  the path `/api/enroll/*` only (an Access application on
  `fleet.echothermcontrols.com/api/enroll/*` with action *Bypass*, Everyone).
  Those three endpoints are built to be open: every call carries the module's
  own secret, they are rate-limited, and they return nothing an operator has
  not chosen to give that module (`app/enroll_api.py`). Everything else stays
  behind Access.

## Order matters

The database must exist before `flask db upgrade`, and `.env` must be complete
before the service starts. So: unprivileged half (minus `db upgrade`) → privileged
half → `db upgrade` → `sudo systemctl restart fleet-portal`.

## Tailscale for adopted modules

The portal polls controllers over Tailscale, so beverly-pi itself is on the
tailnet (`tailscale up --hostname=beverly-pi --accept-dns=false`, key expiry
off in the admin console). To have every module adopted as a new site join
the tailnet by itself:

1. Tailscale admin console → **Access controls**: add
   `"tagOwners": {"tag:controller": ["autogroup:admin"]}` to the policy.
2. **Settings → OAuth clients → Generate OAuth client**: scope **Auth Keys**
   with **Write**, tag `tag:controller`. Tailscale shows the secret once.
3. On beverly-pi, paste the id (shown) and the secret (hidden; the helper confirms how many characters arrived):

   ```bash
   bash ~/fleet-portal/deploy/set-tailscale-oauth.sh
   ```

   then reload the portal: `sudo systemctl restart fleet-portal` (or, as pi,
   `kill -HUP $(systemctl show -p MainPID --value fleet-portal)`: the new
   workers read the new settings from `.env`).

Run it again to replace them, or with `--off` to remove them. It accepts
whatever you paste and then asks Tailscale whether the credentials work,
which is the only check that means anything. To re-verify later without
changing anything (no auth key is minted):

```bash
bash ~/fleet-portal/deploy/check-tailscale.sh
```
 The OAuth secret can
mint keys that join the fleet's tailnet; it lives only in `.env` (chmod 600).

## Alerts

Alerts are always decided and listed on the portal's Alerts page; they are
sent only where a channel is set in `.env` (reload the portal afterwards:
`kill -HUP` the gunicorn master as pi, or restart the service).

- **Phone (ntfy):** `ALERT_NTFY_TOPIC=<a long random name>` (and
  `PORTAL_PUBLIC_URL=https://fleet.echothermcontrols.com` for the link). On
  each phone: install the free ntfy app, tap +, subscribe to that topic. The
  topic name is the only secret; the Alerts page shows it to the
  administrator.
- **Email:** `SMTP_HOST`, `SMTP_PORT` (587 STARTTLS or 465 TLS), `SMTP_USER`,
  `SMTP_PASSWORD`, `SMTP_FROM`, `ALERT_EMAIL_TO` (comma list).

Check with **Send a test alert** on the Alerts page.

## Rotating the database password

```bash
bash ~/fleet-portal/deploy/rotate-db-password.sh && sudo systemctl restart fleet-portal
```

The role changes its own password over its existing connection; the new one
goes only into `.env`. Do this if the password was ever exposed. (Before
2026-10-14 the privileged half created the role with the password on psql's
command line, which sudo wrote into the system journal; it now uses stdin.)

## Backups

The privileged half installs `fleet-portal-backup.timer`: every night at 03:30
(after Beverly's 03:15) `deploy/backup-db.sh` runs as pi and

1. **locally** writes a `pg_dump` to `~/fleet-portal-backups` (chmod 600) and
   keeps the newest 14;
2. **off-site**, once configured, tars that dump together with `.env`,
   encrypts it to the fleet's **master age public key**
   (`deploy/age_encrypt.py`, standard age v1, checked against the real age
   tool) and pushes it to a private GitHub repository with a deploy key that
   can write to that one repository only. The newest 30 stay in its file
   list; git history keeps every night.

`.env` goes with the dump because a restored database is useless without
`FLEET_TOKEN_ENCRYPTION_KEY` (every controller's polling token is encrypted
with it). Encrypted to the master key, a copy is readable only with the
master private key from the vault: GitHub, the repository and a stolen Pi
see ciphertext.

Off-site settings in `.env`:

```
FLEET_OFFSITE_REPO=git@github.com:<owner>/echotherm-portal-backups.git
FLEET_OFFSITE_RECIPIENT=age1...        # the master PUBLIC key
# FLEET_OFFSITE_SSH_KEY=~/.ssh/fleet_portal_backup   (default)
```

The deploy key is `~/.ssh/fleet_portal_backup` on beverly-pi (its `.pub` is
added to the repository as a deploy key **with write access**). Run one now
with `bash ~/fleet-portal/deploy/backup-db.sh`.

## Restoring the portal

On a machine that has the master key (never copy it onto a Pi):

```bash
git clone git@github.com:<owner>/echotherm-portal-backups.git
age -d -i master.key echotherm-portal-backups/backups/fleetportal_<date>.tar.age | tar -xz
# -> fleetportal.dump, env, MANIFEST
```

On the new host: install the portal as above (unprivileged half, then the
privileged half, which creates an empty `fleetportal` database), then copy
the restored `env` over `.env` **keeping the new host's `DATABASE_URL`**
line, and load the dump:

```bash
pg_restore --clean --if-exists --no-owner -d "postgresql://fleetportal@127.0.0.1/fleetportal" fleetportal.dump
sudo systemctl restart fleet-portal
```

Every controller's token decrypts again because `FLEET_TOKEN_ENCRYPTION_KEY`
came back with `env`. Shred the plaintext `env` and dump afterwards.

## Checks

```bash
curl -sI http://localhost:8090/ | head -1        # 302 to /login
systemctl status fleet-portal --no-pager
journalctl -u fleet-portal -n 50 --no-pager
```

A `500` on first load with `FLEET_TOKEN_ENCRYPTION_KEY` in the traceback means
`.env` was incomplete when the service started — fix it and restart.
