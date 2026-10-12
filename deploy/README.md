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

**Set the admin password yourself** — nobody else should know it:

```bash
cd ~/fleet-portal && FLASK_APP=wsgi.py .venv/bin/flask hash-password
# paste the output after ADMIN_PASSWORD_HASH= in .env
```

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

## 3. What the script cannot do

- **DNS.** As `pi`:
  `cloudflared tunnel route dns 97f95f60-3dee-4861-aaee-70d7cb5f8afb fleet.echothermcontrols.com`
- **Cloudflare Access.** Add an application for the hostname in the Cloudflare
  dashboard, as was done for the other hostnames on this tunnel. Until then the
  portal's own login page is the only thing in front of it, on a public name.

## Order matters

The database must exist before `flask db upgrade`, and `.env` must be complete
before the service starts. So: unprivileged half (minus `db upgrade`) → privileged
half → `db upgrade` → `sudo systemctl restart fleet-portal`.

## Checks

```bash
curl -sI http://localhost:8090/ | head -1        # 302 to /login
systemctl status fleet-portal --no-pager
journalctl -u fleet-portal -n 50 --no-pager
```

A `500` on first load with `FLEET_TOKEN_ENCRYPTION_KEY` in the traceback means
`.env` was incomplete when the service started — fix it and restart.
