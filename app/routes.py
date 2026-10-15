from datetime import datetime, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   session, stream_with_context, url_for)
from sqlalchemy.exc import IntegrityError
from werkzeug.security import generate_password_hash

from app.billing import INVOICE_DUE_DAYS, _finalize_payment, current_contract, generate_invoices, period_for_date
from app.extensions import csrf, db
from app import fleet_health
from app.enroll_api import code_hash as enroll_code_hash, normalize_code
from app.fleet_poller import poll_controller
from app.models import (AlertEvent, AlertState, Building, Client, Contract, Controller, Enrollment, Invoice,
                        LegacyAlertState, LegacyBox, Permission, Property, Rollout, RolloutTarget, User)
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
    return redirect(url_for("admin.fleet_overview"))


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
        return redirect(_local_next(url_for("admin.fleet_status")))
    row = poll_controller(current_app._get_current_object(), ctrl)
    flash(f"Polled {ctrl.name!r}: {'OK' if row.ok else 'FAILED — ' + (row.error or '')}",
          "success" if row.ok else "error")
    return redirect(_local_next(url_for("admin.fleet_status")))


def _local_next(default):
    """Back to the page the button was on -- but only a path on this site,
    never an absolute URL a crafted form could smuggle in."""
    nxt = request.form.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return default


# ─── Fleet Overview (the dashboard home) ────────────────────────────────

_LEVEL_ORDER = {"crit": 0, "warn": 1, "info": 2, "unknown": 3, "ok": 4}


def _card(ctrl):
    b = ctrl.building
    p = b.property if b else None
    return {"ctrl": ctrl, "building": b, "property": p, "client": p.client if p else None,
            "eval": fleet_health.evaluate(ctrl, ctrl.status)}


def _fleet_cards():
    base = Controller.query if is_superuser() else visible_controller_query(current_user().id)
    cards = [_card(ctrl) for ctrl in base.filter_by(is_active=True).all()]
    cards.sort(key=lambda k: (_LEVEL_ORDER.get(k["eval"]["level"], 9),
                              (k["client"].name if k["client"] else ""), k["ctrl"].name))
    return cards


_FILTERS = {
    "attention": lambda e: e["level"] in ("warn", "crit"),
    "offline":   lambda e: e["online"] is False,
    "alarms":    lambda e: bool(e["alarms"]),
    "backups":   lambda e: any(w in t for _, t in e["issues"] for w in ("ackup", "undle", "istory")),
}


@admin_bp.route("/overview")
def fleet_overview():
    cards = _fleet_cards()
    summary = fleet_health.summarize(cards)
    show = request.args.get("show", "all")
    if show in _FILTERS:
        cards = [k for k in cards if _FILTERS[show](k["eval"])]
    else:
        show = "all"
    return render_template("fleet_overview.html", title="Fleet Overview", cards=cards, summary=summary,
                           show=show, poll_secs=current_app.config.get("FLEET_POLL_INTERVAL_SECONDS", 60),
                           waiting=len(_waiting_modules()) if _can_adopt() else 0)


@admin_bp.route("/fleet/<int:controller_id>")
def fleet_controller(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("viewer", ctrl)
    return render_template("fleet_controller.html", title=ctrl.name, card=_card(ctrl), status=ctrl.status,
                           raw=(ctrl.status.raw_json if ctrl.status else None) or {}, now_utc=datetime.utcnow())


# ─── Alerts (app/alerts.py decides, app/notify.py sends) ────────────────

MUTE_CHOICES = {"4": 4, "24": 24, "72": 72}


def _visible_controller_ids():
    if is_superuser():
        return None                                   # all of them
    return [c.id for c in visible_controller_query(current_user().id).all()]


@admin_bp.app_context_processor
def _alert_badge():
    """The red count next to "Alerts" in the menu: open alerts this person can see."""
    try:
        if not getattr(g, "is_superuser", False) and not getattr(g, "user", None):
            return {"open_alerts": 0}
        q = AlertState.query.filter(AlertState.alerting.is_(True))
        ids = _visible_controller_ids()
        if ids is not None:
            q = q.filter(AlertState.controller_id.in_(ids or [-1]))
        n = q.count()
        if ids is None:                               # the 2010 boxes are the administrator's for now
            n += _legacy_alert_states().filter(LegacyAlertState.alerting.is_(True)).count()
        return {"open_alerts": n}
    except Exception:
        return {"open_alerts": 0}


def _legacy_alert_states():
    return LegacyAlertState.query.join(LegacyBox, LegacyBox.id == LegacyAlertState.box_id) \
        .filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None))


@admin_bp.route("/alerts")
def alerts_page():
    from app import notify
    ids = _visible_controller_ids()
    states = AlertState.query.join(Controller).filter(Controller.is_active.is_(True))
    events = AlertEvent.query
    if ids is not None:
        states = states.filter(AlertState.controller_id.in_(ids or [-1]))
        events = events.filter(AlertEvent.controller_id.in_(ids or [-1]))
    states = states.all()
    now = datetime.utcnow()
    open_ = sorted([s for s in states if s.alerting], key=lambda s: s.opened_at or now)
    waiting = [s for s in states if not s.alerting and s.first_bad_at]
    muted = [s for s in states if s.muted_until and s.muted_until > now]
    lstates = _legacy_alert_states().all() if ids is None else []
    l_open = sorted([s for s in lstates if s.alerting], key=lambda s: s.opened_at or now)
    l_waiting = [s for s in lstates if not s.alerting and s.first_bad_at]
    l_muted = [s for s in lstates if s.muted_until and s.muted_until > now]
    cfg = current_app.config
    ntfy_qr = None
    if is_superuser() and cfg.get("ALERT_NTFY_TOPIC"):
        import segno
        from markupsafe import Markup
        url = f"{cfg.get('ALERT_NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{cfg['ALERT_NTFY_TOPIC']}"
        ntfy_qr = Markup(segno.make(url, error="m").svg_inline(scale=4, border=2))
    return render_template("alerts.html", title="Alerts", open_=open_, waiting=waiting, muted=muted, ntfy_qr=ntfy_qr,
                           l_open=l_open, l_waiting=l_waiting, l_muted=l_muted, show_legacy=ids is None,
                           legacy_dark_min=cfg.get("LEGACY_DARK_MINUTES", 30),
                           events=events.order_by(AlertEvent.created_at.desc()).limit(100).all(),
                           channels=notify.channels(), now=now,
                           after_min=round(cfg.get("ALERT_AFTER_SECONDS", 300) / 60),
                           repeat_h=cfg.get("ALERT_REPEAT_HOURS", 24),
                           ntfy_server=cfg.get("ALERT_NTFY_SERVER", "https://ntfy.sh"),
                           ntfy_topic=cfg.get("ALERT_NTFY_TOPIC", "") if is_superuser() else "",
                           email_to=cfg.get("ALERT_EMAIL_TO", "") if is_superuser() else "")


@admin_bp.route("/alerts/test", methods=["POST"])
def alerts_test():
    if not is_superuser():
        abort(403)
    from app import alerts
    result = alerts.send_test("the administrator")
    flash(f"Test alert: {result}", "success" if " ok" in result and "failed" not in result else "error")
    return redirect(url_for("admin.alerts_page"))


@admin_bp.route("/fleet/<int:controller_id>/mute", methods=["POST"])
def fleet_mute(controller_id):
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("operator", ctrl)
    row = db.session.get(AlertState, ctrl.id) or AlertState(controller_id=ctrl.id, alerting=False)
    hours = MUTE_CHOICES.get(request.form.get("hours", ""))
    u = current_user()
    who = "the administrator" if is_superuser() else (u.login if u else "?")
    if hours:
        row.muted_until = datetime.utcnow() + timedelta(hours=hours)
        row.muted_by = who
        msg = f"Alerts for {ctrl.name!r} muted for {hours} hours."
    else:
        row.muted_until, row.muted_by = None, None
        msg = f"Alerts for {ctrl.name!r} are on again."
    db.session.add(row)
    db.session.add(AlertEvent(controller_id=ctrl.id, controller_name=ctrl.name[:200], kind="mute",
                              title=msg[:200], text=f"by {who}", delivered="not sent", created_at=datetime.utcnow()))
    db.session.commit()
    flash(msg, "success")
    return redirect(_local_next(url_for("admin.fleet_controller", controller_id=ctrl.id)))


@admin_bp.route("/live/<int:box_id>/mute", methods=["POST"])
def live_mute(box_id):
    if not is_superuser():
        abort(403)
    b = LegacyBox.query.get_or_404(box_id)
    if b.deleted_at:
        abort(404)
    row = db.session.get(LegacyAlertState, b.id) or LegacyAlertState(box_id=b.id, alerting=False)
    hours = MUTE_CHOICES.get(request.form.get("hours", ""))
    who = _who()
    if hours:
        row.muted_until, row.muted_by = datetime.utcnow() + timedelta(hours=hours), who
        msg = f"Alerts for {b.name!r} muted for {hours} hours."
    else:
        row.muted_until, row.muted_by = None, None
        msg = f"Alerts for {b.name!r} are on again."
    db.session.add(row)
    db.session.add(AlertEvent(legacy_box_id=b.id, controller_name=b.name[:200], kind="mute",
                              title=msg[:200], text=f"by {who}", delivered="not sent", created_at=datetime.utcnow()))
    db.session.commit()
    flash(msg, "success")
    return redirect(_local_next(url_for("admin.live_box", box_id=b.id)))


# ─── Software updates (app/rollouts.py) ─────────────────────────────────

def _who():
    u = current_user()
    return "the administrator" if is_superuser() else (u.login if u else "?")


def _polled_controllers():
    base = Controller.query if is_superuser() else visible_controller_query(current_user().id)
    return [c for c in base.filter_by(is_active=True).order_by(Controller.name).all()
            if c.tailscale_ip and c.fleet_api_token]


@admin_bp.route("/updates")
def updates_page():
    from app import rollouts
    ctrls = _polled_controllers()
    versions = {}
    for c in ctrls:
        versions.setdefault(rollouts.current_sha(c) or "", []).append(c)
    releases = rollouts.releases_seen(ctrls)
    newest = next(iter(releases), None)
    ids = {c.id for c in ctrls}
    rs = Rollout.query.order_by(Rollout.id.desc()).limit(15).all()
    if not is_superuser():
        rs = [r for r in rs if any(t.controller_id in ids for t in r.targets)]
    need = [c for c in ctrls if newest and rollouts.current_sha(c) and rollouts.current_sha(c) != newest]
    unknown = [c for c in ctrls if not rollouts.current_sha(c)]

    def _health_first(c):            # the test controller should be one that is up and well
        e = fleet_health.evaluate(c, c.status)
        return (e["online"] is not True, {"ok": 0, "info": 1, "unknown": 2, "warn": 3, "crit": 4}.get(e["level"], 5), c.name)
    test_choices = sorted(need, key=_health_first)
    return render_template("updates.html", title="Updates", versions=versions, releases=releases, newest=newest,
                           need=need, unknown=unknown, test_choices=test_choices, rollouts=rs,
                           active=[r for r in rs if r.state in ("running", "paused")],
                           past=[r for r in rs if r.state not in ("running", "paused")])


@admin_bp.route("/updates/start", methods=["POST"])
def updates_start():
    if not is_superuser():
        abort(403)
    from app import rollouts
    try:
        r = rollouts.create(request.form.get("sha"), _polled_controllers(),
                            _parse_int(request.form.get("test_controller_id")) or 0,
                            batch_size=_parse_int(request.form.get("batch_size")) or 10,
                            soak_minutes=_parse_int(request.form.get("soak_minutes")) or 0, by=_who())
    except ValueError as e:
        flash(f"Not started: {e}.", "error")
        return redirect(url_for("admin.updates_page"))
    flash(f"Rollout of {r.target_sha[:7]} started: the test controller goes first, within a minute.", "success")
    return redirect(url_for("admin.updates_page"))


@admin_bp.route("/updates/check", methods=["POST"])
def updates_check():
    if not is_superuser():
        abort(403)
    from app import rollouts
    res = rollouts.ask_to_check(current_app._get_current_object(), _polled_controllers())
    seen = ", ".join(f"{sha[:7]} ({n})" for sha, n in res["seen"].items()) or "nothing"
    flash(f"Asked {res['asked']} controller(s); {res['answered']} answered. Their release branch shows: {seen}. "
          "The page catches up within a minute, on their next status poll.",
          "success" if res["answered"] else "error")
    return redirect(url_for("admin.updates_page"))


@admin_bp.route("/updates/<int:rollout_id>/<action>", methods=["POST"])
def updates_control(rollout_id, action):
    if not is_superuser():
        abort(403)
    r = Rollout.query.get_or_404(rollout_id)
    if action == "pause" and r.state == "running":
        r.state = "paused"
    elif action == "resume" and r.state == "paused":
        r.state = "running"
    elif action == "cancel" and r.state in ("running", "paused"):
        r.state, r.reason, r.finished_at = "cancelled", f"cancelled by {_who()}", datetime.utcnow()
        for t in r.targets:
            if t.state == "pending":
                t.state, t.detail, t.finished_at = "skipped", "rollout cancelled", datetime.utcnow()
    else:
        abort(400)
    db.session.commit()
    flash({"pause": "Paused: nothing new starts; updates already running finish by themselves.",
           "resume": "Resumed.", "cancel": "Cancelled. Controllers already updated keep the release."}[action],
          "success")
    return redirect(url_for("admin.updates_page"))


@admin_bp.route("/fleet/<int:controller_id>/update", methods=["POST"])
def fleet_update_one(controller_id):
    from app import rollouts
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("operator", ctrl)
    sha = rollouts.ota(ctrl).get("availableSha")
    try:
        r = rollouts.create(sha, [ctrl], ctrl.id, batch_size=1, soak_minutes=0, by=_who())
    except ValueError as e:
        flash(f"Not started: {e}.", "error")
    else:
        flash(f"Updating {ctrl.name!r} to {r.target_sha[:7]}: it starts within a minute, then proves itself "
              "for 10 minutes. Follow it on the Updates page.", "success")
    return redirect(_local_next(url_for("admin.fleet_controller", controller_id=ctrl.id)))


# ─── Live view of the installed 2010 gateways (app/legacy.py) ───────────

_TONE_ORDER = {"crit": 0, "warn": 1, "heat": 2, "ok": 3, "idle": 4}


def _firing(now, box_ids):
    """{box_id: "heat" | "hot water" | "heat and hot water"} for boxes whose
    burner run counters went up between their last two reads: the burner
    fired in that interval. Anagha shows a fuel-valve flame; the 2010 box's
    live status has no such point (it is only in its activity records), so
    the counters are the evidence. Midnight resets count when the new value
    is above zero."""
    from sqlalchemy import text as _text
    if not box_ids:
        return {}
    heat = "coalesce(heat1,0)+coalesce(heat2,0)+coalesce(heat3,0)+coalesce(heat4,0)"
    hw = "coalesce(hw1,0)+coalesce(hw2,0)+coalesce(hw3,0)+coalesce(hw4,0)"
    try:
        rows = db.session.execute(_text(
            f"SELECT box_id, ts, {heat} AS heat, {hw} AS hw FROM legacy_readings "
            "WHERE ts >= :since AND box_id = ANY(:ids) ORDER BY box_id, ts DESC"),
            {"since": now - timedelta(minutes=13), "ids": list(box_ids)}).all()
    except Exception:
        db.session.rollback()
        return {}
    last, out = {}, {}
    for r in rows:
        last.setdefault(r.box_id, []).append(r)
    for box_id, (cur, prev, *_rest) in ((k, v) for k, v in last.items() if len(v) >= 2):
        if (cur.ts - prev.ts).total_seconds() > 11 * 60:
            continue
        went = lambda a, b: (b > a) or (b < a and b > 0)
        kinds = [w for w, a, b in (("heat", prev.heat, cur.heat), ("hot water", prev.hw, cur.hw)) if went(a, b)]
        if kinds:
            out[box_id] = " and ".join(kinds)
    return out


def _legacy_rows(now, boxes=None):
    from app import legacy
    rows = []
    if boxes is None:
        boxes = LegacyBox.query.filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None)) \
            .order_by(LegacyBox.name).all()
    firing = _firing(now, [b.id for b in boxes])
    for b in boxes:
        st = b.status
        c = legacy.card(st.data) if st and st.data else None
        last_ok = st.last_ok_at if st else None
        if b.identity == "other":
            state, why = "other", "Another device answers at this address: no login is sent there"
        elif st and st.auth_failed:
            state, why = "login", "The box refused the login"
        elif not last_ok or now - last_ok > legacy.STALE:
            state = "dark"
            why = (st.error if st and st.error else "never read") if not last_ok else \
                f"not read for {fleet_health.ago((now - last_ok).total_seconds())[:-4]}" + (f": {st.error}" if st and st.error else "")
        else:
            state, why = "live", None
        drift = legacy.clock_drift(st.data) if st and st.data else None
        alarm = legacy.alarm_info((c or {}).get("alarm"), now, drift or timedelta(0))
        recent_alarm = alarm if alarm and alarm["recent"] else None
        problem = (state in ("dark", "login", "other") or bool(c and c["tone"] == "crit")
                   or bool(recent_alarm and recent_alarm["level"] in ("crit", "warn")))
        rows.append({"box": b, "st": st, "c": c, "state": state, "why": why, "last_ok": last_ok,
                     "alarm": alarm, "recent_alarm": recent_alarm, "problem": problem, "drift_min": round(drift.total_seconds() / 60) if drift else 0,
                     "firing": firing.get(b.id) if state == "live" else None,
                     "age": fleet_health.ago((now - last_ok).total_seconds()) if last_ok else None})
    return rows


_LIVE_SORTS = {
    "name": lambda r: r["box"].name.lower(),
    "problems": lambda r: (not r["problem"], {"other": 0, "login": 1, "dark": 2, "live": 3}[r["state"]],
                           _TONE_ORDER.get((r["c"] or {}).get("tone"), 5), r["box"].name.lower()),
    "hi_stack": lambda r: -((r["c"] or {}).get("hi_stack") or -1),
    "aqua": lambda r: -((r["c"] or {}).get("aqua") or -1),
    "coil": lambda r: -((r["c"] or {}).get("coil") or -1),
}


@admin_bp.route("/live")
def live_page():
    if not is_superuser():
        abort(403)
    now = datetime.utcnow()
    rows = _legacy_rows(now)
    hidden = LegacyBox.query.filter(LegacyBox.is_active.is_(False), LegacyBox.deleted_at.is_(None)) \
        .order_by(LegacyBox.name).all()
    summary = {"total": len(rows), "live": sum(r["state"] == "live" for r in rows), "hidden": len(hidden),
               "dark": sum(r["state"] == "dark" for r in rows), "login": sum(r["state"] == "login" for r in rows),
               "calling": sum(1 for r in rows if r["c"] and r["c"]["tone"] == "heat" and r["state"] == "live"),
               "problems": sum(r["problem"] for r in rows)}
    newest = max((r for r in rows if r["recent_alarm"] and r["recent_alarm"]["level"] in ("crit", "warn")
                  and r["recent_alarm"]["utc"]), key=lambda r: r["recent_alarm"]["utc"], default=None)
    show, q = request.args.get("show", "all"), (request.args.get("q") or "").strip().lower()
    if show == "problems":
        rows = [r for r in rows if r["problem"]]
    elif show == "dark":
        rows = [r for r in rows if r["state"] in ("dark", "login", "other")]
    elif show == "calling":
        rows = [r for r in rows if r["c"] and r["c"]["tone"] == "heat" and r["state"] == "live"]
    elif show == "hidden":
        rows = _legacy_rows(now, hidden)
    else:
        show = "all"
    if q:
        rows = [r for r in rows if q in r["box"].name.lower()]
    sort = request.args.get("sort", "problems")
    rows.sort(key=_LIVE_SORTS.get(sort, _LIVE_SORTS["problems"]))
    view = "table" if request.args.get("view") == "table" else "cards"
    from app import legacy
    return render_template("live.html", title="Live", rows=rows, summary=summary, newest=newest, show=show,
                           legend=legacy.TINT_LEGEND,
                           q=request.args.get("q", ""), sort=sort if sort in _LIVE_SORTS else "problems", view=view,
                           hhmm=legacy.hhmm, poll_min=round(current_app.config.get("LEGACY_POLL_INTERVAL_SECONDS", 300) / 60))


@admin_bp.route("/live/<int:box_id>")
def live_box(box_id):
    if not is_superuser():
        abort(403)
    b = LegacyBox.query.get_or_404(box_id)
    if b.deleted_at:
        abort(404)
    row = _legacy_rows(datetime.utcnow(), [b])[0]
    from app import history, legacy
    from app.models import LegacyDaily, LegacyEvent
    daily = LegacyDaily.query.filter_by(box_id=b.id).order_by(LegacyDaily.day.desc()).limit(31).all()
    events = LegacyEvent.query.filter_by(box_id=b.id).order_by(LegacyEvent.ts.desc()).limit(30).all()
    return render_template("live_box.html", title=b.name, r=row, hhmm=legacy.hhmm, now_utc=datetime.utcnow(),
                           dark_min=current_app.config.get("LEGACY_DARK_MINUTES", 30), daily=daily, events=events,
                           local=history.local, raw_days=current_app.config.get("LEGACY_RAW_DAYS", 90))


def _live_box_or_404(box_id):
    if not is_superuser():
        abort(403)
    b = LegacyBox.query.get_or_404(box_id)
    if b.deleted_at:
        abort(404)
    return b


@admin_bp.route("/live/<int:box_id>/history.json")
def live_history(box_id):
    b = _live_box_or_404(box_id)
    from app import analytics
    days = analytics.RANGES.get(request.args.get("days", "2"), 2)
    return jsonify(analytics.box_series(b.id, days))


@admin_bp.route("/live/<int:box_id>/series.json")
def live_series(box_id):
    """The Live page's pop-up chart: chosen sensors + burner minutes per hour."""
    b = _live_box_or_404(box_id)
    from app import analytics
    days = {"1": 1, "2": 2, "7": 7, "30": 30}.get(request.args.get("days", "1"), 1)
    keys = [k for k in request.args.get("keys", "").split(",") if k]
    data = analytics.box_sensor_series(b, days, keys)
    data["name"] = b.name
    data["url"] = url_for("admin.live_box", box_id=b.id)
    return jsonify(data)


def _csv_response(name, header, rows):
    import csv
    import io
    import re

    def gen():
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            if buf.tell() > 32768:
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate()
        yield buf.getvalue()
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "export"
    return Response(stream_with_context(gen()), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{safe}.csv"'})


@admin_bp.route("/live/<int:box_id>/history.csv")
def live_history_csv(box_id):
    b = _live_box_or_404(box_id)
    from app import analytics
    res = request.args.get("res", "hourly")
    if res not in analytics.CSV_COLUMNS:
        abort(400)
    days = analytics.RANGES.get(request.args.get("days", "30"), 30)
    header, rows = analytics.box_csv_rows(b.id, res, days)
    return _csv_response(f"{b.name}_{res}_{days}d", header, rows)


def _analytics_period():
    from app import analytics
    key = request.args.get("period", "7")
    return key if key == "season" or key in analytics.PERIODS else "7"


@admin_bp.route("/analytics")
def analytics_page():
    if not is_superuser():
        abort(403)
    from app import analytics, history
    a = analytics.portfolio(_analytics_period(), poll_min=_poll_min())
    return render_template("analytics.html", title="Analytics", a=a, local=history.local, hm=analytics._hm,
                           law=history, raw_days=current_app.config.get("LEGACY_RAW_DAYS", 90))


@admin_bp.route("/analytics.csv")
def analytics_csv():
    if not is_superuser():
        abort(403)
    from app import analytics
    a = analytics.portfolio(_analytics_period(), poll_min=_poll_min())
    cols = [("building", lambda r: r["box"].name), ("answered_pct", lambda r: round(r["uptime"] * 100, 1)),
            ("heat_burner_min", "heat"), ("hot_water_burner_min", "hw"), ("degree_days", lambda r: round(r["hdd"], 1)),
            ("burner_min_per_degree_day", "mpd"), ("x_fleet_median", "mpd_x"), ("change_vs_previous", "trend"),
            ("inside_avg_f", "in_avg"), ("inside_min_f", "in_min"), ("outside_avg_f", "out_avg"),
            ("desired_avg_f", "dit"), ("below_minimum_hours", "law_hours"),
            ("below_minimum_sensor_hours", "law_sensor_hours"), ("lowest_apartment_f", "apt_low"),
            ("safety_min", "safety"), ("trips", "trips"), ("dark_times", "dark"), ("overheated_hours", "overheat_hours"),
            ("heating_60f_plus_min", "warm_heat"), ("hot_water_min_per_day", "hw_day"),
            ("stack_while_firing_f", "stack_fire"), ("water_counts", "water"), ("water_night_counts", "water_night"),
            ("no_outside_sensor", "no_out_sensor")]

    def val(r, get):
        v = get(r) if callable(get) else r[get]
        return round(v, 2) if isinstance(v, float) else ("" if v is None else v)
    rows = ([val(r, g) for _n, g in cols] for r in sorted(a["rows"], key=lambda r: r["box"].name.lower()))
    return _csv_response(f"analytics_{a['key']}_{a['end']:%Y%m%d}", [n for n, _g in cols], rows)


def _poll_min():
    return max(1, round(current_app.config.get("LEGACY_POLL_INTERVAL_SECONDS", 300) / 60))


@admin_bp.route("/live/action", methods=["POST"])
def live_action():
    """Hide, show again or delete one box or several (the table's checkboxes).
    Hidden: not polled, listed under Hidden. Deleted: gone from the portal;
    the row is kept only so a re-import does not bring it back."""
    if not is_superuser():
        abort(403)
    action = request.form.get("action")
    if action not in ("hide", "show", "delete"):
        abort(400)
    ids = [i for i in (_parse_int(v) for v in request.form.getlist("box_id")) if i]
    boxes = LegacyBox.query.filter(LegacyBox.id.in_(ids or [-1]), LegacyBox.deleted_at.is_(None)).all()
    if not boxes:
        flash("Nothing selected.", "error")
        return redirect(_local_next(url_for("admin.live_page")))
    now = datetime.utcnow()
    for b in boxes:
        if action == "hide":
            b.is_active, b.hidden_by = False, _who()
        elif action == "show":
            b.is_active, b.hidden_by = True, None
        else:
            b.is_active, b.deleted_at = False, now
    db.session.commit()
    n = len(boxes)
    words = {"hide": "hidden: no longer read, listed under Hidden",
             "show": "back on the Live page: read from the next poll",
             "delete": "deleted"}[action]
    flash(f"{n} box{'es' if n != 1 else ''} {words}.", "success")
    if action == "delete" or (action == "hide" and request.form.get("from_box")):
        return redirect(url_for("admin.live_page", show="dark"))
    return redirect(_local_next(url_for("admin.live_page")))


# ─── Adopt a blank module (EMS fleet/enroll.py ↔ app/enroll_api.py) ─────
#
# The operator types the pairing code the module shows on its own screen.
# That code is the only way to open a module's adopt page: a code that
# matched is remembered in the session, so nobody adopts a box just by
# picking it from the "recently seen" list. Adopting creates the Controller
# row; the module then hands over its own polling token by itself.

ADOPT_SEEN_DAYS = 7


def _can_adopt() -> bool:
    return is_superuser() or user_has_min_role_anywhere("operator")


def _adopt_buildings():
    q = Building.query if is_superuser() else Building.query.filter(
        Building.id.in_(creatable_controller_building_query(current_user().id)))
    return q.filter_by(is_active=True).order_by(Building.name).all()


def _waiting_modules():
    since = datetime.utcnow() - timedelta(days=ADOPT_SEEN_DAYS)
    return Enrollment.query.filter(Enrollment.state == "waiting", Enrollment.last_seen >= since) \
        .order_by(Enrollment.last_seen.desc()).all()


def _opened(eid) -> bool:
    return eid in (session.get("adopt_opened") or [])


@admin_bp.route("/adopt", methods=["GET", "POST"])
def adopt():
    if not _can_adopt():
        abort(403)
    if request.method == "POST":
        code = normalize_code(request.form.get("code"))
        row = None
        if len(code) == 6:
            row = Enrollment.query.filter(Enrollment.code_hash == enroll_code_hash(code),
                                          Enrollment.state.in_(("waiting", "assigned"))).first()
        if row is None:
            flash("No module waiting to be adopted shows that code. Check the screen: six letters and digits, "
                  "and the module must be powered and online.", "error")
            return redirect(url_for("admin.adopt"))
        opened = session.get("adopt_opened") or []
        session["adopt_opened"] = (opened + [row.id])[-20:]
        return redirect(url_for("admin.adopt_module", eid=row.id))
    return render_template("adopt.html", title="Adopt a module", waiting=_waiting_modules())


@admin_bp.route("/adopt/<eid>", methods=["GET", "POST"])
def adopt_module(eid):
    if not _can_adopt():
        abort(403)
    row = Enrollment.query.get_or_404(eid)
    if not _opened(eid) and row.state in ("waiting", "assigned"):
        flash("Type the code shown on the module's screen to open it.", "error")
        return redirect(url_for("admin.adopt"))
    if row.controller is not None:
        require_role("viewer", row.controller)

    if request.method == "POST" and request.form.get("action") == "assign":
        if row.state != "waiting":
            flash("This module is already being adopted.", "error")
            return redirect(url_for("admin.adopt_module", eid=eid))
        building = Building.query.get_or_404(_parse_int(request.form.get("building_id")) or 0)
        require_role("operator", building)
        name = (request.form.get("name") or row.hostname or "controller").strip()[:200]
        ctrl = Controller(building_id=building.id, name=name, tailscale_ip=row.lan_ip,
                          tailscale_hostname=row.hostname, fleet_api_token=None)
        db.session.add(ctrl)
        db.session.flush()
        row.controller_id = ctrl.id
        row.state = "assigned"
        row.assigned_at = datetime.utcnow()
        u = current_user()
        row.assigned_by = "superuser" if is_superuser() else (u.login if u else None)
        db.session.commit()
        flash(f"Adopted into {building.name}. Waiting for the module to confirm; it checks in every 15 seconds.", "success")
        return redirect(url_for("admin.adopt_module", eid=eid))

    if request.method == "POST" and request.form.get("action") == "cancel":
        # Wrong building, wrong site, wrong bundle: start over, as long as
        # the module has not yet acted on it (a restore it has taken is
        # past undoing from here).
        if row.state != "assigned":
            flash("Too late to cancel: the module has already acted on it.", "error")
            return redirect(url_for("admin.adopt_module", eid=eid))
        ctrl = row.controller
        if ctrl is not None:
            require_role("operator", ctrl)
        if row.mode == "new" and ctrl is not None and not ctrl.fleet_api_token and ctrl.status is None:
            db.session.delete(ctrl)                 # the empty record this adoption made
        row.controller_id = None
        row.mode, row.state = "new", "waiting"
        row.bundle_ct = row.bundle_site = row.bundle_made = None
        row.fetched_at = row.assigned_at = row.assigned_by = None
        row.module_error = None
        row.ts_key = row.ts_key_made = row.ts_error = None
        db.session.commit()
        flash("Cancelled. The module is waiting again; choose what it is.", "success")
        return redirect(url_for("admin.adopt_module", eid=eid))

    if request.method == "POST" and request.form.get("action") == "dismiss":
        if row.state == "waiting":
            row.state = "rejected"
            row.ts_key = None
            db.session.commit()
            flash("Dismissed. The module stops showing up here; it keeps its code until it is reflashed.", "success")
        return redirect(url_for("admin.adopt"))

    # Once the module has confirmed, take its first status straight away so
    # the last step of the page can turn green without waiting for the poller.
    if row.state == "complete" and row.controller and row.controller.status is None:
        try:
            poll_controller(current_app._get_current_object(), row.controller)
        except Exception:
            db.session.rollback()
    return render_template("adopt_module.html", title="Adopt a module", m=row,
                           buildings=_adopt_buildings() if row.state == "waiting" else [],
                           restore_targets=_restore_targets() if row.state == "waiting" else [],
                           card=_card(row.controller) if row.controller else None)


def _restore_targets():
    """Sites this operator may restore onto a new module: existing controllers
    in buildings they can operate, worst first, so the dead one is on top."""
    q = Controller.query.filter_by(is_active=True)
    if not is_superuser():
        q = q.filter(Controller.building_id.in_(creatable_controller_building_query(current_user().id)))
    cards = [_card(c) for c in q.all()]
    cards.sort(key=lambda k: (_LEVEL_ORDER.get(k["eval"]["level"], 9), k["ctrl"].name))
    return cards


MAX_BUNDLE_BYTES = 2 * 1024 * 1024
AGE_INTRO = b"age-encryption.org/v1\n"


@admin_bp.route("/adopt/<eid>/restore", methods=["POST"])
def adopt_restore(eid):
    """The operator's browser posts the site bundle it re-encrypted to this
    module's own key (app/static/js/agecrypt.js). The portal never sees the
    master key or the plaintext: it checks the shape of what it got, keeps
    it for the module, and points the module at the existing Controller."""
    if not _can_adopt():
        abort(403)
    row = Enrollment.query.get_or_404(eid)
    if not _opened(eid):
        return {"error": "type the module's code first"}, 403
    if row.state != "waiting":
        return {"error": "this module is already being adopted"}, 409
    if not row.age_recipient:
        return {"error": "this module sent no public key, so nothing can be encrypted to it"}, 409
    ctrl = Controller.query.get_or_404(_parse_int(request.form.get("controller_id")) or 0)
    require_role("operator", ctrl)
    f = request.files.get("bundle")
    data = f.read(MAX_BUNDLE_BYTES + 1) if f else b""
    if not data.startswith(AGE_INTRO) or len(data) > MAX_BUNDLE_BYTES:
        return {"error": "that is not an encrypted bundle (or it is over 2 MB)"}, 400
    header = data[:data.find(b"\n---")] if b"\n---" in data else b""
    if header.count(b"\n-> ") != 1 or b"\n-> X25519 " not in header:
        return {"error": "the bundle must be encrypted to exactly one key: this module's"}, 400

    # One module per site: an older adoption pointing at this controller lets go.
    for old in Enrollment.query.filter(Enrollment.controller_id == ctrl.id, Enrollment.id != row.id).all():
        old.controller_id = None
    row.mode = "restore"
    row.controller_id = ctrl.id
    row.bundle_ct = data
    row.bundle_site = (request.form.get("site") or "")[:100] or None
    row.bundle_made = (request.form.get("made") or "")[:40] or None
    row.state = "assigned"
    row.assigned_at = datetime.utcnow()
    u = current_user()
    row.assigned_by = "superuser" if is_superuser() else (u.login if u else None)
    db.session.commit()
    flash(f"Restoring {ctrl.name!r} onto this module. It collects the bundle on its next check-in (about 15 s), "
          "reboots into the site and comes back as it.", "success")
    return {"ok": True, "redirect": url_for("admin.adopt_module", eid=eid)}


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
