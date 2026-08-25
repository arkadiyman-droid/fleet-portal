from datetime import datetime, timedelta

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.billing import INVOICE_DUE_DAYS, _finalize_payment, current_contract, generate_invoices, period_for_date
from app.extensions import csrf, db
from app.fleet_poller import poll_controller
from app.models import Building, Client, Contract, Controller, Invoice, Permission, Property, User
from app.permissions import (
    ROLE_RANK,
    creatable_building_property_query,
    creatable_controller_building_query,
    creatable_property_client_query,
    current_user,
    effective_role,
    is_superuser,
    require_role,
    superuser_required,
    user_has_min_role_anywhere,
    visible_building_query,
    visible_client_query,
    visible_controller_query,
    visible_property_query,
)
from app.stripe_billing import (
    WebhookVerificationError,
    create_checkout_session,
    mark_paid_from_stripe_event,
    stripe_configured,
    verify_stripe_webhook,
)

admin_bp = Blueprint("admin", __name__)


# ─── shared helpers ──────────────────────────────────────────────────────

def _bool_arg(name: str) -> bool:
    return request.args.get(name) in ("1", "true", "yes")


def _parse_date(value: str):
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d").date()


def _parse_int(value: str):
    if not value:
        return None
    return int(value)


def _toggle_archived_url(show_archived: bool) -> str:
    args = request.args.to_dict()
    args["show_archived"] = "0" if show_archived else "1"
    return url_for(request.endpoint, **args)


def _dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _can_edit(entity) -> bool:
    if is_superuser():
        return True
    role = effective_role(entity)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK["operator"]


def _can_archive(entity) -> bool:
    if is_superuser():
        return True
    role = effective_role(entity)
    return role is not None and ROLE_RANK[role] >= ROLE_RANK["admin"]


def _soft_delete_cascade(obj) -> int:
    """Sets is_active=False on obj and every descendant, in this transaction. Returns count archived."""
    count = 0
    if isinstance(obj, Client):
        for prop in obj.properties:
            count += _soft_delete_cascade(prop)
    elif isinstance(obj, Property):
        for bld in obj.buildings:
            count += _soft_delete_cascade(bld)
    elif isinstance(obj, Building):
        for ctrl in obj.controllers:
            count += _soft_delete_cascade(ctrl)
    obj.is_active = False
    count += 1
    return count


@admin_bp.route("/")
def index():
    return redirect(url_for("admin.list_clients"))


# ─── Client ──────────────────────────────────────────────────────────────

@admin_bp.route("/clients")
def list_clients():
    show_archived = _bool_arg("show_archived")
    base = Client.query if is_superuser() else visible_client_query(current_user().id)
    q = base if show_archived else base.filter_by(is_active=True)
    clients = q.order_by(Client.name).all()
    rows = [
        {
            "id": c.id,
            "is_active": c.is_active,
            "cells": [c.name, c.contact_email or "—", c.billing_method, c.created_at.strftime("%Y-%m-%d")],
            "can_edit": _can_edit(c),
            "can_archive": _can_archive(c),
        }
        for c in clients
    ]
    return render_template(
        "entity_list.html",
        title="Clients",
        columns=["Name", "Contact Email", "Billing Method", "Created"],
        rows=rows,
        entity="clients",
        entity_singular="client",
        show_archived=show_archived,
        toggle_archived_url=_toggle_archived_url(show_archived),
        # No parent scope above Client — creating one is Superuser-only.
        new_url=url_for("admin.new_client") if is_superuser() else None,
    )


def _client_fields(client=None):
    return [
        {"name": "name", "label": "Name", "type": "text", "required": True, "value": client.name if client else ""},
        {"name": "contact_name", "label": "Contact Name", "type": "text", "required": False, "value": (client.contact_name or "") if client else ""},
        {"name": "contact_email", "label": "Contact Email", "type": "email", "required": False, "value": (client.contact_email or "") if client else ""},
        {"name": "contact_phone", "label": "Contact Phone", "type": "tel", "required": False, "value": (client.contact_phone or "") if client else ""},
        {
            "name": "billing_method", "label": "Billing Method", "type": "select", "required": True,
            "value": client.billing_method if client else "invoice",
            "options": [("invoice", "Invoice"), ("auto_charge", "Auto-charge")],
        },
    ]


@admin_bp.route("/clients/new", methods=["GET", "POST"])
def new_client():
    # No parent scope above Client to check — the whole entity type is
    # Superuser-only (the list view already hides its + New link too).
    if not is_superuser():
        abort(403)
    if request.method == "POST":
        client = Client(
            name=request.form["name"],
            contact_name=request.form.get("contact_name") or None,
            contact_email=request.form.get("contact_email") or None,
            contact_phone=request.form.get("contact_phone") or None,
            billing_method=request.form.get("billing_method", "invoice"),
        )
        db.session.add(client)
        db.session.commit()
        flash(f"Client {client.name!r} created.", "success")
        return redirect(url_for("admin.list_clients"))
    return render_template("entity_form.html", title="New Client", fields=_client_fields(), action=url_for("admin.new_client"))


@admin_bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
def edit_client(client_id):
    client = Client.query.get_or_404(client_id)
    require_role("operator", client)
    if request.method == "POST":
        client.name = request.form["name"]
        client.contact_name = request.form.get("contact_name") or None
        client.contact_email = request.form.get("contact_email") or None
        client.contact_phone = request.form.get("contact_phone") or None
        client.billing_method = request.form.get("billing_method", "invoice")
        db.session.commit()
        flash(f"Client {client.name!r} updated.", "success")
        return redirect(url_for("admin.list_clients"))
    return render_template(
        "entity_form.html", title=f"Edit Client: {client.name}", fields=_client_fields(client),
        action=url_for("admin.edit_client", client_id=client.id),
    )


@admin_bp.route("/clients/<int:client_id>/delete", methods=["POST"])
def delete_client(client_id):
    client = Client.query.get_or_404(client_id)
    require_role("admin", client)
    n = _soft_delete_cascade(client)
    db.session.commit()
    flash(f"Archived {client.name!r} and {n - 1} descendant record(s).", "success")
    return redirect(url_for("admin.list_clients"))


@admin_bp.route("/clients/<int:client_id>/restore", methods=["POST"])
def restore_client(client_id):
    client = Client.query.get_or_404(client_id)
    require_role("admin", client)
    client.is_active = True
    db.session.commit()
    flash(f"Restored {client.name!r}.", "success")
    return redirect(url_for("admin.list_clients"))


# ─── Property ────────────────────────────────────────────────────────────

@admin_bp.route("/properties")
def list_properties():
    show_archived = _bool_arg("show_archived")
    client_id = _parse_int(request.args.get("client_id", ""))
    filter_client = Client.query.get(client_id) if client_id else None
    base = Property.query if is_superuser() else visible_property_query(current_user().id)
    q = base if show_archived else base.filter_by(is_active=True)
    if filter_client:
        q = q.filter_by(client_id=client_id)
    properties = q.order_by(Property.name).all()
    rows = [
        {
            "id": p.id,
            "is_active": p.is_active,
            "cells": [p.name, p.client.name, p.address or "—"],
            "can_edit": _can_edit(p),
            "can_archive": _can_archive(p),
        }
        for p in properties
    ]
    can_create = is_superuser() or user_has_min_role_anywhere("operator")
    if client_id:
        new_url = url_for("admin.new_property", client_id=client_id) if can_create else None
    else:
        new_url = url_for("admin.new_property") if can_create else None
    return render_template(
        "entity_list.html",
        title="Properties" + (f" — {filter_client.name}" if filter_client else ""),
        columns=["Name", "Client", "Address"],
        rows=rows,
        entity="properties",
        entity_singular="property",
        show_archived=show_archived,
        toggle_archived_url=_toggle_archived_url(show_archived),
        new_url=new_url,
    )


def _property_fields(prop=None):
    if prop is not None and not is_superuser():
        # Editing as a scoped user — parent is immutable (see edit_property),
        # so the dropdown only needs the current client, disabled.
        clients = [prop.client]
        disabled = True
    else:
        clients = (
            Client.query.filter_by(is_active=True).order_by(Client.name).all()
            if is_superuser() else
            creatable_property_client_query(current_user().id).filter_by(is_active=True).order_by(Client.name).all()
        )
        disabled = False
    default_client_id = prop.client_id if prop else _parse_int(request.args.get("client_id", ""))
    return [
        {
            "name": "client_id", "label": "Client", "type": "select", "required": True,
            "value": default_client_id, "options": [(c.id, c.name) for c in clients], "disabled": disabled,
        },
        {"name": "name", "label": "Name", "type": "text", "required": True, "value": prop.name if prop else ""},
        {"name": "address", "label": "Address", "type": "textarea", "required": False, "value": (prop.address or "") if prop else ""},
    ]


@admin_bp.route("/properties/new", methods=["GET", "POST"])
def new_property():
    client_id = _parse_int(request.form.get("client_id") or request.args.get("client_id", ""))
    if client_id:
        require_role("operator", Client.query.get_or_404(client_id))
    elif not is_superuser() and not user_has_min_role_anywhere("operator"):
        # No specific parent named yet (first GET to the unscoped form) —
        # only block outright if the user has no creatable parent at all;
        # otherwise let them through to a dropdown already filtered to
        # what they can actually create under.
        abort(403)
    if request.method == "POST":
        prop = Property(
            client_id=int(request.form["client_id"]),
            name=request.form["name"],
            address=request.form.get("address") or None,
        )
        db.session.add(prop)
        db.session.commit()
        flash(f"Property {prop.name!r} created.", "success")
        return redirect(url_for("admin.list_properties", client_id=prop.client_id))
    return render_template("entity_form.html", title="New Property", fields=_property_fields(), action=url_for("admin.new_property"))


@admin_bp.route("/properties/<int:property_id>/edit", methods=["GET", "POST"])
def edit_property(property_id):
    prop = Property.query.get_or_404(property_id)
    require_role("operator", prop)
    if request.method == "POST":
        if is_superuser():
            prop.client_id = int(request.form["client_id"])
        # else: parent is immutable for scoped users — ignore the submitted value.
        prop.name = request.form["name"]
        prop.address = request.form.get("address") or None
        db.session.commit()
        flash(f"Property {prop.name!r} updated.", "success")
        return redirect(url_for("admin.list_properties", client_id=prop.client_id))
    return render_template(
        "entity_form.html", title=f"Edit Property: {prop.name}", fields=_property_fields(prop),
        action=url_for("admin.edit_property", property_id=prop.id),
    )


@admin_bp.route("/properties/<int:property_id>/delete", methods=["POST"])
def delete_property(property_id):
    prop = Property.query.get_or_404(property_id)
    require_role("admin", prop)
    client_id = prop.client_id
    n = _soft_delete_cascade(prop)
    db.session.commit()
    flash(f"Archived {prop.name!r} and {n - 1} descendant record(s).", "success")
    return redirect(url_for("admin.list_properties", client_id=client_id))


@admin_bp.route("/properties/<int:property_id>/restore", methods=["POST"])
def restore_property(property_id):
    prop = Property.query.get_or_404(property_id)
    require_role("admin", prop)
    prop.is_active = True
    db.session.commit()
    flash(f"Restored {prop.name!r}.", "success")
    return redirect(url_for("admin.list_properties", client_id=prop.client_id))


# ─── Building ────────────────────────────────────────────────────────────

@admin_bp.route("/buildings")
def list_buildings():
    show_archived = _bool_arg("show_archived")
    property_id = _parse_int(request.args.get("property_id", ""))
    filter_property = Property.query.get(property_id) if property_id else None
    base = Building.query if is_superuser() else visible_building_query(current_user().id)
    q = base if show_archived else base.filter_by(is_active=True)
    if filter_property:
        q = q.filter_by(property_id=property_id)
    buildings = q.order_by(Building.name).all()
    rows = []
    for b in buildings:
        contract = current_contract(b)
        contract_cell = f"{contract.unit_count} unit(s) @ {_dollars(contract.rate_per_unit_cents)}/yr" if contract else "—"
        rows.append({
            "id": b.id,
            "is_active": b.is_active,
            "cells": [b.name, b.property.name, contract_cell],
            "can_edit": _can_edit(b),
            "can_archive": _can_archive(b),
            # Contract management is Superuser-only (Phase 4) even though
            # this list itself is visible to scoped viewer/operator/admin
            # users too — only show the link when it would actually work.
            "extra_url": url_for("admin.building_contract", building_id=b.id) if is_superuser() else None,
            "extra_label": "Manage Contract",
        })
    can_create = is_superuser() or user_has_min_role_anywhere("operator")
    if property_id:
        new_url = url_for("admin.new_building", property_id=property_id) if can_create else None
    else:
        new_url = url_for("admin.new_building") if can_create else None
    return render_template(
        "entity_list.html",
        title="Buildings" + (f" — {filter_property.name}" if filter_property else ""),
        columns=["Name", "Property", "Current Contract"],
        rows=rows,
        entity="buildings",
        entity_singular="building",
        show_archived=show_archived,
        toggle_archived_url=_toggle_archived_url(show_archived),
        new_url=new_url,
    )


def _building_fields(bld=None):
    if bld is not None and not is_superuser():
        properties = [bld.property]
        disabled = True
    else:
        properties = (
            Property.query.filter_by(is_active=True).order_by(Property.name).all()
            if is_superuser() else
            creatable_building_property_query(current_user().id).filter_by(is_active=True).order_by(Property.name).all()
        )
        disabled = False
    default_property_id = bld.property_id if bld else _parse_int(request.args.get("property_id", ""))
    return [
        {
            "name": "property_id", "label": "Property", "type": "select", "required": True,
            "value": default_property_id, "options": [(p.id, p.name) for p in properties], "disabled": disabled,
        },
        {"name": "name", "label": "Name", "type": "text", "required": True, "value": bld.name if bld else ""},
        {"name": "address", "label": "Address", "type": "textarea", "required": False, "value": (bld.address or "") if bld else ""},
    ]


@admin_bp.route("/buildings/new", methods=["GET", "POST"])
def new_building():
    property_id = _parse_int(request.form.get("property_id") or request.args.get("property_id", ""))
    if property_id:
        require_role("operator", Property.query.get_or_404(property_id))
    elif not is_superuser() and not user_has_min_role_anywhere("operator"):
        abort(403)
    if request.method == "POST":
        bld = Building(
            property_id=int(request.form["property_id"]),
            name=request.form["name"],
            address=request.form.get("address") or None,
        )
        db.session.add(bld)
        db.session.commit()
        flash(f"Building {bld.name!r} created.", "success")
        return redirect(url_for("admin.list_buildings", property_id=bld.property_id))
    return render_template("entity_form.html", title="New Building", fields=_building_fields(), action=url_for("admin.new_building"))


@admin_bp.route("/buildings/<int:building_id>/edit", methods=["GET", "POST"])
def edit_building(building_id):
    bld = Building.query.get_or_404(building_id)
    require_role("operator", bld)
    if request.method == "POST":
        if is_superuser():
            bld.property_id = int(request.form["property_id"])
        # else: parent is immutable for scoped users — ignore the submitted value.
        bld.name = request.form["name"]
        bld.address = request.form.get("address") or None
        db.session.commit()
        flash(f"Building {bld.name!r} updated.", "success")
        return redirect(url_for("admin.list_buildings", property_id=bld.property_id))
    return render_template(
        "entity_form.html", title=f"Edit Building: {bld.name}", fields=_building_fields(bld),
        action=url_for("admin.edit_building", building_id=bld.id),
    )


@admin_bp.route("/buildings/<int:building_id>/delete", methods=["POST"])
def delete_building(building_id):
    bld = Building.query.get_or_404(building_id)
    require_role("admin", bld)
    property_id = bld.property_id
    n = _soft_delete_cascade(bld)
    db.session.commit()
    flash(f"Archived {bld.name!r} and {n - 1} descendant record(s).", "success")
    return redirect(url_for("admin.list_buildings", property_id=property_id))


@admin_bp.route("/buildings/<int:building_id>/restore", methods=["POST"])
def restore_building(building_id):
    bld = Building.query.get_or_404(building_id)
    require_role("admin", bld)
    bld.is_active = True
    db.session.commit()
    flash(f"Restored {bld.name!r}.", "success")
    return redirect(url_for("admin.list_buildings", property_id=bld.property_id))


# ─── Controller ──────────────────────────────────────────────────────────

@admin_bp.route("/controllers")
def list_controllers():
    show_archived = _bool_arg("show_archived")
    building_id = _parse_int(request.args.get("building_id", ""))
    filter_building = Building.query.get(building_id) if building_id else None
    base = Controller.query if is_superuser() else visible_controller_query(current_user().id)
    q = base if show_archived else base.filter_by(is_active=True)
    if filter_building:
        q = q.filter_by(building_id=building_id)
    controllers = q.order_by(Controller.name).all()
    rows = [
        {
            "id": c.id,
            "is_active": c.is_active,
            "cells": [c.name, c.building.name, c.tailscale_hostname or "—"],
            "can_edit": _can_edit(c),
            "can_archive": _can_archive(c),
        }
        for c in controllers
    ]
    can_create = is_superuser() or user_has_min_role_anywhere("operator")
    if building_id:
        new_url = url_for("admin.new_controller", building_id=building_id) if can_create else None
    else:
        new_url = url_for("admin.new_controller") if can_create else None
    return render_template(
        "entity_list.html",
        title="Controllers" + (f" — {filter_building.name}" if filter_building else ""),
        columns=["Name", "Building", "Tailscale Host"],
        rows=rows,
        entity="controllers",
        entity_singular="controller",
        show_archived=show_archived,
        toggle_archived_url=_toggle_archived_url(show_archived),
        new_url=new_url,
    )


def _controller_fields(ctrl=None):
    if ctrl is not None and not is_superuser():
        buildings = [ctrl.building]
        disabled = True
    else:
        buildings = (
            Building.query.filter_by(is_active=True).order_by(Building.name).all()
            if is_superuser() else
            creatable_controller_building_query(current_user().id).filter_by(is_active=True).order_by(Building.name).all()
        )
        disabled = False
    default_building_id = ctrl.building_id if ctrl else _parse_int(request.args.get("building_id", ""))
    return [
        {
            "name": "building_id", "label": "Building", "type": "select", "required": True,
            "value": default_building_id, "options": [(b.id, b.name) for b in buildings], "disabled": disabled,
        },
        {"name": "name", "label": "Name", "type": "text", "required": True, "value": ctrl.name if ctrl else ""},
        {
            "name": "tailscale_ip", "label": "Tailscale IP", "type": "text",
            "required": False, "value": (ctrl.tailscale_ip or "") if ctrl else "",
        },
        {
            "name": "tailscale_hostname", "label": "Tailscale Hostname (informational — MagicDNS not assumed)",
            "type": "text", "required": False, "value": (ctrl.tailscale_hostname or "") if ctrl else "",
        },
        {
            "name": "fleet_api_token",
            "label": "Fleet API Token" + (" (set — leave blank to keep)" if ctrl and ctrl.fleet_api_token else ""),
            "type": "password", "required": False, "value": "",
        },
    ]


@admin_bp.route("/controllers/new", methods=["GET", "POST"])
def new_controller():
    building_id = _parse_int(request.form.get("building_id") or request.args.get("building_id", ""))
    if building_id:
        require_role("operator", Building.query.get_or_404(building_id))
    elif not is_superuser() and not user_has_min_role_anywhere("operator"):
        abort(403)
    if request.method == "POST":
        ctrl = Controller(
            building_id=int(request.form["building_id"]),
            name=request.form["name"],
            tailscale_hostname=request.form.get("tailscale_hostname") or None,
            tailscale_ip=request.form.get("tailscale_ip") or None,
            fleet_api_token=request.form.get("fleet_api_token") or None,
        )
        db.session.add(ctrl)
        db.session.commit()
        flash(f"Controller {ctrl.name!r} created.", "success")
        return redirect(url_for("admin.list_controllers", building_id=ctrl.building_id))
    return render_template("entity_form.html", title="New Controller", fields=_controller_fields(), action=url_for("admin.new_controller"))


@admin_bp.route("/controllers/<int:controller_id>/edit", methods=["GET", "POST"])
def edit_controller(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("operator", ctrl)
    if request.method == "POST":
        if is_superuser():
            ctrl.building_id = int(request.form["building_id"])
        # else: parent is immutable for scoped users — ignore the submitted value.
        ctrl.name = request.form["name"]
        ctrl.tailscale_hostname = request.form.get("tailscale_hostname") or None
        ctrl.tailscale_ip = request.form.get("tailscale_ip") or None
        new_token = request.form.get("fleet_api_token")
        if new_token:
            ctrl.fleet_api_token = new_token
        db.session.commit()
        flash(f"Controller {ctrl.name!r} updated.", "success")
        return redirect(url_for("admin.list_controllers", building_id=ctrl.building_id))
    return render_template(
        "entity_form.html", title=f"Edit Controller: {ctrl.name}", fields=_controller_fields(ctrl),
        action=url_for("admin.edit_controller", controller_id=ctrl.id),
    )


@admin_bp.route("/controllers/<int:controller_id>/delete", methods=["POST"])
def delete_controller(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("admin", ctrl)
    building_id = ctrl.building_id
    _soft_delete_cascade(ctrl)
    db.session.commit()
    flash(f"Archived {ctrl.name!r}.", "success")
    return redirect(url_for("admin.list_controllers", building_id=building_id))


@admin_bp.route("/controllers/<int:controller_id>/restore", methods=["POST"])
def restore_controller(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("admin", ctrl)
    ctrl.is_active = True
    db.session.commit()
    flash(f"Restored {ctrl.name!r}.", "success")
    return redirect(url_for("admin.list_controllers", building_id=ctrl.building_id))


# ─── Fleet Status (Phase 2 — read-only status rollup) ───────────────────

@admin_bp.route("/fleet")
def fleet_status():
    base = Controller.query if is_superuser() else visible_controller_query(current_user().id)
    controllers = base.filter_by(is_active=True).order_by(Controller.name).all()
    return render_template("fleet_status.html", title="Fleet Status", controllers=controllers)


@admin_bp.route("/fleet/<int:controller_id>/poll", methods=["POST"])
def fleet_poll_now(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    # Polling is a status check, not a control action or a portal-data
    # mutation — anyone who can see this controller may refresh its status.
    require_role("viewer", ctrl)
    if not ctrl.tailscale_ip or not ctrl.fleet_api_token:
        flash(f"{ctrl.name!r} has no Tailscale IP / token configured.", "error")
        return redirect(url_for("admin.fleet_status"))
    row = poll_controller(current_app._get_current_object(), ctrl)
    flash(f"Polled {ctrl.name!r}: {'OK' if row.ok else 'FAILED — ' + (row.error or '')}",
          "success" if row.ok else "error")
    return redirect(url_for("admin.fleet_status"))


# ─── Users & Permissions (Phase 3 — Superuser-only) ──────────────────────

_SCOPE_MODELS = {"client": Client, "property": Property, "building": Building}


def _scope_label(scope_type: str, scope_id: int) -> str:
    model = _SCOPE_MODELS.get(scope_type)
    entity = model.query.get(scope_id) if model else None
    return f"{scope_type.capitalize()}: {entity.name if entity else f'#{scope_id} (deleted)'}"


@admin_bp.route("/users")
@superuser_required
def list_users():
    show_archived = _bool_arg("show_archived")
    q = User.query if show_archived else User.query.filter_by(is_active=True)
    users = q.order_by(User.name).all()
    rows = [
        {
            "id": u.id,
            "is_active": u.is_active,
            "cells": [u.name, u.email, u.login],
            "can_edit": True,
            "can_archive": True,
            "extra_url": url_for("admin.user_permissions", user_id=u.id),
        }
        for u in users
    ]
    return render_template(
        "entity_list.html",
        title="Users",
        columns=["Name", "Email", "Login"],
        rows=rows,
        entity="users",
        entity_singular="user",
        show_archived=show_archived,
        toggle_archived_url=_toggle_archived_url(show_archived),
        new_url=url_for("admin.new_user"),
    )


def _user_fields(user=None):
    return [
        {"name": "name", "label": "Name", "type": "text", "required": True, "value": user.name if user else ""},
        {"name": "email", "label": "Email", "type": "email", "required": True, "value": user.email if user else ""},
        {"name": "login", "label": "Login", "type": "text", "required": True, "value": user.login if user else ""},
        {
            "name": "password",
            "label": "Password" + (" (set — leave blank to keep)" if user else ""),
            "type": "password", "required": user is None, "value": "",
        },
    ]


@admin_bp.route("/users/new", methods=["GET", "POST"])
@superuser_required
def new_user():
    if request.method == "POST":
        login = request.form["login"]
        if login == current_app.config["ADMIN_USERNAME"]:
            flash("That login is reserved for the Superuser account.", "error")
            return render_template("entity_form.html", title="New User", fields=_user_fields(), action=url_for("admin.new_user"))
        user = User(
            name=request.form["name"],
            email=request.form["email"],
            login=login,
            password_hash=generate_password_hash(request.form["password"]),
        )
        db.session.add(user)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Email or login already in use.", "error")
            return render_template("entity_form.html", title="New User", fields=_user_fields(), action=url_for("admin.new_user"))
        flash(f"User {user.name!r} created.", "success")
        return redirect(url_for("admin.list_users"))
    return render_template("entity_form.html", title="New User", fields=_user_fields(), action=url_for("admin.new_user"))


@admin_bp.route("/users/<int:user_id>/edit", methods=["GET", "POST"])
@superuser_required
def edit_user(user_id):
    user = User.query.get_or_404(user_id)
    if request.method == "POST":
        login = request.form["login"]
        if login == current_app.config["ADMIN_USERNAME"]:
            flash("That login is reserved for the Superuser account.", "error")
            return render_template(
                "entity_form.html", title=f"Edit User: {user.name}", fields=_user_fields(user),
                action=url_for("admin.edit_user", user_id=user.id),
            )
        user.name = request.form["name"]
        user.email = request.form["email"]
        user.login = login
        new_password = request.form.get("password")
        if new_password:
            user.password_hash = generate_password_hash(new_password)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("Email or login already in use.", "error")
            return render_template(
                "entity_form.html", title=f"Edit User: {user.name}", fields=_user_fields(user),
                action=url_for("admin.edit_user", user_id=user.id),
            )
        flash(f"User {user.name!r} updated.", "success")
        return redirect(url_for("admin.list_users"))
    return render_template(
        "entity_form.html", title=f"Edit User: {user.name}", fields=_user_fields(user),
        action=url_for("admin.edit_user", user_id=user.id),
    )


@admin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@superuser_required
def delete_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = False
    db.session.commit()
    flash(f"Archived {user.name!r}.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/users/<int:user_id>/restore", methods=["POST"])
@superuser_required
def restore_user(user_id):
    user = User.query.get_or_404(user_id)
    user.is_active = True
    db.session.commit()
    flash(f"Restored {user.name!r}.", "success")
    return redirect(url_for("admin.list_users"))


@admin_bp.route("/users/<int:user_id>/permissions")
@superuser_required
def user_permissions(user_id):
    user = User.query.get_or_404(user_id)
    grants = Permission.query.filter_by(user_id=user.id).order_by(Permission.scope_type, Permission.scope_id).all()
    grant_rows = [
        {"id": p.id, "label": _scope_label(p.scope_type, p.scope_id), "role": p.role}
        for p in grants
    ]
    clients = Client.query.filter_by(is_active=True).order_by(Client.name).all()
    properties = Property.query.filter_by(is_active=True).order_by(Property.name).all()
    buildings = Building.query.filter_by(is_active=True).order_by(Building.name).all()
    return render_template(
        "user_permissions.html", title=f"Permissions: {user.name}", user=user, grants=grant_rows,
        clients=clients, properties=properties, buildings=buildings,
    )


@admin_bp.route("/users/<int:user_id>/permissions", methods=["POST"])
@superuser_required
def add_permission(user_id):
    user = User.query.get_or_404(user_id)
    scope_type = request.form.get("scope_type", "")
    role = request.form.get("role", "")
    scope_id = _parse_int(request.form.get("scope_id", ""))
    model = _SCOPE_MODELS.get(scope_type)
    if model is None or role not in ROLE_RANK or scope_id is None:
        flash("Invalid grant.", "error")
        return redirect(url_for("admin.user_permissions", user_id=user.id))
    model.query.get_or_404(scope_id)  # confirms the target actually exists
    db.session.add(Permission(user_id=user.id, scope_type=scope_type, scope_id=scope_id, role=role))
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        flash("A grant for this scope already exists — revoke it first to change the role.", "error")
    else:
        flash("Grant added.", "success")
    return redirect(url_for("admin.user_permissions", user_id=user.id))


@admin_bp.route("/users/<int:user_id>/permissions/<int:permission_id>/revoke", methods=["POST"])
@superuser_required
def revoke_permission(user_id, permission_id):
    grant = Permission.query.filter_by(id=permission_id, user_id=user_id).first_or_404()
    db.session.delete(grant)
    db.session.commit()
    flash("Grant revoked.", "success")
    return redirect(url_for("admin.user_permissions", user_id=user_id))


# ─── Billing (Phase 4 — Superuser-only) ──────────────────────────────────

@admin_bp.route("/billing")
@superuser_required
def billing_dashboard():
    today = datetime.utcnow().date()
    period_start, period_end = period_for_date(today)
    drafts = Invoice.query.filter_by(status="draft").order_by(Invoice.period_start.desc(), Invoice.id).all()
    pending = Invoice.query.filter_by(status="pending").order_by(Invoice.due_date).all()
    recent_paid = Invoice.query.filter_by(status="paid").order_by(Invoice.updated_at.desc()).limit(20).all()
    return render_template(
        "billing_dashboard.html", title="Billing", period_start=period_start, period_end=period_end,
        drafts=drafts, pending=pending, recent_paid=recent_paid, today=today,
    )


@admin_bp.route("/billing/generate", methods=["POST"])
@superuser_required
def billing_generate_now():
    period_start, period_end = period_for_date(datetime.utcnow().date())
    created = generate_invoices(period_start, period_end)
    if created:
        flash(f"Generated {len(created)} draft invoice(s) for {period_start.isoformat()}–{period_end.isoformat()}.", "success")
    else:
        flash("No new invoices to generate — every eligible building already has one for this period.", "success")
    return redirect(url_for("admin.billing_dashboard"))


@admin_bp.route("/billing/invoices/<int:invoice_id>")
@superuser_required
def invoice_detail(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    is_overdue = invoice.status == "pending" and invoice.due_date is not None and invoice.due_date < datetime.utcnow().date()
    return render_template(
        "invoice_detail.html", title=f"Invoice #{invoice.id}", invoice=invoice,
        is_overdue=is_overdue, stripe_configured=stripe_configured(),
    )


@admin_bp.route("/billing/invoices/<int:invoice_id>/approve", methods=["POST"])
@superuser_required
def approve_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.status != "draft":
        flash("Only draft invoices can be approved.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    invoice.status = "pending"
    invoice.issued_date = datetime.utcnow().date()
    invoice.due_date = invoice.issued_date + timedelta(days=INVOICE_DUE_DAYS)
    db.session.commit()

    client = invoice.building.property.client
    if client.billing_method == "auto_charge" and stripe_configured():
        try:
            create_checkout_session(invoice)
        except Exception:
            current_app.logger.exception("Stripe: failed to auto-create Checkout Session for invoice %s", invoice.id)
            flash(
                f"Invoice #{invoice.id} approved and issued — due {invoice.due_date.isoformat()}, "
                "but the Stripe payment link could not be created automatically (see logs); "
                "use ‘Create Payment Link’ on the invoice to retry.", "error",
            )
            return redirect(url_for("admin.billing_dashboard"))

    flash(f"Invoice #{invoice.id} approved and issued — due {invoice.due_date.isoformat()}.", "success")
    return redirect(url_for("admin.billing_dashboard"))


@admin_bp.route("/billing/invoices/<int:invoice_id>/void", methods=["POST"])
@superuser_required
def void_invoice(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.status != "draft":
        flash("Only draft invoices can be voided.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    invoice.status = "void"
    db.session.commit()
    flash(f"Invoice #{invoice.id} voided.", "success")
    return redirect(url_for("admin.billing_dashboard"))


@admin_bp.route("/billing/invoices/<int:invoice_id>/pay", methods=["POST"])
@superuser_required
def record_payment(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.status != "pending":
        flash("Only pending (issued) invoices can be marked paid.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    method = request.form.get("method", "")
    paid_date = _parse_date(request.form.get("paid_date", "")) or datetime.utcnow().date()
    try:
        amount_cents = round(float(request.form.get("amount_dollars", "0")) * 100)
    except ValueError:
        amount_cents = 0
    if amount_cents <= 0 or method not in {"check", "ach", "wire", "card", "other"}:
        flash("Invalid payment.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    payment = _finalize_payment(invoice, amount_cents, paid_date, method)
    if payment is None:
        flash("Invoice is no longer pending — payment not recorded.", "error")
    else:
        flash(f"Payment of {_dollars(amount_cents)} recorded — invoice #{invoice.id} marked paid.", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))


@admin_bp.route("/billing/invoices/<int:invoice_id>/create-payment-link", methods=["POST"])
@superuser_required
def create_payment_link(invoice_id):
    invoice = Invoice.query.get_or_404(invoice_id)
    if invoice.status != "pending":
        flash("Only pending (issued) invoices can have a payment link created.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    if not stripe_configured():
        flash("Stripe isn't configured on this server — set STRIPE_SECRET_KEY to enable payment links.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    try:
        create_checkout_session(invoice)
    except Exception:
        current_app.logger.exception("Stripe: failed to create Checkout Session for invoice %s", invoice.id)
        flash("Stripe error creating the payment link — see server logs.", "error")
        return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))
    flash("Payment link created.", "success")
    return redirect(url_for("admin.invoice_detail", invoice_id=invoice.id))


@admin_bp.route("/webhooks/stripe", methods=["POST"])
@csrf.exempt
def stripe_webhook():
    """Stripe calls this unauthenticated, with no CSRF token — exempted
    from both the login gate (see admin_auth.py's _EXEMPT_ENDPOINTS) and
    Flask-WTF's CSRF protection. A verified cryptographic signature
    (STRIPE_WEBHOOK_SECRET) stands in for both of those instead."""
    try:
        event = verify_stripe_webhook(request.get_data(), request.headers.get("Stripe-Signature", ""))
    except WebhookVerificationError:
        current_app.logger.warning("Stripe webhook: rejected (missing/invalid signature or not configured).")
        abort(400)
    mark_paid_from_stripe_event(event)
    return ("", 200)


# ─── Building Contracts (Phase 4 — Superuser-only) ───────────────────────

@admin_bp.route("/buildings/<int:building_id>/contract")
@superuser_required
def building_contract(building_id):
    bld = Building.query.get_or_404(building_id)
    contracts = Contract.query.filter_by(building_id=bld.id).order_by(Contract.start_date.desc()).all()
    return render_template(
        "building_contract.html", title=f"Contract: {bld.name}", building=bld,
        contract=current_contract(bld), contracts=contracts,
    )


@admin_bp.route("/buildings/<int:building_id>/contract", methods=["POST"])
@superuser_required
def update_building_contract(building_id):
    bld = Building.query.get_or_404(building_id)
    unit_count = _parse_int(request.form.get("unit_count", ""))
    rate_per_unit_cents = _parse_int(request.form.get("rate_per_unit_cents", "")) or 1000
    start_date = _parse_date(request.form.get("start_date", "")) or datetime.utcnow().date()
    if not unit_count or unit_count <= 0:
        flash("Unit count must be a positive number.", "error")
        return redirect(url_for("admin.building_contract", building_id=bld.id))
    existing = current_contract(bld)
    if existing is not None:
        if start_date <= existing.start_date:
            flash("New contract's start date must be after the current contract's start date.", "error")
            return redirect(url_for("admin.building_contract", building_id=bld.id))
        existing.end_date = start_date - timedelta(days=1)
    db.session.add(Contract(
        building_id=bld.id, unit_count=unit_count,
        rate_per_unit_cents=rate_per_unit_cents, start_date=start_date, end_date=None,
    ))
    db.session.commit()
    flash(f"Contract updated: {unit_count} unit(s) @ {_dollars(rate_per_unit_cents)}/yr, effective {start_date.isoformat()}.", "success")
    return redirect(url_for("admin.building_contract", building_id=bld.id))
