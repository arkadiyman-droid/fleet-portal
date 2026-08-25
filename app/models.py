from datetime import datetime, date

from sqlalchemy import CheckConstraint

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

    # Inert until Phase 2 (connectivity).
    tailscale_hostname = db.Column(db.String(200))
    tailscale_ip = db.Column(db.String(50))
    # Inert until Phase 2/6. Plaintext is acceptable ONLY while this column is unused —
    # real secret handling must be designed before Phase 2 starts populating it.
    fleet_api_token = db.Column(db.String(200))

    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    building = db.relationship("Building", back_populates="controllers")

    def __repr__(self):
        return f"<Controller {self.id} {self.name!r}>"
