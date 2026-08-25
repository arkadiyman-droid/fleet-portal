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

    # Inert until Phase 4 (billing).
    billing_method = db.Column(db.String(20), nullable=False, default="invoice")
    # Inert until Phase 5 (payment processor).
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

    # Inert until Phase 4 (billing) — contractual unit count, independent of actual sensor config.
    billed_unit_count = db.Column(db.Integer)
    contract_start_date = db.Column(db.Date)

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    property = db.relationship("Property", back_populates="buildings")
    controllers = db.relationship(
        "Controller", back_populates="building", cascade="all, delete-orphan", passive_deletes=True
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
