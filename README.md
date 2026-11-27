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
| 3 | Multi-user accounts, roles, scoped permissions, view-only "My properties" | ✅ built |
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

### View-only accounts ("My properties")

How a knowledgeable person outside the company is given a look at their own
buildings and nothing else.

**Setting one up** (the administrator, under **Users**):

1. **New user** — name, email, login and a first password. The page goes
   straight on to what that person can see.
2. **Properties … can watch** — tick the properties to share and press **Save
   view access**. Each tick is a `viewer` grant on that property; unticking
   takes it away. The grants below the tick list are for anything finer (one
   building, a whole client, or operator/admin rights) and are left alone by
   the ticks. The Users list shows what each person can see.
3. The site sits behind Cloudflare Access, so the person's email also has to
   be allowed in the Access policy for the portal's hostname before the
   portal's own login page is reachable.

**What the person gets** — a short menu: **My properties**, **Fans**,
**Alerts**, and their own name (their account page, where they can change the
password they were given).

- **My properties** (`/home`, `app/home.py`): each shared property with every
  building's heating status — outdoor, supply, return, hot water, setpoint,
  run time, alarms — whatever reads the building (EchoTherm controller, Bloom
  or a 2010 box), and the property's roof fans. A building opens its own page
  with readings, charts, history and CSV export.
- **Fans**: the same Fans page the administrator has, limited to their
  buildings, with the schedule/switching, push, raw-values and Bloom links
  removed.
- **Alerts**: only their buildings' alerts and alert history.

**What they never get** — anything that changes something (hide/delete/mute a
box, fan schedules or automatic switching, filing, users, billing, updates),
a box's network address, and any building not shared with them. The two
exceptions are ticks the administrator gives per property on the same page:
**may run the fans here** and **may change heating settings here** (see
*Changing settings, and the settings ledger* below).

The rule is in one place per kind of thing: `permissions.can_view_building`
(a grant of any role on the building, its property or its client), applied by
`routes._live_box_or_404`, `_bloom_asset_or_403`, `_fan_rows` /
`_fan_site_or_403` and `_visible_box_ids`. A reading that is not filed under
a building belongs to the administrator alone, so **file a building in
Portfolio before sharing its property**. `tests/test_viewer_access_db.py`
signs in as a view-only person and tries every page and data feed.

### Changing settings, and the settings ledger

**Changing a building's settings** (`/skyline/<id>/settings`,
`app/bloom_settings.py`, `routes.skyline_settings`). A Bloom heating building's
page has a **Change settings…** link: day and night indoor setpoints, the
outdoor cutoffs, when the day and night periods start, the heating season's
dates and the secondary call-for-heat rule — the settings of Bloom's own
Schedule screen, with Bloom's own limits. The building is read live each time;
**Review changes** shows old → new and sends nothing; a second, confirming
submit with a reason sends it (`bloom.put_points`), and only if Bloom still
holds what the preview showed. It is read back and logged
(`bloom_setting_writes`). A setting Bloom lists twice on a building is not
offered there.

The poller does **not** store Bloom's whole building object. `bloom.slim()`
keeps only what the portal reads from `BloomStatus.data`:

- the building's `_id`, `locid`, `name` and `accounts`;
- each sensor's `type`, `pv`, `name`, `assoc`, `stats` and `pollTs`;
- the `_id` of settable points, which the Settings link needs.

Bloom sends about 24 KB a building, and the row is rewritten every minute, so
the full object was ~0.4 GB a day of SD-card writes. Slimming halves what is
stored. Settings writes are unaffected: they always fetch the building live.
A field newly read from the stored copy must be added to `_KEEP_SENSOR` or
`_KEEP_BUILDING`; `test_the_stored_copy_is_slim_and_reads_the_same` checks
the pages built from it.

**The other kinds of box** have the same page, reached from **Change
settings…** in the *Settings changes* card of the box's own page, and built on
one shared form (`app/box_settings.py`, `templates/box_settings.html`): the
box's settings are read live with the box's own ranges, **Review changes**
shows old → new, a confirming submit with a reason sends only what was shown,
it is read back, and the ledger records who and why.

- **EchoTherm controllers** (`/fleet/<id>/settings`,
  `routes.fleet_controller_settings`). The controller itself lists what the
  portal may change — ten everyday comfort settings: indoor target, night
  setback, outdoor cutoffs, day and night start, the season dates — and takes
  a change only where its site has switched that on (the controller's own
  dashboard: Settings → Fleet Portal → *Let the Fleet Portal change comfort
  settings*; off by default). Elsewhere the page is read-only and says so. The
  controller refuses anything off its list, anything outside a setting's
  range, and a setting that moved since the person was shown it; it logs the
  change with the person's name, and the ledger reads that back.
- **2010 oBIX boxes** (`/live/<id>/settings`, `routes.live_box_settings`,
  `app/legacy_settings.py`). Outdoor cutoffs, day and night hours, the heating
  season, the two heat-off points and the **hourly indoor targets** (weekdays,
  Saturday, Sunday × 24) — the table that is the temperature a building is
  actually heated to on these boxes. The write follows the gateway's own
  source and Windows client: a `PUT` of only the changed items, by name, no
  wrapper. Because the box clamps silently and rewrites whole blocks of its
  memory from the gateway's copy, the page checks every value against the
  box's own range first, closes every connection at once (the gateway serves
  one at a time and drops the next while one is open), refuses to write to a box that started minutes ago or
  whose settings read back as nonsense, holds the poller's lock so nothing
  else talks to the box meanwhile, requires the box's reply to carry exactly
  what was sent, and reads back afterwards (also reporting any *other*
  setting that moved). The module's docstring is the reference for why each
  guard exists.

**Who may** — the administrator, and any person given the **may change heating
settings here** tick for the building's property. The tick is offered on every
property, and can be set from either side: on a person's own permissions page
(all their properties), or on a property's **People** page (Properties →
People: everyone under that property — through a grant on it, on its client,
or on one of its buildings — each with *sees the property*, *runs the fans*
and *changes heating settings*; a person can be added from there too). It is
the same tick on both pages
(`models.SettingControl`, `permissions.can_change_settings`). The tick sits
beside the fan tick, works the same way (it needs the view of the property and
goes when the view goes), and every change is recorded under that person's
own name.

**The settings ledger** (`app/setting_audit.py`, table `setting_changes`,
page **Changes**). One row is one setting of one building going from one value
to another: when, from what, to what, and who.

- A change sent through the portal is recorded at once with the person and
  their reason.
- A change made anywhere else — the vendor's own site, an app, the keypad —
  is caught by comparing each read of a box's settings with the read before,
  and recorded as changed *outside the portal*. When that difference is just a
  portal change arriving at the controller, the portal's row is marked as seen
  there instead of a second row being written.
- Only settings are compared, never readings or values a controller computes
  for itself, and a row is written only when one changes — so a building
  nobody touches adds nothing.

Where to read it: **Changes** in the administrator's menu (the whole fleet,
filtered by period, by through/outside the portal, and searched by building,
setting, person or reason) and the **Settings changes** card on a building's
own page.

What is watched today:

| Kind of box | Settings compared | How often |
|---|---|---|
| Bloom heating buildings | the 13 Schedule settings + the two hot-water alarm limits | every poll (about a minute) |
| 2010 oBIX boxes | every field of `emsSettings`, `emsConfig`, `DITs`, `DTs`, `hotWaterHeat`, `vacuumSteam`, `aquastat`, `cfhtable` except clock stamps | each box in its turn, about once an hour (`LEGACY_SETTINGS_EVERY_MIN`, 0 = off) |
| EchoTherm controllers | every setting the controller holds, read with the fleet token from its `GET /api/fleet/settings` (credentials arrive only as "set" / "not set") | every 5 minutes (`FLEET_SETTINGS_EVERY_MIN`, 0 = off) |

An EchoTherm controller also says **who** made a change at the controller —
the dashboard login, an API key by name, an oBIX account, or the controller's
own screen — so those rows carry a name even though they did not go through
the portal. A controller running software older than that route is simply not
compared until it is updated.

Roof-fan schedule switching keeps its own log on the Fans pages (automatic
switching would bury everything else here).

**Adding a kind of box** (the next vendor) takes three
things, listed at the top of `app/setting_audit.py`: a flat snapshot of its
settings, a `describe()` that puts a setting in words, and one `track()` call
in its poller. The ledger, the Changes page and the building cards take every
kind as they are. A kind the portal can also write to calls `record_portal()`
when a person sends a change.

### Subscriptions: owners pay by the apartment unit, card on file

`app/subscriptions.py`. One Stripe subscription per owner (Client). Its
quantity is the owner's apartment units at a price per unit for the interval
chosen: **monthly** (default $0.85 per unit) or **quarterly** (default $2.50
per unit). The prices are set on the Billing page; new ones apply to new
subscriptions at once and, if asked, to existing ones from their next renewal,
never mid-period.

**Units are set per property** on **Billing → Unit counts**
(`routes.billing_units`), because a "building" here is often one boiler zone of
a larger building (north/south halves, high/low zones). Each property row shows
its units, where the number came from, and the violations portal's figure as a
one-click suggestion; all are saved at once. A property with no count of its
own is billed by its buildings' contract counts (Buildings → Contract) instead
— never both (`subscriptions.property_units`). Every change is kept in
`property_unit_changes` (who, when, from → to, source), and an owner already
subscribed is billed the new total, prorated on its next charge.

**The payer's side.** A person given *may manage … subscription and payment*
for an owner (Users → Permissions; `models.BillingControl`, needs no other
right) gets **Billing** in their menu: the owner's properties, units and both
prices. The main button, **Subscribe — pay by bank account**, opens Stripe
Checkout for a **US bank account (ACH debit)** only; the smaller **Pay by card
instead** opens one for cards only (Apple Pay and Google Pay come with cards).
The bank account is the main way on purpose: Stripe charges 0.8% capped at $5
for ACH against 2.9% + 30¢ for a card, so on a quarterly bill of thousands of
dollars it saves hundreds (`subscriptions.CHECKOUT_METHODS`). One method per
Checkout because Stripe re-orders a mixed list itself (it put card above the
bank account when both were offered), and naming the methods keeps Klarna,
Cash App Pay and other shopper methods off a business subscription. Stripe
keeps the method used on file and charges it automatically each period. **Manage card and invoices** opens
Stripe's own customer page (card on file, invoices and receipts, cancel at the
end of the paid period). Monthly ↔ quarterly is switched on the owner's page:
the unused part of the period is credited and the new one charged. No card
number ever passes through the portal.

**Staying right.** Stripe is the source of truth. The local row is refreshed
from Stripe — never from what a browser says — on return from Checkout, on
every webhook (`/webhooks/stripe`), when the billing page is opened, and daily
in the billing scheduler, which also bills each owner for the units it has
now (a unit count change is prorated on the next charge). So it keeps working
while Stripe's webhooks cannot reach the portal behind Cloudflare Access.

**To switch it on:** a Stripe account; in Stripe, the payment methods wanted
(Settings → Payment methods); `STRIPE_SECRET_KEY` in `.env`; for instant
updates, a webhook endpoint `https://<portal>/webhooks/stripe` (snapshot
payload) for `checkout.session.completed`, `checkout.session.async_payment_succeeded`,
`customer.subscription.created/updated/deleted`, `invoice.paid`,
`invoice.payment_succeeded`, `invoice.payment_failed` and `invoice.finalized`
(the handler takes only ids from an event and re-reads the object from Stripe,
so the endpoint's API version does not matter), its signing secret as
`STRIPE_WEBHOOK_SECRET`, and a Cloudflare Access bypass for that one path.
Test with Stripe's test keys and test card first; a test event can be sent
from Stripe's dashboard with no software installed (Workbench → Shell →
`stripe trigger customer.subscription.created`). The half-yearly invoices
(below) keep working for owners who pay by invoice.

### Directory: managers from the violations portal

PATHSCO Violations (a separate application on the same server) already
records who manages each property, by email. **Directory** (administrator
only, `app/directory.py`, `routes.directory_page`) turns that into view-only
access here, in three steps on one page:

1. **Which property is which.** The two systems name properties differently
   ("Bedell" / "Bedell Terrace"; "Betty" and "Medgar Evers" are one property
   here). The page suggests the match from the name and the streets; the
   administrator confirms. Several of theirs may be one property here.
2. **Who would see what.** Every property manager and regional manager on a
   linked property, with the properties they would be able to watch. Ticking
   and approving creates the account and shares those properties as `viewer`.
   Nobody gets access without that approval. An address outside
   `DIRECTORY_EMAIL_DOMAINS`, or a mistyped one, cannot be approved.
3. **No longer in the directory.** When the violations portal stops listing
   someone for a property, the page offers to remove it — never by itself, and
   never a grant given by hand under Users (`permissions.source` tells them
   apart).

The directory is re-read when the page is opened and the last read is over
six hours old, or on the button. It comes from the violations portal's
`GET /api/v1/directory` with a read-only token minted on that portal's
Integrations page:

    DIRECTORY_URL=http://127.0.0.1:8000
    DIRECTORY_TOKEN=bev_...

**Sign-in by company email** (`app/cf_access.py`, `app/admin_auth.py`). A
manager approved here has no portal password. Cloudflare Access has already
verified their email before any request arrives, and says so in a signed
token; the portal checks that token (signature against the team's published
keys, this application's audience, issuer, expiry) and signs them in as the
account with that email. It needs:

    CF_ACCESS_TEAM_DOMAIN=<team>.cloudflareaccess.com
    CF_ACCESS_AUD=<this application's audience tag>

and the managers' email domain allowed in this site's Access policy. The
Directory page says whether the token on the administrator's own request
verifies, so a wrong audience tag shows up before anyone is invited. Log out
is respected (the next page does not sign the person straight back in), and
accounts with a password keep working as before.

### Reloads and the background readers

A deploy reloads the portal (`kill -HUP`): gunicorn starts new workers and the
old ones exit. An exiting Python process waits for every thread pool to finish
everything queued on it. The 2010-box reader queues about 186 boxes on 16
threads, so a reload landing mid-round made the old worker run the rest of the
round first, and once that took over gunicorn's 60 s limit and the worker was
killed. `app/shutdown.py` raises a "stopping" flag at the very start of the
exit, before Python waits for the pools:

- boxes still queued skip themselves;
- a box being read stops after the request it is waiting on (at most
  `legacy.TIMEOUT`, 30 s);
- the cut-short round saves nothing (the new worker reads every box again
  within minutes);
- the fleet, Bloom, hot-water, billing and server-sampler loops start no new
  round.

Measured: a child process with 40 queued half-second jobs exits at once with
the flag, and after 21 s without it.

### Abnormal boxes

`app/abnormal.py` flags a 2010 box doing something its own settings cannot
explain. Each finding is a warning: it gives the row an amber stripe, appears
in the building's issue panel and on its own page, and the Live wall gets an
**Abnormal** filter. It is never critical on its own. The checks:

- **Indoor target (DIT) not from the box's tables.** The box's reported
  `targetInsideTemperature` is compared with what its own hourly tables give.
  That follows the firmware: weekday, Saturday or Sunday per `useHolidayDITs`
  and `dayStartHour`, then Auto-DIT (`autoDITAdjHigh`/`Low` past
  `autoDITTempHigh`/`Low`), the hour either side allowed, 1° of slack. On a
  vacuum-steam box (type 5) whose gap equals `vacuumSteam.dccCompensation`,
  the warning says so. Found 2026-11-26: 801 South St reported 150° / 97°
  for weeks because a stray DCC compensation (+79 / +26, outside the box's own
  -20..20) was added to a 70-71° table. The boxes never reached Inside
  Satisfied and overheated the apartments.
- **DCC compensation** set on a vacuum-steam box. Other boxes hold junk in
  this field and ignore it.
- **Hourly target table** with an hour outside 60-80°, and **every day on
  the Saturday or Sunday table** (`useHolidayDITs` 2 / 3).
- **Outdoor cutoff** outside 40-70°, and a **heat-off point** below the
  highest sensible hourly target (heat stops before the apartments get
  there).
- **Summer mode in November-March**, by the box's own season dates (the
  firmware's test) or its status.
- **Clock** 15 minutes or more off: its hourly schedule runs at the wrong
  times.
- **Unknown heating-system type.**
- **Overheating:** the apartments averaged 5° or more over target, never
  under 3° over, for the last 3 hours, while the burner ran 30 minutes or
  more. This reads the stored readings in one query.

The settings come from the hourly snapshots (`setting_snapshots`). Only the
~90 keys the checks use are read, and they are kept 5 minutes per worker.
A box that is not answering is not checked, since its last read may be stale.
When added, the fleet showed no false alarms from the settings checks, and 3
overheating buildings.

### Live page speed

The Live wall reloads every minute in every open tab, so its build time is
what visitors feel. Its slowest part was the **Heat o/n** column: reading back
the whole night for every building took 3-5 s on the Pi. `heat_overnight`
now caches each night's verdicts per worker. Once the night is over (9 am plus
20 minutes for late reads) the verdict cannot change and is kept until 9 pm.
While the night is still running it is reworked at most every 3 minutes
(`OPEN_TTL_S`). It also reads only the columns the verdict uses. Each box's
status loads in the same query as the box. The page itself draws the flame
icon once (`<symbol id="lv-flame">`) and sends every row's issue panel as one
JSON list (`#lv-issues`, rows point into it by `data-i`) instead of an escaped
copy inside each row. Result on the Pi, 2026-11-26: 6 s down to about 1.2 s
(1.7 s on a worker's first load of the night), page 529 KB down to 435 KB.

**Portfolio and My properties** build one entry per building
(`portfolio.building_entry`). Looking up each building's controller, Bloom
asset, 2010 box and status one at a time cost about 900 queries (3 s on the
Pi). `portfolio.drivers()` now fetches every Bloom asset and 2010 box with
its status in two queries, and the buildings come with their controllers
already loaded. Portfolio dropped from 4.2 s to about 0.3 s, with the same
entries for all 225 buildings, checked against live data.

**The other slow pages** (measured on the Pi, 2026-11-26):

- **Sensors** read three days of every box (about 150,000 reads) and walked
  them on every visit, taking about 5 s. The walk is faster now (no function
  call per read, and no frozen-run search on a channel that never ran hot).
  The read sorts in memory (`SET LOCAL work_mem`). It found the same 42
  faults as before on live data. The answer is also reused: under 5 minutes
  old it is shown as is; under 30 minutes it is shown at once and worked out
  again in the background; older, it is worked out while the visitor waits
  (about 3.7 s, normally only the first visit after a reload). The page says
  when it was checked.
- **Analytics** fetched each box's status one at a time (180 queries).
  The status now comes with the box, and the cold-apartment detail reads
  only the columns it uses. It went from 1.4-1.9 s to about 0.9 s.
- **Buildings** looked up each building's open contract and property
  separately (258 queries). Now 4 queries, 0.7 s down to 0.1 s.
- **Users** asked twice per person who runs fans or changes settings.
  `_control_names()` gets both lists for everyone at once: 137 queries down
  to 56, 0.4 s down to 0.2 s.

**Compression.** Every page leaves through a Cloudflare tunnel over the
server's own internet link. Cloudflare compresses for the visitor, but only
after the page has crossed that link at full size. `app/compress.py` now
gzips pages and JSON before they leave (level 5), when the caller asks for it.
Cloudflare does ask. Live goes out at 44 KB instead of 435 KB, Buildings at
10 KB instead of 179 KB. Static files, streamed responses, error pages and
anything under 1 KB are left alone.

### Server: the machine these websites run on

**Server** (superuser only, `/server`) shows the Raspberry Pi the portal runs on.
It is read live each time the page opens and refreshes every minute
(`app/server_health.py`). It reads `/proc`, `/sys` and a few commands
(`systemctl`, `ss`, `vcgencmd`, `nmcli`) and needs no extra package.

- **Headline numbers:** CPU, memory, main-disk space, temperature, network
  speed, requests a minute through the Cloudflare tunnels, websites
  answering, and uptime.
- **History charts:** 6 hours, 24 hours, 7 days or 30 days, from
  `server_samples`. One row is written a minute by `ServerSampler`, kept
  `SERVER_KEEP_DAYS` (30). A gap in a chart means the server was off.
- **Websites:** every hostname in every running `cloudflared` config, checked
  locally with a GET of `/` using that Host header. Redirects are not followed,
  so a sign-in redirect counts as answering. Each tunnel's own `/ready` and
  `/metrics` give its links to Cloudflare and its request and error counts.
- **Services:** the units in `WATCH`, both system and `--user`, each with its
  state, how long it has run, crash restarts, memory and CPU. Memory counts
  shared database memory once.
- **Storage:** used and free space, write speed, and average writes a day.
  On an SD card it also shows the card's name, date and maker id.
- **Memory:** usage, plus the biggest programs.
- **Network:** each connection, the Wi-Fi signal and dropped packets.
- **Delay to router:** once a minute the sampler sends 10 pings to the router
  (the default route's gateway) and 10 to the internet (`1.1.1.1`), side by
  side, about 2 s. A page load never pings: it shows the newest measurement
  while it is under 5 minutes old. A healthy home network answers in 1-5 ms.
  30 ms or 10% lost is amber; 100 ms or 30% lost is red. The router hop is the
  Wi-Fi or cable alone, so a slow router there means the home network, not the
  server and not the internet provider. Both are charted (`router_ms`,
  `internet_ms` and the loss in `server_samples`, migration `a4c6e8f0b2d1`).
  The service needs no extra rights: Raspberry Pi OS allows ping for every
  group (`net.ipv4.ping_group_range`).
- **Database:** sizes, connections and live standby copies.
- **Backups:** the newest file, the last nightly run and the age of the
  off-site copy, for each entry in `BACKUPS`.
- **System:** hardware, OS and clock sync. Under-voltage and throttling come
  from `vcgencmd get_throttled`.
- **Problems:** `checks()` lists them worst first. A failed service, a site
  not answering, a tunnel with no links, a missing or failed backup, a disk 90%
  full, under 5% memory left, 80 °C or more, a router more than 100 ms away
  or not answering, or an internet connection that is down is red.

**If this server fails** (`resilience()`) lists the single points of failure:
one server, an SD-card boot disk, Wi-Fi only, no database standby, backups
with no off-site copy, and power. They are the to-do list for adding servers.

**Several servers.** Every sample and snapshot carries the host name.
`server_hosts` keeps each server's newest whole snapshot, and the page shows a
picker as soon as a second host appears. Only the machine the portal runs on is
read live. Another server will report by running the same `collect()` and
storing its snapshot. That reporting path is not built yet.

The two gunicorn workers both run the sampler. A transaction-level advisory
lock (`875_219_006`), plus "sampled under 48 s ago? skip", keeps it to one
snapshot a minute. The unique `(host, at)` row is the last guard.
`SERVER_SAMPLER_ENABLED=0` turns sampling off.

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
