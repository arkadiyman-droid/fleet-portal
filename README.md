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
| — | **Fleet Overview dashboard** (the home page) | ✅ built 2026-10-13 |

**Not deployed anywhere yet.** Intended target is beverly-pi behind a Cloudflare
tunnel, alongside the existing violations portal. **First run anywhere:
2026-10-13**, as a development copy on the operator's laptop: all tests
(including the Postgres tier) pass, the schema migrates, login and the
Client → Property → Building → Controller screens work, and the poller reads
the bench controller.

### Fleet Overview

`/` opens `/overview`: tiles for controllers, online, offline, alarms active,
need attention, backup issues and updates waiting (each a filter), then one
card per controller, worst first. A card shows the building and client,
online/offline and when it was last heard from, the heating picture (outdoor,
apartment average against target, mode, calling for heat), the box (power,
CPU temperature, disk, uptime), the backup (config, history snapshot, site
bundle, each aged and coloured), the software version, whether it was
restored from a bundle, and the reasons for any amber or red, in plain words.
`/fleet/<id>` is the same controller in full, with its recent alarms.

What is green, amber or red is decided in one place, `app/fleet_health.py`,
with its thresholds as constants and its rules under test
(`tests/test_fleet_health.py`). Box health, backup and software state come
from the `fleetHealth` block EMS publishes in `/api/status` (EMS
`use_controller/fleet/health.py`, from 2026-10-13); a controller on older
software shows its heating data and "health not reported", never a false red.
`ControllerStatus.last_ok_at` records when a controller last answered, so an
offline one says how long it has been gone.

Fixed on the way: the poller looked the status row up BEFORE its network
call, so the background poll and a "Refresh now" click on a never-polled,
offline controller could both INSERT it and the page crashed on the primary
key. It now looks up after the call and retries a lost race as an update
(`tests/test_fleet_poller_db.py` reproduces the race).

### Adopt a module

A controller flashed with the golden image and built with the portal's
address (EMS `build_image.bat <ip> <portal-url>`) calls home when it has not
been restored from a bundle (EMS `use_controller/fleet/enroll.py`). It shows a
pairing code on its touchscreen. **Adopt** (in the nav, and a notice on the
overview whenever a module is waiting): type the code, pick the building and
a name, press Adopt. The page then follows four steps — found, assigned,
module confirmed, first status — and refreshes itself; the module hands over
its own polling token within about 15 seconds, so nobody types an address or
a token. Model: `Enrollment` (migration `d4dafc53716b`).

The three `/api/enroll/*` endpoints (`app/enroll_api.py`) are the only ones
reachable without a login. The module proves itself with a secret on every
call (only its hash is stored, as is the code's), a stranger cannot take
over a known module, the code is never shown by the portal, a module's adopt
page opens only after its code was typed in this session, and calls are
rate-limited. Behind Cloudflare Access those paths need a bypass policy:
see `deploy/README.md`. Tests: `tests/test_enroll_db.py`.

**Onto the fleet's Tailscale network, by itself.** With Tailscale settings
(`TAILSCALE_OAUTH_CLIENT_ID` / `_SECRET`, set with
`deploy/set-tailscale-oauth.sh`), adopting a module as a new site also puts
it on the tailnet: on the module's next check-in the portal asks Tailscale
for a one-time key for it (`app/tailscale_api.py`: single use,
pre-approved, tagged `tag:controller`, one hour if unused) and hands it over
in the `/api/enroll/status` answer. The module joins with a root helper (EMS
`tools/tailscale_join.sh`, key on stdin), reports its Tailscale address with
`/complete`, and the portal polls it there from then on, wherever the
building is. Tagged devices belong to the tag, not a person, so their keys
never expire and nobody turns expiry off by hand. If the portal cannot get a
key, the adopt page says why in one sentence and the module waits; a
restore needs none (the site's own Tailscale identity is in its bundle).
Without the settings, adoption polls the module's LAN address as before.
The key is stored encrypted until the module reports its address, then
dropped. Migration `a7c3e9d21f04`. Tests: `tests/test_enroll_tailscale_db.py`,
`tests/test_tailscale_api.py`.

Tailscale side, once: the tailnet policy lists `"tagOwners": {"tag:controller":
["autogroup:admin"]}`, and an OAuth client (Settings → OAuth clients) has the
**Auth Keys: Write** scope for `tag:controller` only.

**Restore an existing site onto the module** (the other choice on the same
page): pick the site being replaced (offline ones first), the site's bundle
file and the master key file. The page decrypts the bundle **in the
operator's browser**, shows the same checklist as EMS `restore_site.bat`,
re-encrypts it to the module's own age key and posts only that ciphertext
(`/adopt/<id>/restore`). The module takes it on its next check-in
(`/api/enroll/bundle`, after which the portal deletes its copy), stages it
with a root helper, reboots, and comes back as the site with that site's own
token, so the first good poll completes it. Until the module acts, **Cancel
and start over** undoes the choice. The master key never reaches the server.

The browser's age implementation is `app/static/js/agecrypt.js` (WebCrypto
for X25519, HKDF and HMAC; ChaCha20-Poly1305 written from RFC 8439, so no
third-party script runs next to the master key) and the bundle inspector is
`app/static/js/sitebundle.js`. `tests/js/agecrypt.test.mjs` checks the RFC
vectors, opens files made by the real age tool, refuses tampering, and reads
a bundle made by real GNU tar; `tests/test_agecrypt_js.py` runs it under
pytest (needs Node) and compares the AEAD with Python's on random inputs.
Checked once by hand: the real age 1.2.1 on a controller opened what this
code encrypted.

For development without hardware: EMS `tools/simulate_blank_module.py
--portal http://127.0.0.1:5050` runs the real enrollment code as a pretend
module and answers this portal's polls. With Tailscale settings it receives a
real one-time key but never uses it (it would join the laptop to the fleet);
it reports 127.0.0.1 as its Tailscale address so the portal keeps polling it.


### Software updates

The **Updates** page shows which release each controller runs and the
newest one its release branch offers, and rolls a release out:

1. pick the release, one **test controller**, a batch size and how long to
   watch each batch (defaults 10 and 30 minutes);
2. the test controller is asked first, alone (`POST /api/fleet/software` on
   the controller with the polling token: EMS `fleet/software.py`). It
   fetches the release itself from the official release branch with its own
   read-only key, installs it, and proves it for 10 minutes, rolling itself
   back if it crash-loops. The portal follows it through the controller's
   own report (`fleetHealth.ota`);
3. only when the whole batch is updated, confirmed and still out of the red
   after the watch time does the next batch start.

Any failed or rolled-back update, one that does not finish in 30 minutes,
a release branch that moved, or a controller turning red after updating
**halts** the rollout and sends an alert: a bad release reaches nobody else.
Offline controllers are retried for an hour and then skipped, as are sites
with remote updates switched off (`fleet.remote_update`) and controllers
too old to accept them (update those once by hand). Pause, resume and
cancel are on the page; a single controller updates from its own page.
The portal never rolls a fleet back by itself: that is putting the
previous release back on the release branch, deliberately.
`app/rollouts.py` (driven after each poll cycle, under the poller's lock),
models `Rollout`, `RolloutTarget` (migration `d9a1c7e3b5f2`), tests
`tests/test_rollouts_db.py`.

### Alerts

When a controller turns **red** on the Fleet Overview (the same rules:
offline, an active heating alarm, power failing, overheating, disk nearly
full, a backup failing) and **stays red for 5 minutes**, the portal sends an
**ALERT**; a **reminder** every 24 hours while it stays red; and
**RECOVERED**, with how long it lasted, when it is no longer red. Yellow
never pages anyone, and a single missed poll or a reboot never does either.
`app/alerts.py` decides (right after each poll cycle, inside the poller's
advisory lock, so nothing is sent twice); `app/notify.py` sends.

Channels, both optional: **phone push through ntfy** (the free ntfy app,
subscribed to the portal's private topic; no account) and **email** through
any SMTP account. Every decision is also kept on the **Alerts** page (menu,
with a red count while anything is in alarm), sent or not, with what each
channel answered, so nothing is lost before a channel is set up. **Send a
test alert** is on that page (administrator).

A technician on site mutes a controller from its own page for 4 hours, 24
hours or 3 days; an alert that recovers during a mute closes quietly.

Settings: `ALERT_AFTER_SECONDS` (300), `ALERT_REPEAT_HOURS` (24, 0 = never),
`ALERT_NTFY_TOPIC`, `ALERT_NTFY_SERVER` (https://ntfy.sh), `ALERT_NTFY_TOKEN`,
`ALERT_EMAIL_TO`, `SMTP_HOST`/`PORT`/`USER`/`PASSWORD`/`FROM`,
`PORTAL_PUBLIC_URL` (the link in each message), `ALERTS_ENABLED`.
Models `AlertState`, `AlertEvent` (migration `c4e8b2f9a1d7`). Tests:
`tests/test_alerts.py` (rules and channels), `tests/test_alerts_db.py`.

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
