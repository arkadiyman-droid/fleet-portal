from datetime import datetime

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, url_for
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.extensions import db
from app.fleet_poller import poll_controller
from app.models import Building, Client, Controller, Permission, Property, User
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
    rows = [
        {
            "id": b.id,
            "is_active": b.is_active,
            "cells": [b.name, b.property.name, str(b.billed_unit_count) if b.billed_unit_count is not None else "—"],
            "can_edit": _can_edit(b),
            "can_archive": _can_archive(b),
        }
        for b in buildings
    ]
    can_create = is_superuser() or user_has_min_role_anywhere("operator")
    if property_id:
        new_url = url_for("admin.new_building", property_id=property_id) if can_create else None
    else:
        new_url = url_for("admin.new_building") if can_create else None
    return render_template(
        "entity_list.html",
        title="Buildings" + (f" — {filter_property.name}" if filter_property else ""),
        columns=["Name", "Property", "Billed Units"],
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
        {
            "name": "billed_unit_count", "label": "Billed Unit Count (contractual)", "type": "number", "required": False,
            "value": bld.billed_unit_count if bld and bld.billed_unit_count is not None else "",
        },
        {
            "name": "contract_start_date", "label": "Contract Start Date", "type": "date", "required": False,
            "value": bld.contract_start_date.isoformat() if bld and bld.contract_start_date else "",
        },
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
            billed_unit_count=_parse_int(request.form.get("billed_unit_count", "")),
            contract_start_date=_parse_date(request.form.get("contract_start_date", "")),
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
        bld.billed_unit_count = _parse_int(request.form.get("billed_unit_count", ""))
        bld.contract_start_date = _parse_date(request.form.get("contract_start_date", ""))
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
