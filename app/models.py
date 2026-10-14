from datetime import datetime, date

from sqlalchemy import CheckConstraint
from sqlalchemy.dialects.postgresql import JSONB

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
                        exists with no token yet; mode "restore": a site
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

    controller = db.relationship("Controller", backref=db.backref("enrollment", uselist=False))

    __table_args__ = (
        CheckConstraint("state IN ('waiting','assigned','restoring','complete','rejected')", name="ck_enrollments_state"),
        CheckConstraint("mode IN ('new','restore')", name="ck_enrollments_mode"),
    )

    def __repr__(self):
        return f"<Enrollment {self.id} {self.state} {self.hostname}>"
