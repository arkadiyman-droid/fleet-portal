# EchoTherm Fleet Portal

Central fleet-management system for USE-Controller/EchoTherm installations:
Clients → Properties → Buildings → Controllers, with scoped multi-user
permissions, read-only status polling from each site, and half-yearly billing.

**This is a separate, independent project from the EMS/USE-Controller Raspberry
Pi codebase** — no shared code, no shared database. EMS stays single-tenant with
no concept of clients or money. The two systems meet at exactly one point today:
the portal polls each controller's `GET /api/status` using a bearer token that
the controller mints itself (EMS `use_controller/fleet/token.py`).

---

## Status

| Phase | | |
|---|---|---|
| 1 | Data model + super-admin CRUD | ✅ built |
| 2 | Read-only status polling over Tailscale | ✅ built |
| 3 | Multi-user accounts, roles, scoped permissions | ✅ built |
| 4 | Billing — contracts, invoices, payments | ✅ built |
| 5 | Stripe payment processor | ✅ built |
| 6 | Fleet-proxied control actions into EMS | ❌ not built |

**Not deployed anywhere yet.** Intended target is beverly-pi behind a Cloudflare
tunnel, alongside the existing violations portal.


**Not built, beyond Phase 6:** utility-data ingestion (ConEd / National Grid /
DEP Green Button). See `UTILITY_DATA_INTEGRATION.md` in the EMS repo for the
research, endpoints and constraints — the conclusion there is that this pull
belongs in *this* service rather than on a controller.

---

## Requirements

**PostgreSQL, specifically.** SQLite is not a substitute and the schema will not
build on it:

- `ControllerStatus.raw_json` is `JSONB`.
- "At most one current contract per building" is a **partial** unique index
  (`WHERE end_date IS NULL`). On another dialect SQLAlchemy emits a plain unique
  index, which would forbid a building from ever having a second contract.
- Both schedulers coordinate across workers with `pg_try_advisory_lock`.

## Setup

1. **A database.** Either `docker compose up -d` for a throwaway local Postgres,
   or point `DATABASE_URL` at an existing server. (beverly-pi already runs
   PostgreSQL 17 natively — on that host, create a database rather than starting
   a second engine in Docker.)

2. **Virtualenv and dependencies:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate      # .venv\Scripts\activate on Windows
   pip install -r requirements.txt
   ```

3. **Environment.** Copy `.env.example` to `.env` and fill it in:
   ```bash
   cp .env.example .env
   python -c "import secrets; print(secrets.token_hex(32))"               # SECRET_KEY
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"   # FLEET_TOKEN_ENCRYPTION_KEY
   flask hash-password                                                    # ADMIN_PASSWORD_HASH
   ```
   `FLEET_TOKEN_ENCRYPTION_KEY` encrypts each controller's polling token at rest.
   **Rotating it does not re-encrypt existing rows** — stored tokens decrypt to
   `None` and must be re-entered, which is deliberate (a rotation degrades
   gracefully instead of crashing the page).

   Stripe is optional. With `STRIPE_SECRET_KEY` unset the whole payment feature
   degrades to manual pay-by-hand invoicing, which is the state it ships in.

4. **Migrations:**
   ```bash
   flask db upgrade
   ```
   The initial revision is committed, so every environment shares one schema
   history. Use `flask db migrate -m "..."` only when changing the models — and
   **always apply a generated migration before committing it.** `env.py` defines
   a `render_item()` hook so that a column using `EncryptedString` carries its
   own import; without it autogenerate emits the type but not the import, and the
   migration fails with `NameError` on a fresh database.

5. **Run:**
   ```bash
   flask run --host=0.0.0.0
   ```
   Visit `http://<host>:5000/` — redirects to `/login`.

---

## Tests

```bash
.venv/bin/pytest            # or .venv\Scripts\pytest on Windows
```

Two tiers, and the split is deliberate.

**Pure tests need no database** and run anywhere. They cover the billing
calendar and its boundaries, invoice terms, the settlement guard, Stripe method
mapping, the role ladder, and the settled policies asserted at schema and source
level. A suite that only runs on a fully-configured machine is a suite nobody
runs, so these are kept free of setup.

**Database tests need Postgres** and skip cleanly without it:

```bash
FLEET_TEST_DATABASE_URL=postgresql+psycopg://user:pw@host/fleet_test pytest
```

The database is dropped and recreated per test — **never point this at anything
real.** These cover what only Postgres can prove: that invoice generation is
idempotent via the `(building_id, period_start)` unique constraint, and that the
partial index permits many contracts per building but only one open one.

### A note on what some of these tests assert

Several policies in this system are *absences* — the strongest being **"non-payment
never restricts access"**, because the product runs live heating equipment and
nobody gets locked out mid-winter over an invoice. An absence cannot be tested by
exercising it, only by asserting the code has no way to express it. So
`tests/test_billing_policy.py` reads `permissions.py` and `admin_auth.py` and
fails if either so much as references `Invoice`, `Payment` or `Contract`, and
asserts no suspension flag exists on `Client`. Those tests are themselves guarded
by one that proves the source files are really being read, so they cannot pass
vacuously.

`pytest.ini` sets `python_classes = *` so test classes can be named for the
behaviour they assert rather than prefixed `Test`. The consequence is that model
and SQLAlchemy classes are imported as modules (`import app.models as m`) to keep
them out of the collected namespace.

---

## Architecture notes

- **Contracts are append-only.** A unit-count or rate change closes the old row
  (`end_date`) and opens a new one; existing rows are never mutated, so billing
  history stays accurate.
- **Invoices snapshot** `unit_count` and `rate_per_unit_cents` at generation, so
  a later contract change never retroactively alters an issued invoice.
- **Generation produces drafts only.** A superuser approves each one, and
  `issued_date`/`due_date` are set at approval — not at generation, which may sit
  in draft indefinitely.
- **"Overdue" is never stored**, only derived at render time as `pending` and
  `due_date < today`, so it cannot go stale against the clock.
- **One payment settles one invoice in full** — no partial-payment
  reconciliation. Manual entry and the Stripe webhook both funnel through
  `billing._finalize_payment()` so the two paths cannot drift, and the Stripe
  PaymentIntent id is unique so a retried webhook delivery cannot double-record.
