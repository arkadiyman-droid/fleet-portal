"""
app/billing.py — Phase 4 billing: contracts, invoice generation, scheduler.

Policy (settled): $10/unit/year by default (rate stored per-Contract, not
global — see Contract.rate_per_unit_cents), billed twice yearly in two
half-year periods (Jan 1-Jun 30, Jul 1-Dec 31), no proration. One Invoice
per Building per period — never consolidated per Client. Generation
produces DRAFT invoices only; a Superuser must explicitly approve/void
each one (see app/routes.py). NET terms: INVOICE_DUE_DAYS below, applied
from issued_date (set at approval time, not generation time).
"""

import datetime
import logging
import threading
import time

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Building, Contract, Invoice, Payment

log = logging.getLogger(__name__)

INVOICE_DUE_DAYS = 30

_ADVISORY_LOCK_KEY = 875_219_002  # distinct from fleet_poller.py's 875_219_001


def current_contract(building):
    """The building's open contract (end_date IS NULL), if any. At most
    one can exist per building — enforced by contracts' partial unique
    index (uq_contracts_building_current)."""
    return Contract.query.filter_by(building_id=building.id, end_date=None).first()


def period_for_date(d: datetime.date):
    """Which half-year `d` falls in: (Jan 1, Jun 30) or (Jul 1, Dec 31) of
    d's year."""
    if d.month <= 6:
        return datetime.date(d.year, 1, 1), datetime.date(d.year, 6, 30)
    return datetime.date(d.year, 7, 1), datetime.date(d.year, 12, 31)


def generate_invoices(period_start: datetime.date, period_end: datetime.date):
    """Creates one DRAFT Invoice per active Building with a current
    Contract that covers this period, for this exact period. Safe to call
    repeatedly for the same period: buildings that already have an invoice
    for period_start are skipped up front (cheap, the common case), and
    the (building_id, period_start) unique constraint is the DB-level
    backstop for any race (e.g. the daily BillingScheduler cycle and a
    manual 'Generate Now' click landing at the same time) — a per-row
    SAVEPOINT means one duplicate never kills the rest of the batch.
    Never approves/sends anything (see routes.approve_invoice)."""
    already_invoiced = {
        row[0] for row in db.session.query(Invoice.building_id)
        .filter(Invoice.period_start == period_start).all()
    }
    created = []
    for bld in Building.query.filter(Building.is_active.is_(True)).all():
        if bld.id in already_invoiced:
            continue
        contract = current_contract(bld)
        if contract is None:
            continue
        if contract.start_date > period_end:
            continue  # contract not yet in effect during this period
        amount_cents = contract.unit_count * contract.rate_per_unit_cents
        invoice = Invoice(
            building_id=bld.id, contract_id=contract.id,
            period_start=period_start, period_end=period_end,
            unit_count=contract.unit_count, rate_per_unit_cents=contract.rate_per_unit_cents,
            amount_cents=amount_cents, status="draft",
        )
        try:
            with db.session.begin_nested():  # SAVEPOINT — isolates this row
                db.session.add(invoice)
                db.session.flush()
        except IntegrityError:
            continue  # another process already created this building+period's invoice
        created.append(invoice)
    db.session.commit()
    return created


def _finalize_payment(invoice, amount_cents, paid_date, method, stripe_payment_intent_id=None):
    """Shared by routes.record_payment() (manual) and
    stripe_billing.mark_paid_from_stripe_event() (webhook) — the only two
    places an Invoice may move pending -> paid, so the two paths can never
    drift. Returns the created Payment, or None if the invoice is no
    longer pending (already paid/void), or the SAVEPOINT-isolated insert
    hits the stripe_payment_intent_id unique constraint (a retried webhook
    delivery) — both are legitimate no-ops, not errors."""
    if invoice.status != "pending":
        return None
    payment = Payment(
        invoice_id=invoice.id, amount_cents=amount_cents, paid_date=paid_date,
        method=method, stripe_payment_intent_id=stripe_payment_intent_id,
    )
    try:
        with db.session.begin_nested():  # SAVEPOINT — isolates this insert
            db.session.add(payment)
            db.session.flush()
    except IntegrityError:
        return None  # duplicate stripe_payment_intent_id — a retried webhook delivery
    invoice.status = "paid"
    db.session.commit()
    return payment


class BillingScheduler:
    """Mirrors app/fleet_poller.py's FleetPoller shape exactly: daemon
    thread, start(app), log-and-swallow per cycle, Postgres advisory lock
    so multiple workers/replicas never double-generate.

    'Is a run due' check: is today within the 7-day window starting Jan 1
    or Jul 1 (self-heals if the server was down exactly on day 1). If so,
    call generate_invoices() unconditionally for that period — deliberately
    NOT gated behind a separate 'has this period already run at all' check,
    because generate_invoices() is itself idempotent per building+period.
    An extra outer gate would in fact be wrong: a building whose first
    Contract is set on day 3 of the window (after day-1's batch already
    ran) must still get invoiced once generation runs again on day 4 —
    an 'already ran' short-circuit would silently skip it for the whole
    half-year. Re-running the (cheap, mostly-no-op) generation daily
    within the window is what makes that self-healing actually work."""

    def __init__(self):
        self._thread = None

    def start(self, app) -> None:
        if not app.config.get("BILLING_SCHEDULER_ENABLED", True):
            log.info("Billing scheduler disabled (BILLING_SCHEDULER_ENABLED=0)")
            return
        self._thread = threading.Thread(target=self._run, args=(app,), name="billing-scheduler", daemon=True)
        self._thread.start()
        log.info("Billing scheduler started")

    def _run(self, app) -> None:
        interval = app.config.get("BILLING_SCHEDULER_INTERVAL_SECONDS", 86400)  # daily
        while True:
            try:
                self._run_cycle(app)
            except Exception as e:
                log.warning("Billing scheduler cycle failed: %s", e)
            time.sleep(interval)

    def _run_cycle(self, app) -> None:
        with app.app_context():
            conn = db.engine.connect()
            try:
                got_lock = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": _ADVISORY_LOCK_KEY}).scalar()
                if not got_lock:
                    return  # another worker/process owns this cycle
                today = datetime.date.today()
                period_start, period_end = period_for_date(today)
                window_end = period_start + datetime.timedelta(days=6)
                if not (period_start <= today <= window_end):
                    return  # not within a generation window right now
                created = generate_invoices(period_start, period_end)
                if created:
                    log.info(
                        "Billing scheduler: generated %d draft invoice(s) for period starting %s",
                        len(created), period_start,
                    )
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _ADVISORY_LOCK_KEY})
                conn.close()


billing_scheduler = BillingScheduler()
