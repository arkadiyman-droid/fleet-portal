from datetime import datetime, date

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from app.crypto_types import EncryptedString
from app.extensions import db


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    contact_name = db.Column(db.String(200))
    contact_email = db.Column(db.String(200))
    contact_phone = db.Column(db.String(50))

    # Set at onboarding (invoice vs auto_charge). Not consumed by Phase 4's
    # invoice generation/approval flow at all — Phase 5's payment-processor
    # integration is what will actually act on this value.
    billing_method = db.Column(db.String(20), nullable=False, default="invoice")
    # Stripe Customer id — created lazily on the first Checkout Session for
    # this client. See app/stripe_billing.py::get_or_create_customer().
    billing_customer_id = db.Column(db.String(200))

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    properties = db.relationship(
        "Property", back_populates="client", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("billing_method IN ('invoice', 'auto_charge')", name="ck_clients_billing_method"),
    )

    def __repr__(self):
        return f"<Client {self.id} {self.name!r}>"


class Property(db.Model):
    __tablename__ = "properties"

    id = db.Column(db.Integer, primary_key=True)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.Text)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = db.relationship("Client", back_populates="properties")
    buildings = db.relationship(
        "Building", back_populates="property", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self):
        return f"<Property {self.id} {self.name!r}>"


class Building(db.Model):
    __tablename__ = "buildings"

    id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey("properties.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    address = db.Column(db.Text)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    property = db.relationship("Property", back_populates="buildings")
    controllers = db.relationship(
        "Controller", back_populates="building", cascade="all, delete-orphan", passive_deletes=True
    )
    # Billed unit count / contract terms (Phase 1 placeholders removed in
    # Phase 4) now live in Contract, ordered newest-first — the "current"
    # one is whichever has end_date IS NULL. See app/billing.py::current_contract().
    contracts = db.relationship(
        "Contract", back_populates="building", cascade="all, delete-orphan",
        passive_deletes=True, order_by="Contract.start_date.desc()",
    )
    invoices = db.relationship(
        "Invoice", back_populates="building", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self):
        return f"<Building {self.id} {self.name!r}>"


class Controller(db.Model):
    __tablename__ = "controllers"

    id = db.Column(db.Integer, primary_key=True)
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(200), nullable=False)

    # Tailscale IP is what the fleet poller actually connects to — stable
    # per-device. Hostname is informational only (MagicDNS isn't guaranteed
    # tailnet-wide, so it's never relied on for the poll URL).
    tailscale_hostname = db.Column(db.String(200))
    tailscale_ip = db.Column(db.String(50))
    # Fleet Portal read-only status-polling credential, matched against a
    # sha256 hash stored on the controller itself (see EMS's fleet/token.py)
    # — encrypted at rest here (see app/crypto_types.py) since, unlike while
    # this column was inert, it's now a live credential worth protecting.
    fleet_api_token = db.Column(EncryptedString)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    building = db.relationship("Building", back_populates="controllers")

    def __repr__(self):
        return f"<Controller {self.id} {self.name!r}>"


class ControllerStatus(db.Model):
    """Latest polled /api/status snapshot for a Controller — one row per
    controller (upserted), not a growing history table. Phase 2's job is
    proving connectivity, not trending; a history table would need its own
    retention policy this phase doesn't need. The full raw payload is kept
    (not hand-picked columns) so EMS-side field additions don't require a
    fleet-portal migration — see app/fleet_poller.py."""
    __tablename__ = "controller_statuses"

    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="CASCADE"), primary_key=True)
    polled_at = db.Column(db.DateTime, nullable=False)
    ok = db.Column(db.Boolean, nullable=False)
    http_status = db.Column(db.Integer)
    error = db.Column(db.Text)
    response_ms = db.Column(db.Integer)
    raw_json = db.Column(JSONB)
    building_name = db.Column(db.String(200))
    version = db.Column(db.String(50))
    # When the controller last answered. polled_at is the last ATTEMPT, so on
    # an offline controller it keeps moving; this says how long it has really
    # been gone. Set by fleet_poller.poll_controller on success only.
    last_ok_at = db.Column(db.DateTime)

    controller = db.relationship("Controller", backref=db.backref("status", uselist=False))

    def __repr__(self):
        return f"<ControllerStatus controller_id={self.controller_id} ok={self.ok}>"


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    email = db.Column(db.String(200), nullable=False, unique=True)
    login = db.Column(db.String(80), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    permissions = db.relationship(
        "Permission", back_populates="user", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self):
        return f"<User {self.id} {self.login!r}>"


class Permission(db.Model):
    """A (user, scope, role) grant. scope_type/scope_id is a polymorphic
    reference to a Client/Property/Building row — no DB FK is possible
    across three different target tables, so referential validity is
    enforced at write time (app/routes.py's add_permission(), via
    get_or_404 against the right model), not by the schema. See
    app/permissions.py for how a grant at one level implies the same role
    on everything beneath it."""
    __tablename__ = "permissions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    scope_type = db.Column(db.String(20), nullable=False)
    scope_id = db.Column(db.Integer, nullable=False)
    role = db.Column(db.String(20), nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User", back_populates="permissions")

    __table_args__ = (
        CheckConstraint("scope_type IN ('client', 'property', 'building')", name="ck_permissions_scope_type"),
        CheckConstraint("role IN ('viewer', 'operator', 'admin')", name="ck_permissions_role"),
        db.UniqueConstraint("user_id", "scope_type", "scope_id", name="uq_permissions_user_scope"),
        db.Index("ix_permissions_scope", "scope_type", "scope_id"),
    )

    def __repr__(self):
        return f"<Permission user_id={self.user_id} {self.scope_type}:{self.scope_id} {self.role}>"


class Contract(db.Model):
    """A new row per unit-count/rate period for a Building — existing rows
    are never mutated, only closed (end_date set). At most one row per
    Building may be 'current' (end_date IS NULL) — enforced by the partial
    unique index below, not just app logic. See app/billing.py."""
    __tablename__ = "contracts"

    id = db.Column(db.Integer, primary_key=True)
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False)
    unit_count = db.Column(db.Integer, nullable=False)
    # Per-row, not a global constant — a future rate change never rewrites
    # the history on old contracts.
    rate_per_unit_cents = db.Column(db.Integer, nullable=False, default=1000)  # $10.00/unit/year
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)  # NULL = currently in effect

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    building = db.relationship("Building", back_populates="contracts")

    __table_args__ = (
        CheckConstraint("unit_count > 0", name="ck_contracts_unit_count_positive"),
        CheckConstraint("rate_per_unit_cents > 0", name="ck_contracts_rate_positive"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="ck_contracts_end_after_start"),
        db.Index(
            "uq_contracts_building_current", "building_id", unique=True,
            postgresql_where=db.text("end_date IS NULL"),
        ),
    )

    def __repr__(self):
        return f"<Contract building_id={self.building_id} units={self.unit_count} start={self.start_date}>"


class Invoice(db.Model):
    """One invoice per Building per half-year period — never consolidated
    per Client. unit_count/rate_per_unit_cents are snapshotted from the
    Contract at generation time, so a later contract change never
    retroactively alters an already-generated invoice's amount.
    status: draft -> pending -> paid, plus void (discard a mistaken draft
    before it's sent). 'Overdue' is never stored — derived at render time
    as status == 'pending' and due_date < today. See app/billing.py."""
    __tablename__ = "invoices"

    id = db.Column(db.Integer, primary_key=True)
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="CASCADE"), nullable=False)
    contract_id = db.Column(db.Integer, db.ForeignKey("contracts.id", ondelete="RESTRICT"), nullable=False)

    period_start = db.Column(db.Date, nullable=False)
    period_end = db.Column(db.Date, nullable=False)

    unit_count = db.Column(db.Integer, nullable=False)
    rate_per_unit_cents = db.Column(db.Integer, nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)

    status = db.Column(db.String(20), nullable=False, default="draft")
    issued_date = db.Column(db.Date)  # set on approve
    due_date = db.Column(db.Date)     # set on approve = issued_date + INVOICE_DUE_DAYS

    # Latest Stripe Checkout Session for this invoice — overwritten by
    # every (re)generation, no uniqueness needed. See app/stripe_billing.py.
    stripe_checkout_session_id = db.Column(db.String(255))
    stripe_checkout_url = db.Column(db.Text)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    building = db.relationship("Building", back_populates="invoices")
    contract = db.relationship("Contract")
    payments = db.relationship(
        "Payment", back_populates="invoice", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (
        CheckConstraint("status IN ('draft', 'pending', 'paid', 'void')", name="ck_invoices_status"),
        CheckConstraint("amount_cents >= 0", name="ck_invoices_amount_nonneg"),
        # DB-level backstop making generate_invoices() safely re-runnable —
        # see app/billing.py.
        db.UniqueConstraint("building_id", "period_start", name="uq_invoices_building_period"),
        db.Index("ix_invoices_status", "status"),
    )

    def __repr__(self):
        return f"<Invoice {self.id} building_id={self.building_id} period={self.period_start} {self.status}>"


class Payment(db.Model):
    """One payment against an invoice, recorded either manually by a
    Superuser or automatically via a verified Stripe webhook (see
    app/stripe_billing.py) — both paths funnel through
    app/billing.py::_finalize_payment(). Deliberately no partial-payment
    reconciliation: one Payment settles an Invoice in full."""
    __tablename__ = "payments"

    id = db.Column(db.Integer, primary_key=True)
    invoice_id = db.Column(db.Integer, db.ForeignKey("invoices.id", ondelete="CASCADE"), nullable=False)
    amount_cents = db.Column(db.Integer, nullable=False)
    paid_date = db.Column(db.Date, nullable=False)
    method = db.Column(db.String(20), nullable=False)
    # Stripe PaymentIntent id — NULL for manually-recorded payments. UNIQUE
    # so a retried webhook delivery (Stripe explicitly retries) can never
    # double-record a payment. Postgres allows multiple NULLs under a
    # unique constraint, so manual payments are unaffected.
    stripe_payment_intent_id = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    invoice = db.relationship("Invoice", back_populates="payments")

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="ck_payments_amount_positive"),
        CheckConstraint("method IN ('check', 'ach', 'wire', 'card', 'other')", name="ck_payments_method"),
        db.UniqueConstraint("stripe_payment_intent_id", name="uq_payments_stripe_payment_intent_id"),
    )

    def __repr__(self):
        return f"<Payment invoice_id={self.invoice_id} amount_cents={self.amount_cents}>"


class Enrollment(db.Model):
    """A blank controller that called home to be adopted (EMS
    use_controller/fleet/enroll.py, portal app/enroll_api.py).

    It is NOT a Controller until an operator adopts it by typing the pairing
    code the module shows on its own screen. Until then it is only what the
    module claims about itself, so none of it is trusted for anything but
    display. The module proves it is the same box on every call with a
    secret; only hashes of the secret and the code are kept here.

    state: waiting   -> said hello, nobody has adopted it
           assigned  -> an operator adopted it; mode "new": a Controller row
                        exists with no token yet (with Tailscale settings,
                        the module first joins the tailnet with ts_key); mode "restore": a site
                        bundle, encrypted to THIS module's own key in the
                        operator's browser, waits for it in bundle_ct
           restoring -> (restore) the module took the bundle and is
                        rebooting into that site
           complete  -> the module handed over its polling token (new), or
                        the restored site answered its first poll (restore)
           rejected  -> an operator dismissed it

    The bundle is only ever ciphertext here: the operator's browser
    decrypted it with the master key and re-encrypted it to age_recipient
    (app/static/js/agecrypt.js), so only the module's own private key opens
    it. It is deleted the moment the module takes it.
    """
    __tablename__ = "enrollments"

    id = db.Column(db.String(36), primary_key=True)          # the module's own uuid
    secret_hash = db.Column(db.String(64), nullable=False)
    code_hash = db.Column(db.String(64), nullable=False, index=True)
    state = db.Column(db.String(12), nullable=False, default="waiting")

    hostname = db.Column(db.String(100))
    serial = db.Column(db.String(64))
    model = db.Column(db.String(120))
    ems_version = db.Column(db.String(60))
    lan_ip = db.Column(db.String(50))
    remote_ip = db.Column(db.String(50))                       # what the portal saw the call come from
    age_recipient = db.Column(db.String(100))                  # the module's own public key (for a later restore)

    first_seen = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    last_seen = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="SET NULL"))
    assigned_by = db.Column(db.String(120))
    assigned_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)

    mode = db.Column(db.String(10), nullable=False, default="new")          # new | restore
    bundle_ct = db.Column(db.LargeBinary)                                    # ciphertext, until fetched
    bundle_site = db.Column(db.String(100))                                  # what the operator's browser read from it
    bundle_made = db.Column(db.String(40))
    fetched_at = db.Column(db.DateTime)
    restoring_at = db.Column(db.DateTime)
    module_error = db.Column(db.String(300))                                 # the module's own last error, as it reports it

    # New-site adoptions onto the fleet's tailnet (app/tailscale_api.py): the
    # one-time key minted for this module (encrypted at rest, dropped once the
    # module reports its Tailscale address), when it was minted or last tried,
    # and why it could not be, in plain words, for the adopt page.
    ts_key = db.Column(EncryptedString)
    ts_key_made = db.Column(db.DateTime)
    ts_error = db.Column(db.String(300))

    controller = db.relationship("Controller", backref=db.backref("enrollment", uselist=False))

    __table_args__ = (
        CheckConstraint("state IN ('waiting','assigned','restoring','complete','rejected')", name="ck_enrollments_state"),
        CheckConstraint("mode IN ('new','restore')", name="ck_enrollments_mode"),
    )

    def __repr__(self):
        return f"<Enrollment {self.id} {self.state} {self.hostname}>"


class AlertState(db.Model):
    """Where each controller stands for alerting (app/alerts.py): one row per
    controller, kept across restarts so an open alert is not sent twice."""
    __tablename__ = "alert_states"

    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="CASCADE"), primary_key=True)
    alerting = db.Column(db.Boolean, nullable=False, default=False)   # an ALERT went out and has not recovered
    first_bad_at = db.Column(db.DateTime)                              # red since (for the ALERT_AFTER_SECONDS wait)
    opened_at = db.Column(db.DateTime)
    notified_at = db.Column(db.DateTime)                               # last ALERT or REMINDER
    issue = db.Column(db.String(300))                                  # the first red issue, for the Alerts page
    muted_until = db.Column(db.DateTime)
    muted_by = db.Column(db.String(120))

    controller = db.relationship("Controller", backref=db.backref("alert_state", uselist=False))


class AlertEvent(db.Model):
    """Every alert decision, sent or not: the Alerts page's history."""
    __tablename__ = "alert_events"

    id = db.Column(db.Integer, primary_key=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="SET NULL"), index=True)
    controller_name = db.Column(db.String(200))                        # kept if the controller is deleted
    # or one of the installed 2010 boxes (/live); controller_name then holds the box's name
    legacy_box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="SET NULL"), index=True)
    kind = db.Column(db.String(12), nullable=False)                    # open | reminder | recovered | test | mute | outage | outage-end
    title = db.Column(db.String(200))
    text = db.Column(db.String(1000))
    delivered = db.Column(db.String(300))                              # what each channel answered
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, index=True)


class Rollout(db.Model):
    """A software release going out to controllers, batch by batch
    (app/rollouts.py). Wave 0 is the test controller; a wave starts only
    when the previous one has updated, confirmed and stayed out of the red
    for soak_minutes. Anything failing halts it."""
    __tablename__ = "rollouts"

    id = db.Column(db.Integer, primary_key=True)
    target_sha = db.Column(db.String(40), nullable=False)
    state = db.Column(db.String(12), nullable=False, default="running")   # running|paused|done|halted|cancelled
    wave = db.Column(db.Integer, nullable=False, default=0)                # the wave in progress
    wave_done_at = db.Column(db.DateTime)                                  # when the current wave finished (soak starts)
    batch_size = db.Column(db.Integer, nullable=False, default=10)
    soak_minutes = db.Column(db.Integer, nullable=False, default=30)
    reason = db.Column(db.String(300))                                     # why it halted / was cancelled
    created_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)

    targets = db.relationship("RolloutTarget", back_populates="rollout", cascade="all, delete-orphan",
                              order_by="(RolloutTarget.wave, RolloutTarget.id)")


class RolloutTarget(db.Model):
    """One controller in a rollout."""
    __tablename__ = "rollout_targets"
    __table_args__ = (db.UniqueConstraint("rollout_id", "controller_id", name="uq_rollout_target"),)

    id = db.Column(db.Integer, primary_key=True)
    rollout_id = db.Column(db.Integer, db.ForeignKey("rollouts.id", ondelete="CASCADE"), nullable=False, index=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="CASCADE"), nullable=False, index=True)
    wave = db.Column(db.Integer, nullable=False, default=0)
    state = db.Column(db.String(12), nullable=False, default="pending")   # pending|requested|confirming|done|failed|skipped
    from_sha = db.Column(db.String(40))
    first_try_at = db.Column(db.DateTime)
    requested_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    detail = db.Column(db.String(300))

    rollout = db.relationship("Rollout", back_populates="targets")
    controller = db.relationship("Controller")


class LegacyLogin(db.Model):
    """A login for the installed 2010 gateways (app/legacy.py). Most boxes
    share one; others are added when a box refuses the shared one."""
    __tablename__ = "legacy_logins"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(60), nullable=False, unique=True)
    username = db.Column(EncryptedString, nullable=False)
    password = db.Column(EncryptedString, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class LegacyBox(db.Model):
    """One installed 2010 gateway, read view-only over oBIX for /live."""
    __tablename__ = "legacy_boxes"
    __table_args__ = (db.UniqueConstraint("host", "port", name="uq_legacy_box_address"),)

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)               # as the old client's list names it
    host = db.Column(db.String(200), nullable=False)
    port = db.Column(db.Integer, nullable=False)
    # gateway | open | other | unreachable | unknown: 'other' is never sent a login
    identity = db.Column(db.String(12), nullable=False, default="unknown")
    login_id = db.Column(db.Integer, db.ForeignKey("legacy_logins.id", ondelete="SET NULL"))
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="SET NULL"))
    # Hidden (is_active False): not polled, listed under Live's "Hidden", one
    # click brings it back. Deleted (deleted_at set): gone from the portal;
    # the row stays only so a re-run of import-legacy-boxes.py skips it.
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    deleted_at = db.Column(db.DateTime)
    hidden_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    login = db.relationship("LegacyLogin")
    building = db.relationship("Building")
    status = db.relationship("LegacyStatus", uselist=False, back_populates="box", cascade="all, delete-orphan")

    @property
    def url(self):
        return f"http://{self.host}:{self.port}"


class LegacyStatus(db.Model):
    """The latest reading of one box (overwritten each poll)."""
    __tablename__ = "legacy_statuses"

    box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="CASCADE"), primary_key=True)
    polled_at = db.Column(db.DateTime, nullable=False)
    ok = db.Column(db.Boolean, nullable=False, default=False)
    auth_failed = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.String(300))
    response_ms = db.Column(db.Integer)
    last_ok_at = db.Column(db.DateTime)
    names_at = db.Column(db.DateTime)
    data = db.Column(JSONB)                                         # {status:{...}, names:{...}, alarm:{...}}

    box = db.relationship("LegacyBox", back_populates="status")


class LegacyAlertState(db.Model):
    """Where each 2010 box stands for alerting (app/alerts.py check_legacy):
    the AlertState of a box, plus the latest box alarm already seen, so a
    NEW safety or ignition alarm is sent once and an old one never is."""
    __tablename__ = "legacy_alert_states"

    box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="CASCADE"), primary_key=True)
    alerting = db.Column(db.Boolean, nullable=False, default=False)
    first_bad_at = db.Column(db.DateTime)
    opened_at = db.Column(db.DateTime)
    notified_at = db.Column(db.DateTime)
    issue = db.Column(db.String(300))
    muted_until = db.Column(db.DateTime)
    muted_by = db.Column(db.String(120))
    last_alarm = db.Column(db.String(120))                             # "name@timestamp" of the alarm last seen

    box = db.relationship("LegacyBox", backref=db.backref("alert_state", uselist=False,
                                                          cascade="all, delete-orphan"))


# ── History of the 2010 boxes (app/history.py, app/analytics.py) ─────────────
#
# Measured on the real boxes (2026-10-14): one read as JSON costs 1,173 bytes
# (22 GB a year for ~180 boxes), typed columns 374 bytes (7 GB a year). So:
# every read is kept typed for LEGACY_RAW_DAYS (monthly partitions, dropped
# whole); hourly and daily summaries and the event log are kept for good.

class LegacyReading(db.Model):
    """One successful read of one box, typed. In production the table is
    PARTITIONED BY RANGE (ts), one partition per month (migration
    b3e7c9a1d5f2; app/history.py makes and drops them). No foreign key:
    box rows are never removed (delete only marks them)."""
    __tablename__ = "legacy_readings"
    # BRIN: a few kilobytes for months of append-only, time-ordered rows
    __table_args__ = (db.Index("ix_legacy_readings_ts", "ts", postgresql_using="brin"),)

    box_id = db.Column(db.Integer, primary_key=True)
    ts = db.Column(db.DateTime, primary_key=True)                    # UTC, when the portal read it
    code = db.Column(db.String(12))                                  # callForHeat, e.g. "Outsd Sat"
    out = db.Column(db.SmallInteger)                                 # the box's outside sensor
    inside = db.Column(db.SmallInteger)                              # averageTemperature
    dit = db.Column(db.SmallInteger)                                 # targetInsideTemperature
    aqua = db.Column(db.SmallInteger)
    coil = db.Column(db.SmallInteger)
    mix = db.Column(db.SmallInteger)
    supp = db.Column(db.SmallInteger)
    retn = db.Column(db.SmallInteger)
    risr = db.Column(db.SmallInteger)
    dsup = db.Column(db.SmallInteger)                                # desiredSupplyTemp: hot-water target (0 = not calling)
    stack = db.Column(db.SmallInteger)
    hi_stack = db.Column(db.SmallInteger)
    valve = db.Column(db.SmallInteger)                               # controlValvePctOpen
    pressure = db.Column(db.SmallInteger)                            # pressure0
    # run minutes since the box's own midnight, per boiler: heat and hot water
    heat1 = db.Column(db.SmallInteger)
    hw1 = db.Column(db.SmallInteger)
    heat2 = db.Column(db.SmallInteger)
    hw2 = db.Column(db.SmallInteger)
    heat3 = db.Column(db.SmallInteger)
    hw3 = db.Column(db.SmallInteger)
    heat4 = db.Column(db.SmallInteger)
    hw4 = db.Column(db.SmallInteger)
    fuel_level = db.Column(db.SmallInteger)
    gallons = db.Column(db.Integer)
    fuel_count = db.Column(db.BigInteger)                            # meter pulse counters
    water_count = db.Column(db.BigInteger)
    ign_count = db.Column(db.SmallInteger)
    outputs = db.Column(db.Integer)                                  # relay image
    inputs = db.Column(db.Integer)
    sys = db.Column(ARRAY(db.SmallInteger))                          # systemTemperature0..15
    apt = db.Column(ARRAY(db.SmallInteger))                          # apartmentTemperature0..31 (0 = no sensor)


class LegacyHourly(db.Model):
    """One box, one hour (UTC start): kept for good. out_avg is the box's own
    outside sensor, or the fleet's median that hour (out_fleet) when the box
    has none. Run minutes are summed over its boilers."""
    __tablename__ = "legacy_hourly"

    box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="CASCADE"), primary_key=True)
    hour = db.Column(db.DateTime, primary_key=True, index=True)
    n = db.Column(db.SmallInteger, nullable=False)                   # reads in the hour
    out_avg = db.Column(db.Float)
    out_fleet = db.Column(db.Boolean, nullable=False, default=False)
    in_avg = db.Column(db.Float)
    in_min = db.Column(db.SmallInteger)
    in_max = db.Column(db.SmallInteger)
    dit = db.Column(db.SmallInteger)
    aqua_avg = db.Column(db.Float)
    aqua_max = db.Column(db.SmallInteger)
    supp_avg = db.Column(db.Float)
    retn_avg = db.Column(db.Float)
    coil_min = db.Column(db.SmallInteger)
    coil_max = db.Column(db.SmallInteger)
    mix_min = db.Column(db.SmallInteger)
    mix_max = db.Column(db.SmallInteger)
    stack_avg = db.Column(db.Float)
    stack_max = db.Column(db.SmallInteger)
    hi_stack = db.Column(db.SmallInteger)
    heat_min = db.Column(db.SmallInteger, nullable=False, default=0)
    hw_min = db.Column(db.SmallInteger, nullable=False, default=0)
    calling_min = db.Column(db.SmallInteger, nullable=False, default=0)   # status says heat is being called for
    safety_min = db.Column(db.SmallInteger, nullable=False, default=0)    # Safety / Ignition Failure
    bypass_min = db.Column(db.SmallInteger, nullable=False, default=0)
    fuel = db.Column(db.Integer)                                     # meter counts used
    water = db.Column(db.Integer)
    hdd = db.Column(db.Float, nullable=False, default=0)             # heating degree-days (65 F base)
    law_req = db.Column(db.SmallInteger)                             # heat-season minimum this hour, if any
    law_low = db.Column(db.SmallInteger, nullable=False, default=0)  # apartment sensors averaging below it
    # hot water while calling for heat: the box's target supply, the real supply,
    # and the minutes the loop was cold (supply / return under legacy.LOOP_COLD_F)
    dsup_avg = db.Column(db.Float)
    supp_call_avg = db.Column(db.Float)
    short_min = db.Column(db.SmallInteger, nullable=False, default=0)
    apt_low = db.Column(db.SmallInteger)                             # lowest apartment read in the hour
    apt_avg = db.Column(ARRAY(db.SmallInteger))                      # per sensor, the hour's average (null = none)


class LegacyDaily(db.Model):
    """One box, one New York calendar day, from its hourly rows."""
    __tablename__ = "legacy_daily"

    box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="CASCADE"), primary_key=True)
    day = db.Column(db.Date, primary_key=True, index=True)
    n = db.Column(db.Integer, nullable=False)
    hours = db.Column(db.SmallInteger, nullable=False)
    out_avg = db.Column(db.Float)
    out_min = db.Column(db.Float)
    out_max = db.Column(db.Float)
    hdd = db.Column(db.Float, nullable=False, default=0)
    in_avg = db.Column(db.Float)
    in_min = db.Column(db.SmallInteger)
    in_max = db.Column(db.SmallInteger)
    dit = db.Column(db.SmallInteger)
    heat_min = db.Column(db.Integer, nullable=False, default=0)
    hw_min = db.Column(db.Integer, nullable=False, default=0)
    calling_min = db.Column(db.Integer, nullable=False, default=0)
    safety_min = db.Column(db.Integer, nullable=False, default=0)
    fuel = db.Column(db.Integer)
    water = db.Column(db.Integer)
    water_night = db.Column(db.Integer)                              # 1 am to 5 am: leaks show here
    law_hours = db.Column(db.SmallInteger, nullable=False, default=0)
    law_sensor_hours = db.Column(db.Integer, nullable=False, default=0)
    apt_low = db.Column(db.SmallInteger)
    stack_fire = db.Column(db.Float)                                 # average hourly peak stack while firing
    overheat_hours = db.Column(db.SmallInteger, nullable=False, default=0)
    warm_heat_min = db.Column(db.Integer, nullable=False, default=0)  # heat run with 60 F+ outside


class LegacyEvent(db.Model):
    """What changed, when: status changes, box alarms, dark / back."""
    __tablename__ = "legacy_events"
    __table_args__ = (db.Index("ix_legacy_events_box_ts", "box_id", "ts"),)

    id = db.Column(db.Integer, primary_key=True)
    box_id = db.Column(db.Integer, db.ForeignKey("legacy_boxes.id", ondelete="CASCADE"), nullable=False)
    ts = db.Column(db.DateTime, nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)                  # status | alarm | dark | back
    code = db.Column(db.String(40))                                  # the new status code / alarm name
    text = db.Column(db.String(200))


class ControllerReading(db.Model):
    """A compact snapshot of one EchoTherm controller's /api/status, every
    5 minutes, kept 90 days (app/ctrl_history.py): charts only."""
    __tablename__ = "controller_readings"

    controller_id = db.Column(db.Integer, db.ForeignKey("controllers.id", ondelete="CASCADE"), primary_key=True)
    ts = db.Column(db.DateTime, primary_key=True)
    values = db.Column(JSONB, nullable=False)                        # {chart key: number}, run counters included
    apt = db.Column(ARRAY(db.SmallInteger))                          # aptTemps, trailing zeros dropped


# ── Bloom / Empowered Buildings (app/bloom.py) ───────────────────────────────
#
# A third monitored source, alongside EchoTherm controllers and the 2010 oBIX
# boxes: buildings on a different vendor's controller, read VIEW-ONLY through
# Bloom's private JSON API (my.empoweredbuildings.com — POST /api/users/login
# for a bearer token, then GET /api/assets + /api/alarms). Added for Skyline
# Terrace (Canton OH). Nothing here writes to Bloom or to any site.

class BloomAccount(db.Model):
    """One Bloom login, read view-only. The username/password are a real
    credential, so they are encrypted at rest (app/crypto_types.py), as is the
    bearer token cached from the last successful login and reused until it is
    rejected (app/bloom.py re-logs in on a 401). One account owns many assets."""
    __tablename__ = "bloom_accounts"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(60), nullable=False, unique=True)
    base_url = db.Column(db.String(200), nullable=False, default="https://my.empoweredbuildings.com")
    username = db.Column(EncryptedString, nullable=False)
    password = db.Column(EncryptedString, nullable=False)
    token = db.Column(EncryptedString)                               # cached JWT, until it 401s
    token_at = db.Column(db.DateTime)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    assets = db.relationship(
        "BloomAsset", back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self):
        return f"<BloomAccount {self.id} {self.label!r}>"


class BloomAsset(db.Model):
    """One Bloom-monitored building ('asset' in Bloom's terms), read
    view-only. asset_key is Bloom's own id/slug for it (how a poll result is
    matched back to this row); building_id ties it into the
    Client -> Property -> Building hierarchy (Skyline Properties), nullable so
    an asset can exist before it is placed, like a LegacyBox."""
    __tablename__ = "bloom_assets"
    # (account, asset_key, view) is unique: several rows may share ONE Bloom feed
    # when that feed covers more than one building (e.g. a two-tower plant). Each
    # such row carries a distinct `view` so it scopes the shared feed to its own
    # sub-building; a normal whole-feed asset uses view="".
    __table_args__ = (db.UniqueConstraint("account_id", "asset_key", "view", name="uq_bloom_asset_view"),)

    id = db.Column(db.Integer, primary_key=True)
    account_id = db.Column(db.Integer, db.ForeignKey("bloom_accounts.id", ondelete="CASCADE"), nullable=False)
    asset_key = db.Column(db.String(200), nullable=False)           # Bloom asset id / slug
    name = db.Column(db.String(200), nullable=False)                # e.g. "631 Alan Page Dr"
    address = db.Column(db.Text)
    # Scoping a shared feed to one sub-building. view = "" for a normal asset;
    # otherwise the apartment/space name prefix to keep (e.g. "354" / "358").
    # plant_tag = the plant's building letter (e.g. "A"/"B") used to pick the
    # right "PDT BLDG <tag> SUPPLY/RETURN" and to drop the other tower's points.
    view = db.Column(db.String(20), nullable=False, default="", server_default="")
    plant_tag = db.Column(db.String(8))
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="SET NULL"))
    # Hidden (is_active False): kept but not shown/polled. Deleted (deleted_at
    # set): gone from the portal, the row stays only so a re-import skips it.
    # Mirrors LegacyBox's two-state retirement.
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    deleted_at = db.Column(db.DateTime)
    hidden_by = db.Column(db.String(120))
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    account = db.relationship("BloomAccount", back_populates="assets")
    building = db.relationship("Building")
    status = db.relationship(
        "BloomStatus", uselist=False, back_populates="asset", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<BloomAsset {self.id} {self.name!r}>"


class BloomStatus(db.Model):
    """The latest reading of one Bloom asset (overwritten each poll). The whole
    asset object plus its open alarms are kept as raw JSON, so a Bloom-side
    field change never needs a migration — the card is derived at read time
    (app/bloom.py::card), exactly like ControllerStatus.raw_json / LegacyStatus.data."""
    __tablename__ = "bloom_statuses"

    asset_id = db.Column(db.Integer, db.ForeignKey("bloom_assets.id", ondelete="CASCADE"), primary_key=True)
    polled_at = db.Column(db.DateTime, nullable=False)
    ok = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.String(300))
    response_ms = db.Column(db.Integer)
    last_ok_at = db.Column(db.DateTime)
    data = db.Column(JSONB)                                         # {asset:{...}, alarms:[...]}

    asset = db.relationship("BloomAsset", back_populates="status")

    def __repr__(self):
        return f"<BloomStatus asset_id={self.asset_id} ok={self.ok}>"


class RcController(db.Model):
    """One Reliable Controls MACH-ProWeb controller we read, view-only, by
    driving its web UI headlessly (app/rc_dhw.py). The DHW values it serves are
    only available through RC's proprietary bulk protocol, decoded client-side by
    the controller's own JavaScript — so we run that JavaScript in a headless
    Chromium and read the rendered values, rather than replicating the protocol.
    The username/password are a real credential, encrypted at rest. Nothing here
    ever writes to the controller (we never click an editable point)."""
    __tablename__ = "rc_controllers"

    id = db.Column(db.Integer, primary_key=True)
    label = db.Column(db.String(60), nullable=False, unique=True)
    base_url = db.Column(db.String(200), nullable=False)             # e.g. http://162.184.200.208:47002
    username = db.Column(EncryptedString, nullable=False)
    password = db.Column(EncryptedString, nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    towers = db.relationship(
        "RcDhwTower", back_populates="controller", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self):
        return f"<RcController {self.id} {self.label!r}>"


class RcDhwTower(db.Model):
    """One DHW plant 'view' on a controller — a single MACH-ProWeb graphic page
    whose rendered points we scrape. Whitney Young has two: Tower A (358, device
    200000, group 200000GRP2) and Tower B (354, device 100000, group 100000GRP2);
    both share the same graphic template, so the same gel-id map (app/rc_dhw.py)
    reads either. `group` is the page's RC group id; the URL hash is base64(group).
    building_id ties the reading into Client -> Property -> Building (nullable, like
    a LegacyBox/BloomAsset, so a tower can exist before it is placed)."""
    __tablename__ = "rc_dhw_towers"
    __table_args__ = (db.UniqueConstraint("controller_id", "group", name="uq_rc_dhw_tower_group"),)

    id = db.Column(db.Integer, primary_key=True)
    controller_id = db.Column(db.Integer, db.ForeignKey("rc_controllers.id", ondelete="CASCADE"), nullable=False)
    building_id = db.Column(db.Integer, db.ForeignKey("buildings.id", ondelete="SET NULL"))
    tower = db.Column(db.String(8), nullable=False)                  # "A" / "B" (display)
    device = db.Column(db.String(16), nullable=False)                # RC device instance, e.g. "200000"
    group = db.Column(db.String(32), nullable=False)                 # RC group id, e.g. "200000GRP2"
    label = db.Column(db.String(120))                                # e.g. "DHW System Tower A 358"
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    controller = db.relationship("RcController", back_populates="towers")
    building = db.relationship("Building")
    status = db.relationship(
        "RcDhwStatus", uselist=False, back_populates="tower", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<RcDhwTower {self.id} {self.tower!r} dev={self.device}>"


class RcDhwStatus(db.Model):
    """The latest DHW reading for one tower (overwritten each poll). The full
    gel-id -> value map is kept as raw JSON (`data`) so a graphic change never
    needs a migration; the handful of promoted numbers (supply/return/pump/alarm)
    are derived at scrape time from the confirmed gel map. Mirrors BloomStatus."""
    __tablename__ = "rc_dhw_statuses"

    tower_id = db.Column(db.Integer, db.ForeignKey("rc_dhw_towers.id", ondelete="CASCADE"), primary_key=True)
    polled_at = db.Column(db.DateTime, nullable=False)
    ok = db.Column(db.Boolean, nullable=False, default=False)
    error = db.Column(db.String(300))
    response_ms = db.Column(db.Integer)
    last_ok_at = db.Column(db.DateTime)
    supply_f = db.Column(db.Float)                                   # DHW Supply To Building
    return_f = db.Column(db.Float)                                   # DHW Return From Building
    pump_running = db.Column(db.Boolean)
    alarm = db.Column(db.Boolean)                                    # any DHW HWP alarm not "Normal"
    data = db.Column(JSONB)                                          # {gel_id: "value text", ...}

    tower = db.relationship("RcDhwTower", back_populates="status")

    def __repr__(self):
        return f"<RcDhwStatus tower_id={self.tower_id} ok={self.ok}>"
