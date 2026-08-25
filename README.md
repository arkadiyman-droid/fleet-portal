# EchoTherm Fleet Portal

Phase 1 of the fleet-management system for USE-Controller/EchoTherm installations: central data model and super-admin CRUD for Clients → Properties → Buildings → Controllers. See the full architecture plan and build order at the artifact linked from `project_ecosystem_gaps.md` in the EMS repo.

This is a separate, independent project from the EMS/USE-Controller Raspberry Pi codebase — no shared code, no shared database. The only future integration point is a fleet-proxied API call (Phase 6), not built yet.

## Setup (local dev / staging on a Pi)

1. Start Postgres:
   ```bash
   docker compose up -d
   ```
2. Create a virtualenv and install dependencies:
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate      # or .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```
3. Copy `.env.example` to `.env` and fill in the required values:
   ```bash
   cp .env.example .env
   python -c "import secrets; print(secrets.token_hex(32))"   # paste into SECRET_KEY
   flask hash-password                                         # paste the output into ADMIN_PASSWORD_HASH
   ```
4. Run migrations:
   ```bash
   flask db init      # only the first time this repo is set up
   flask db migrate -m "Initial schema: clients, properties, buildings, controllers"
   flask db upgrade
   ```
5. Run the app:
   ```bash
   flask run --host=0.0.0.0
   ```
6. Visit `http://<host>:5000/` — redirects to `/login`.

## What's built (Phase 1)

- Client / Property / Building / Controller tables with soft-delete (archive/restore), cascading archive down the tree.
- Single super-admin login (env-var credential, no user table yet).
- Plain server-rendered CRUD — no JSON API, no frontend framework.

## What's explicitly not built yet

- Tailscale connectivity / site status polling (Phase 2)
- Multi-user accounts, roles, permissions (Phase 3)
- Billing: Contracts, Invoices, Payments (Phase 4)
- Payment processor integration (Phase 5)
- Fleet-proxied control actions into the EMS repo (Phase 6)
