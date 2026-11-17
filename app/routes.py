from datetime import datetime, timedelta, timezone

from flask import (Blueprint, Response, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   session, stream_with_context, url_for)
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

from app.billing import INVOICE_DUE_DAYS, _finalize_payment, current_contract, generate_invoices, period_for_date
from app.extensions import csrf, db
from app import fleet_health
from app.enroll_api import code_hash as enroll_code_hash, normalize_code
from app.fleet_poller import poll_controller
from app.models import (AlertEvent, AlertState, BloomAsset, Building, Client, Contract, Controller, Enrollment,
                        Invoice, LegacyAlertState, LegacyBox, LegacyStatus, Permission, Property, Rollout,
                        RolloutTarget, User)
from app.permissions import (
    ROLE_RANK,
    can_view_building,
    creatable_building_property_query,
    creatable_controller_building_query,
    creatable_property_client_query,
    current_user,
    effective_role,
    is_superuser,
    is_view_only,
    require_role,
    superuser_required,
    user_has_min_role_anywhere,
    visible_building_ids,
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
    # Portfolio (the whole fleet by owner -> property) is the landing page for
    # the administrator; everyone else lands on the properties shared with them.
    if is_superuser():
        return redirect(url_for("admin.portfolio_page"))
    return redirect(url_for("admin.home_page"))


# ─── My properties: what a signed-in person may watch (app/home.py) ──────

@admin_bp.app_context_processor
def _menu_flags():
    """view_only: a signed-in person whose every grant is 'viewer' gets the
    short menu (My properties, Fans, Alerts)."""
    try:
        return {"view_only": is_view_only()}
    except Exception:
        return {"view_only": False}


@admin_bp.route("/home")
def home_page():
    """The properties shared with this account: every building's heating
    status, whatever reads it, and the property's roof fans. Read-only. The
    administrator sees every filed building here (Portfolio is the fuller view)."""
    import time as _time
    from sqlalchemy.orm import joinedload
    from app import home, portfolio
    now_dt, now = datetime.utcnow(), _time.time()
    q = Building.query.options(joinedload(Building.property).joinedload(Property.client)) \
        .filter(Building.is_active.is_(True))
    ids = visible_building_ids()
    if ids is not None:
        q = q.filter(Building.id.in_(ids or [-1]))
    buildings, rc_dhw = [], portfolio.rc_dhw_by_building()
    for b in q.all():
        p = b.property
        if p is None or not p.is_active or (p.client is not None and not p.client.is_active):
            continue
        buildings.append({"client": p.client.name if p.client else None, "client_id": p.client_id,
                          "prop": p.name, "prop_id": p.id,
                          "entry": portfolio.building_entry(b, now, now_dt, rc_dhw)})
    data = home.assemble(buildings, _fan_rows(now_dt))
    return render_template("home.html", title="My properties", **data)


@admin_bp.route("/account", methods=["GET", "POST"])
def account_page():
    """A signed-in person's own account: what has been shared with them, and
    changing their own password (the administrator sets the first one)."""
    user = current_user()
    if request.method == "POST":
        if user is None:
            abort(403)                               # the administrator's password lives in the server's settings
        cur, new = request.form.get("current", ""), request.form.get("new", "")
        if user.password_hash.startswith("!"):
            flash("This account signs in by company email and has no password.", "error")
        elif not check_password_hash(user.password_hash, cur):
            flash("The current password is not right. Nothing was changed.", "error")
        elif len(new) < 10:
            flash("Choose a new password of at least 10 characters.", "error")
        elif new != request.form.get("again", ""):
            flash("The two new passwords do not match.", "error")
        elif new == cur:
            flash("The new password is the same as the current one.", "error")
        else:
            user.password_hash = generate_password_hash(new)
            db.session.commit()
            flash("Password changed.", "success")
        return redirect(url_for("admin.account_page"))
    grants = []
    if user is not None:
        grants = [{"label": _scope_label(p.scope_type, p.scope_id), "role": p.role}
                  for p in Permission.query.filter_by(user_id=user.id)
                  .order_by(Permission.scope_type, Permission.scope_id).all()]
    return render_template("account.html", title="My account", user=user, grants=grants,
                           has_password=bool(user) and not user.password_hash.startswith("!"))


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
    card = _card(ctrl)
    from app.legacy import tint as _tint              # outdoor weather band, same as the Live wall
    _h = (card.get("eval") or {}).get("heating") or {}
    otint = _tint({"out": _h.get("outdoor")}, None, None, None, []).get("out")
    from app import heat_overnight
    heat = heat_overnight.for_echo([ctrl.id]).get(ctrl.id)
    return render_template("fleet_controller.html", title=ctrl.name, card=card, status=ctrl.status, otint=otint,
                           heat=heat, raw=(ctrl.status.raw_json if ctrl.status else None) or {}, now_utc=datetime.utcnow())


# ─── Alerts (app/alerts.py decides, app/notify.py sends) ────────────────

MUTE_CHOICES = {"4": 4, "24": 24, "72": 72}


def _visible_controller_ids():
    if is_superuser():
        return None                                   # all of them
    return [c.id for c in visible_controller_query(current_user().id).all()]


def _visible_box_ids():
    """None = every 2010 box (the administrator); otherwise the ids of the
    boxes filed under a building this person may see."""
    ids = visible_building_ids()
    if ids is None:
        return None
    if not ids:
        return []
    return [i for (i,) in db.session.query(LegacyBox.id).filter(
        LegacyBox.building_id.in_(ids), LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None)).all()]


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
        box_ids = _visible_box_ids()
        lq = _legacy_alert_states().filter(LegacyAlertState.alerting.is_(True))
        if box_ids is not None:
            lq = lq.filter(LegacyAlertState.box_id.in_(box_ids or [-1]))
        n += lq.count()
        return {"open_alerts": n}
    except Exception:
        return {"open_alerts": 0}


@admin_bp.app_context_processor
def _heat_badge_flag():
    """Whether the overnight heat-ran verdict shows on the Live wall, nav and
    detail pages. Off while the signal is being calibrated; the /heat report is
    still reachable by URL. Flip HEAT_BADGE on to restore the surfaces."""
    return {"heat_badge": current_app.config.get("HEAT_BADGE", True)}


def _hot_water_now(box_ids, now) -> dict:
    """box id -> its current target / supply, for the hot-water boxes among
    box_ids read in the last legacy.STALE (Alerts shows them beside the alarm).
    One timed-out poll keeps the last good reading, as Live does."""
    from app import legacy
    out = {}
    if not box_ids:
        return out
    for st in LegacyStatus.query.filter(LegacyStatus.box_id.in_(set(box_ids))).all():
        if not (st.data and st.last_ok_at and now - st.last_ok_at <= legacy.STALE):
            continue
        if legacy.heat_system(st.data) != "hot water":
            continue
        c = legacy.card(st.data)
        out[st.box_id] = {"target": c.get("target"), "supp": c.get("supp"), "retn": c.get("retn"),
                          "cold": c.get("loop_cold")}
    return out


def _legacy_alert_states():
    return LegacyAlertState.query.join(LegacyBox, LegacyBox.id == LegacyAlertState.box_id) \
        .filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None))


@admin_bp.route("/alerts")
def alerts_page():
    from app import history, notify
    ids = _visible_controller_ids()
    box_ids = _visible_box_ids()                       # None = all; a list = only those 2010 boxes
    states = AlertState.query.join(Controller).filter(Controller.is_active.is_(True))
    events = AlertEvent.query
    if ids is not None:
        states = states.filter(AlertState.controller_id.in_(ids or [-1]))
        events = events.filter(or_(AlertEvent.controller_id.in_(ids or [-1]),
                                   AlertEvent.legacy_box_id.in_(box_ids or [-1])))
    states = states.all()
    now = datetime.utcnow()
    open_ = sorted([s for s in states if s.alerting], key=lambda s: s.opened_at or now)
    waiting = [s for s in states if not s.alerting and s.first_bad_at]
    muted = [s for s in states if s.muted_until and s.muted_until > now]
    lq = _legacy_alert_states()
    if box_ids is not None:
        lq = lq.filter(LegacyAlertState.box_id.in_(box_ids or [-1]))
    lstates = lq.all()
    show_legacy = box_ids is None or bool(box_ids)
    l_open = sorted([s for s in lstates if s.alerting], key=lambda s: s.opened_at or now)
    l_waiting = [s for s in lstates if not s.alerting and s.first_bad_at]
    l_muted = [s for s in lstates if s.muted_until and s.muted_until > now]
    events = events.order_by(AlertEvent.created_at.desc()).limit(100).all()
    # Device alarms the 2010 boxes reported themselves (what the Live wall's old
    # Alarm column showed), now combined into the Alerts tab. From the event log
    # (app/history.changes), newest first; these may or may not have paged.
    box_alarms = []
    if show_legacy:
        from app.models import LegacyBox, LegacyEvent
        rq = LegacyEvent.query.filter(LegacyEvent.kind == "alarm")
        if box_ids is not None:
            rq = rq.filter(LegacyEvent.box_id.in_(box_ids))
        rows = rq.order_by(LegacyEvent.ts.desc()).limit(40).all()
        bn = {b.id: b.name for b in LegacyBox.query
              .filter(LegacyBox.id.in_([e.box_id for e in rows] or [-1])).all()}
        box_alarms = [{"name": bn.get(e.box_id, f"box {e.box_id}"), "box_id": e.box_id,
                       "when": history.local(e.ts).strftime("%b %d %I:%M %p").replace(" 0", " "),
                       "text": e.text or e.code or "alarm"} for e in rows if e.box_id in bn]
    hw_now = _hot_water_now([s.box_id for s in lstates if s.alerting or s.first_bad_at or (s.muted_until and s.muted_until > now)]
                            + [e.legacy_box_id for e in events if e.legacy_box_id], now) if show_legacy else {}
    cfg = current_app.config
    ntfy_qr = None
    if is_superuser() and cfg.get("ALERT_NTFY_TOPIC"):
        import segno
        from markupsafe import Markup
        url = f"{cfg.get('ALERT_NTFY_SERVER', 'https://ntfy.sh').rstrip('/')}/{cfg['ALERT_NTFY_TOPIC']}"
        ntfy_qr = Markup(segno.make(url, error="m").svg_inline(scale=4, border=2))
    return render_template("alerts.html", title="Alerts", open_=open_, waiting=waiting, muted=muted, ntfy_qr=ntfy_qr,
                           l_open=l_open, l_waiting=l_waiting, l_muted=l_muted, show_legacy=show_legacy,
                           legacy_dark_min=cfg.get("LEGACY_DARK_MINUTES", 30),
                           events=events, hw_now=hw_now, box_alarms=box_alarms,
                           channels=notify.channels(), now=now,
                           after_min=round(cfg.get("ALERT_AFTER_SECONDS", 600) / 60),
                           repeat_h=cfg.get("ALERT_REPEAT_HOURS", 0),
                           recovered_min=round(cfg.get("ALERT_RECOVERED_MIN_SECONDS", 1800) / 60),
                           flap_opens=cfg.get("ALERT_FLAP_OPENS", 3), flap_hours=cfg.get("ALERT_FLAP_HOURS", 6),
                           digest_hour=cfg.get("ALERT_DIGEST_HOUR", 8),
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


_TINT_NAMES = {"inside": "Inside average", "supp": "Supply", "target": "Target", "retn": "Return", "aqua": "Aquastat", "stack": "Stack",
               "coil": "Coil", "mix": "Hot water (mix)"}
_TINT_KEYS = {"inside": "inside", "supp": "supp", "target": "dsup", "retn": "retn", "aqua": "aqua", "stack": "stack", "coil": "coil",
              "mix": "mix"}


def _issue_context(now, box_ids):
    """For the Live page's issue panel, in two queries for all boxes: the last
    day's events (newest first, a few per box) and each box's alert state."""
    from app import history
    from app.models import LegacyEvent
    events, states = {}, {}
    if not box_ids:
        return events, states
    for e in LegacyEvent.query.filter(LegacyEvent.box_id.in_(box_ids), LegacyEvent.ts >= now - timedelta(hours=24)) \
            .order_by(LegacyEvent.ts.desc()).all():
        lst = events.setdefault(e.box_id, [])
        if len(lst) < 8:
            lst.append({"kind": e.kind, "when": history.local(e.ts).strftime("%I:%M %p").lstrip("0"),
                        "text": e.text or e.code or e.kind})
    for s in LegacyAlertState.query.filter(LegacyAlertState.box_id.in_(box_ids)).all():
        states[s.box_id] = s
    return events, states


def _issues(row, now, events, alert_state):
    """Everything worth knowing about one box, as the card badge's panel
    shows it: problems (what makes the badge red), coloured readings with
    their reasons, and the last day's changes."""
    from app import history
    c, problems, notes = row["c"] or {}, [], []
    hot_water = c.get("system") == "hot water"
    IGN = " (not alerted on hot-water buildings: no fuel-valve feedback)"
    if row["state"] != "live":
        problems.append(row["why"] or "Not reading")
    elif c.get("tone") == "crit" and c.get("code") == "Ign Fail" and hot_water:
        notes.append({"text": f"Status now: {c['status']}" + IGN, "tone": "info"})
    elif c.get("tone") == "crit":
        problems.append(f"Status now: {c['status']}")
    ra = row["recent_alarm"]
    if ra and ra["level"] in ("crit", "warn") and ra.get("name") == "ignitionFail" and hot_water:
        notes.append({"text": f"Box alarm: {ra['words']}, {ra['when']}" + IGN, "tone": "info"})
    elif ra and ra["level"] in ("crit", "warn"):
        problems.append(f"Box alarm: {ra['words']}, {ra['when']}" + (" (still active)" if ra["active"] else ""))
    a = alert_state
    if a and a.issue and a.issue.startswith(("No heat", "Hot water cold")) and (a.alerting or a.first_bad_at):
        problems.append(a.issue)
    elif row["state"] == "live" and c.get("loop_cold"):
        problems.append(c["loop_cold"])               # supply / return under 80 F while calling, right now
    if a and a.alerting:
        problems.append(f"Phone alert open since {history.local(a.opened_at).strftime('%b %d %I:%M %p').replace(' 0', ' ')}"
                        if a.opened_at else "Phone alert open")
    if a and a.muted_until and a.muted_until > now:
        notes.append({"text": f"Alerts muted until {history.local(a.muted_until).strftime('%b %d %I:%M %p').replace(' 0', ' ')}",
                      "tone": "info"})
    if row["state"] == "live":
        for k, (tone, why) in (c.get("tint") or {}).items():
            if k == "out":
                continue                                  # outdoor band is weather context, not a per-box note
            # the reasons name their reading already, except the inside average's
            notes.append({"text": f"Inside average: {why}" if k == "inside" else why, "tone": tone, "k": _TINT_KEYS.get(k)})
        if ra and ra["level"] == "info":
            notes.append({"text": f"Box note: {ra['words']}, {ra['when']}", "tone": "info"})
        if c.get("no_sensors"):
            from app import legacy
            notes.append({"text": legacy.NO_SENSORS_NOTE, "tone": "info"})
    keys = ["inside", "out"] if row["state"] != "live" else (
        ["dsup", "supp", "retn"] if c.get("loop_cold") else ["stack", "aqua", "inside"] if problems else ["inside", "out"])
    return {"name": row["box"].name + (f" · {c['system_label']}" if c.get("system_label") else ""),
            "problems": problems, "notes": notes, "events": events,
            "keys": ",".join(keys), "url": f"/live/{row['box'].id}"}


def _legacy_rows(now, boxes=None):
    from app import heat_overnight, legacy
    rows = []
    if boxes is None:
        boxes = LegacyBox.query.filter(LegacyBox.is_active.is_(True), LegacyBox.deleted_at.is_(None)) \
            .order_by(LegacyBox.name).all()
    firing = _firing(now, [b.id for b in boxes])
    heat = heat_overnight.for_legacy([b.id for b in boxes], now)
    events, astates = _issue_context(now, [b.id for b in boxes])
    for b in boxes:
        st = b.status
        c = legacy.card(st.data, guard_out=True) if st and st.data else None   # live display: drop a railed OAT to "—"
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
        ign_ok = bool(c and c.get("system") == "hot water")       # hot water: ignition is not a problem
        down = state in ("dark", "login", "other")
        ra_ok = recent_alarm and not (ign_ok and recent_alarm.get("name") == "ignitionFail")
        # critical = must act now; a warn-level alarm or a softer issue is a warning.
        critical = (down
                    or bool(c and c["tone"] == "crit" and not (ign_ok and c["code"] == "Ign Fail"))
                    or bool(c and c.get("loop_cold"))
                    or bool(ra_ok and recent_alarm["level"] == "crit"))
        problem = critical or bool(ra_ok and recent_alarm["level"] == "warn")
        rows.append({"box": b, "st": st, "c": c, "state": state, "why": why, "last_ok": last_ok,
                     "alarm": alarm, "recent_alarm": recent_alarm, "problem": problem, "critical": critical, "drift_min": round(drift.total_seconds() / 60) if drift else 0,
                     "firing": firing.get(b.id) if state == "live" else None,
                     "heat": heat.get(b.id),
                     "age": fleet_health.ago((now - last_ok).total_seconds()) if last_ok else None})
        rows[-1]["issues"] = _issues(rows[-1], now, events.get(b.id, []), astates.get(b.id))
        rows[-1]["problem"] = rows[-1]["problem"] or bool(rows[-1]["issues"]["problems"])
    return rows


def _row_sev(r):
    """One severity tier per row for the colour stripe and the filters:
    dark (offline) > crit (must act) > warn (needs a look) > heat (heating) > ok."""
    if r["state"] != "live":
        return "dark"
    if r.get("critical"):
        return "crit"
    tone = (r["c"] or {}).get("tone")
    if r.get("problem") or tone == "warn":   # an open warning shows amber even while heating
        return "warn"
    if tone == "heat":
        return "heat"
    return "ok"


_LIVE_SORTS = {
    "name": lambda r: r["box"].name.lower(),
    "problems": lambda r: (not r["critical"], not r["problem"],
                           {"other": 0, "login": 1, "dark": 2, "live": 3}[r["state"]],
                           _TONE_ORDER.get((r["c"] or {}).get("tone"), 5), r["box"].name.lower()),
    "hi_stack": lambda r: -((r["c"] or {}).get("hi_stack") or -1),
    "aqua": lambda r: -((r["c"] or {}).get("aqua") or -1),
    "coil": lambda r: -((r["c"] or {}).get("coil") or -1),
}


def _tag_legacy_rows(rows):
    """The 2010 boxes already carry a LegacyBox in row['box']; add the common
    source/detail_url the merged Live wall and template now expect."""
    for r in rows:
        r["source"] = "2010"
        r["detail_url"] = url_for("admin.live_box", box_id=r["box"].id)
    return rows


def _other_row(name, source, box_id, c, state, last_ok, now_dt, detail_url, problems, why=None, firing=None, keys="", heat=None):
    """A Live row for a non-oBIX building (Bloom or EchoTherm), shaped like a
    legacy row so live.html renders it unchanged: box is a namespace (so
    row.box.name works in Python sorts and Jinja), oBIX-only card fields are
    None (the template shows a dash), and alarms become the issue panel's
    problems. These rows have no hide/delete."""
    import types
    age = fleet_health.ago((now_dt - last_ok).total_seconds()) if last_ok else None
    return {"box": types.SimpleNamespace(name=name, id=box_id, hidden_by=None),
            "source": source, "detail_url": detail_url, "c": c, "state": state, "why": why, "heat": heat,
            "last_ok": last_ok, "age": age, "alarm": None, "recent_alarm": None, "firing": firing,
            "problem": bool(problems) or state != "live",
            "critical": (state != "live") or bool(c and c.get("tone") == "crit"),
            "issues": {"name": name, "problems": list(problems or []), "notes": [], "events": [],
                       "keys": keys, "url": detail_url}}


def _bloom_live_rows(now_dt):
    from app import bloom
    stale = timedelta(minutes=current_app.config.get("BLOOM_POLL_INTERVAL_SECONDS", 60) // 60 + 10)
    out = []
    # DHW (Reliable Controls scrape, app/rc_dhw.py) by building, so a Bloom row
    # on a building with a DHW tower can show its hot-water supply. Prefetched
    # once to avoid an N+1 across the wall.
    from app.models import RcDhwTower
    dhw_by_building = {}
    for tw in RcDhwTower.query.filter(RcDhwTower.is_active.is_(True)).all():
        if tw.building_id and tw.status and tw.status.ok:
            dhw_by_building[tw.building_id] = tw.status
    assets = (BloomAsset.query.filter(BloomAsset.is_active.is_(True), BloomAsset.deleted_at.is_(None))
              .order_by(BloomAsset.name).all())
    from app import heat_overnight
    heat = heat_overnight.for_bloom([a.id for a in assets], now_dt)
    for a in assets:
        st = a.status
        card = bloom.card(st.data, a.view, a.plant_tag) if st and st.ok and st.data else None
        last_ok = st.last_ok_at if st else None
        url = url_for("admin.skyline_building", asset_id=a.id)
        if not card or not last_ok or now_dt - last_ok > stale:
            out.append(_other_row(a.name, "bloom", f"b{a.id}", None, "dark", last_ok, now_dt, url, [],
                                  why="not reading", heat=heat.get(a.id)))
            continue
        # Mapped to the oBIX columns (user QA 2026-10-30): DHW supply -> Mix,
        # boiler firing -> Fire. No Bloom equivalent for Coil/Aqua/Stack/Hi
        # stk/Risr/Water/HW RT on these hot-water buildings -> left blank.
        c = {"code": None, "status": card["status"], "tone": card["tone"], "no_sensors": False,
             "out": card["outside_f"], "inside": card["space_avg_f"], "dit": card["setpoint_f"],
             "heat_rt": card["heat_rt"], "hw_rt": None, "total_rt": card["heat_rt"],
             "aqua": None, "coil": None, "mix": card["dhw_supply_f"], "stack": None, "hi_stack": None, "water": None,
             "supp": card["supply_f"], "retn": card["return_f"], "risr": None,
             "target": card["supply_sp_f"], "oil_pct": None,
             "dhw_supply": None, "dhw_retn": None, "dhw_pump": None, "dhw_alarm": None,
             "system": "hot water", "system_label": "Bloom · Skyline", "fuel": None, "n_boilers": 0, "tint": {}}
        ds = dhw_by_building.get(a.building_id)
        if ds:
            c["dhw_supply"], c["dhw_retn"] = ds.supply_f, ds.return_f
            c["dhw_pump"], c["dhw_alarm"] = ds.pump_running, ds.alarm
        # Same Anagha cell colours as the 2010 boxes: supply/return loop temps,
        # the average-apartment comfort cell, and hot water. The DHW column shows
        # the Reliable Controls scrape where there's a tower, the Bloom reading
        # otherwise, so colour whichever one it will show.
        from app.legacy import tint as _tint, dhw_tint as _dhw_tint
        c["tint"] = _tint(c, c["inside"], c["dit"], None, [])
        if c["dhw_supply"] is not None:
            td = _dhw_tint(c["dhw_supply"])
            if td:
                c["tint"]["dhw_supply"] = td
        out.append(_other_row(a.name, "bloom", f"b{a.id}", c, "live", last_ok, now_dt, url,
                              card.get("alarms") or [], firing=card.get("firing"), keys="inside", heat=heat.get(a.id)))
    return out


def _echo_live_rows(now_dt):
    from app import heat_overnight
    out = []
    ctrls = Controller.query.filter(Controller.is_active.is_(True)).order_by(Controller.name).all()
    heat = heat_overnight.for_echo([ctrl.id for ctrl in ctrls], now_dt)
    for ctrl in ctrls:
        ev = fleet_health.evaluate(ctrl, getattr(ctrl, "status", None), None, now_dt)
        heating, lvl, online = (ev.get("heating") or {}), ev.get("level"), ev.get("online")
        problems = [txt for (l, txt) in ev.get("issues", []) if l in ("crit", "warn")]
        last_ok = getattr(getattr(ctrl, "status", None), "last_ok_at", None)
        url = url_for("admin.fleet_controller", controller_id=ctrl.id)
        name = ctrl.name + (f" · {ctrl.building.name}" if ctrl.building else "")
        if not online:
            out.append(_other_row(name, "echotherm", f"e{ctrl.id}", None, "dark", last_ok, now_dt, url,
                                  problems, why=ev.get("errorPlain") or "offline", heat=heat.get(ctrl.id)))
            continue
        tone = "crit" if lvl == "crit" else "warn" if lvl == "warn" else ("heat" if heating.get("demand") else "ok")
        c = {"code": None, "status": heating.get("reason") or heating.get("mode") or "Online", "tone": tone,
             "no_sensors": bool(heating.get("noSensors")),
             "out": heating.get("outdoor"),
             "inside": round(heating["avgApt"], 1) if heating.get("avgApt") is not None else None,
             "dit": heating.get("target"),
             "heat_rt": None, "hw_rt": None, "total_rt": None,
             "aqua": None, "coil": None, "mix": None, "stack": None, "hi_stack": None, "water": None,
             "supp": heating.get("supply"), "retn": heating.get("ret"), "risr": None,
             "target": heating.get("supplyTarget"), "oil_pct": None,
             "system": heating.get("system") or "hot water", "system_label": "EchoTherm", "fuel": None,
             "n_boilers": 0, "tint": {}}
        from app.legacy import tint as _tint
        c["tint"] = _tint(c, c["inside"], c["dit"], None, [])
        out.append(_other_row(name, "echotherm", f"e{ctrl.id}", c, "live", last_ok, now_dt, url, problems,
                              heat=heat.get(ctrl.id)))
    return out


@admin_bp.route("/live")
def live_page():
    if not is_superuser():
        abort(403)
    now = datetime.utcnow()
    rows = _tag_legacy_rows(_legacy_rows(now)) + _bloom_live_rows(now) + _echo_live_rows(now)
    hidden = LegacyBox.query.filter(LegacyBox.is_active.is_(False), LegacyBox.deleted_at.is_(None)) \
        .order_by(LegacyBox.name).all()
    summary = {"total": len(rows), "live": sum(r["state"] == "live" for r in rows), "hidden": len(hidden),
               "dark": sum(r["state"] == "dark" for r in rows), "login": sum(r["state"] == "login" for r in rows),
               "calling": sum(1 for r in rows if r["c"] and r["c"]["tone"] == "heat" and r["state"] == "live"),
               "problems": sum(1 for r in rows if r["critical"]),
               "warnings": sum(1 for r in rows if r["problem"] and not r["critical"]),
               "noheat": sum(1 for r in rows if (r.get("heat") or {}).get("state") == "no-heat"),
               "steam": sum(1 for r in rows if (r["c"] or {}).get("system") == "steam"),
               "hotwater": sum(1 for r in rows if (r["c"] or {}).get("system") == "hot water"),
               "s2010": sum(1 for r in rows if r["source"] == "2010"),
               "bloom": sum(1 for r in rows if r["source"] == "bloom"),
               "echo": sum(1 for r in rows if r["source"] == "echotherm")}
    newest = max((r for r in rows if r["recent_alarm"] and r["recent_alarm"]["level"] in ("crit", "warn")
                  and r["recent_alarm"]["utc"]), key=lambda r: r["recent_alarm"]["utc"], default=None)
    show = request.args.get("show", "all")
    source = request.args.get("source", "all")
    q = (request.args.get("q") or "").strip().lower()
    if source in ("2010", "bloom", "echotherm"):
        rows = [r for r in rows if r["source"] == source]
    else:
        source = "all"
    # Show-row counts reflect the current source selection; the source-row
    # counts (total/s2010/bloom/echo) stay fleet-wide.
    summary["shown_total"] = len(rows)
    for k, f in (("problems", lambda r: r["critical"]),
                 ("warnings", lambda r: r["problem"] and not r["critical"]),
                 ("noheat", lambda r: (r.get("heat") or {}).get("state") == "no-heat"),
                 ("calling", lambda r: r["c"] and r["c"]["tone"] == "heat" and r["state"] == "live"),
                 ("dark", lambda r: r["state"] == "dark"), ("login", lambda r: r["state"] == "login"),
                 ("steam", lambda r: (r["c"] or {}).get("system") == "steam"),
                 ("hotwater", lambda r: (r["c"] or {}).get("system") == "hot water")):
        summary[k] = sum(1 for r in rows if f(r))
    if show == "problems":
        rows = [r for r in rows if r["critical"]]
    elif show == "warnings":
        rows = [r for r in rows if r["problem"] and not r["critical"]]
    elif show == "noheat":
        rows = [r for r in rows if (r.get("heat") or {}).get("state") == "no-heat"]
    elif show == "dark":
        rows = [r for r in rows if r["state"] in ("dark", "login", "other")]
    elif show == "calling":
        rows = [r for r in rows if r["c"] and r["c"]["tone"] == "heat" and r["state"] == "live"]
    elif show == "hidden":
        rows = _tag_legacy_rows(_legacy_rows(now, hidden))
    elif show in ("steam", "hotwater"):
        want = "steam" if show == "steam" else "hot water"
        rows = [r for r in rows if (r["c"] or {}).get("system") == want]
    else:
        show = "all"
    if q:
        rows = [r for r in rows if q in r["box"].name.lower()]
    for r in rows:
        r["sev"] = _row_sev(r)
    sort = request.args.get("sort", "problems")
    rows.sort(key=_LIVE_SORTS.get(sort, _LIVE_SORTS["problems"]))
    view = "table" if request.args.get("view") == "table" else "cards"
    from app import heat_overnight, legacy
    # The window the Heat o/n column is reporting ("last night" through the day,
    # frozen until 9pm; "tonight so far" once the new window opens) — shown in the
    # header so a frozen overnight verdict isn't mistaken for the live moment.
    _hs, _he, heat_window_label = heat_overnight.window(now)
    return render_template("live.html", title="Live", rows=rows, summary=summary, newest=newest, show=show,
                           source=source, legend=legacy.TINT_LEGEND, heat_window_label=heat_window_label,
                           q=request.args.get("q", ""), sort=sort if sort in _LIVE_SORTS else "problems", view=view,
                           hhmm=legacy.hhmm, poll_min=round(current_app.config.get("LEGACY_POLL_INTERVAL_SECONDS", 300) / 60))


@admin_bp.route("/sensors")
def sensor_health_page():
    """Bad sensors across the 2010 boxes — stuck high, frozen, dropped out or
    noisy, read from the stored history (app/sensor_health.py)."""
    if not is_superuser():
        abort(403)
    from app import sensor_health
    findings = sensor_health.scan()
    kinds = {}
    for f in findings:
        kinds[f["kind"]] = kinds.get(f["kind"], 0) + 1
    return render_template("sensor_health.html", title="Sensor health", findings=findings,
                           buildings=sensor_health.by_building(findings),
                           kinds=kinds, window_days=sensor_health.WINDOW_DAYS,
                           flat_hours=sensor_health.FLAT_HOURS)


@admin_bp.route("/heat")
def heat_overnight_page():
    """Did each building run for heat overnight? A dispatch list of the ones
    that needed heat and never fired, read back from history so the morning's
    warmer outdoor can't hide what failed in the cold (app/heat_overnight.py)."""
    if not is_superuser():
        abort(403)
    from app import heat_overnight
    now = datetime.utcnow()
    rows = _tag_legacy_rows(_legacy_rows(now)) + _bloom_live_rows(now) + _echo_live_rows(now)
    _start, _end, label = heat_overnight.window(now)
    tally = {"ran": 0, "no-heat": 0, "idle": 0, "unknown": 0}
    noheat = []
    for r in rows:
        state = (r.get("heat") or {}).get("state")
        if state in tally:
            tally[state] += 1
        if state == "no-heat":
            noheat.append(r)
    # Coldest-first: the building that failed in the hardest cold needs the call most.
    noheat.sort(key=lambda r: ((r["heat"].get("min_out") if r["heat"].get("min_out") is not None else 999),
                               r["box"].name.lower()))
    source_name = {"2010": "2010 box", "bloom": "Bloom", "echotherm": "EchoTherm"}
    return render_template("heat_overnight.html", title="Overnight heat", rows=noheat, tally=tally,
                           window_label=label, total=len(rows), source_name=source_name)


@admin_bp.route("/live/<int:box_id>")
def live_box(box_id):
    b = _live_box_or_404(box_id)
    row = _legacy_rows(datetime.utcnow(), [b])[0]
    from app import history, legacy, sensor_health
    from app.models import LegacyDaily, LegacyEvent
    daily = LegacyDaily.query.filter_by(box_id=b.id).order_by(LegacyDaily.day.desc()).limit(31).all()
    events = LegacyEvent.query.filter_by(box_id=b.id).order_by(LegacyEvent.ts.desc()).limit(30).all()
    bad_sensors = sensor_health.for_box(b.id, b.name)
    return render_template("live_box.html", title=b.name, r=row, hhmm=legacy.hhmm, now_utc=datetime.utcnow(),
                           dark_min=current_app.config.get("LEGACY_DARK_MINUTES", 30), daily=daily, events=events,
                           local=history.local, raw_days=current_app.config.get("LEGACY_RAW_DAYS", 90),
                           bad_sensors=bad_sensors, sensor_window_days=sensor_health.WINDOW_DAYS)


def _live_box_or_404(box_id):
    """A 2010 box this request may READ: the administrator, or anyone with a
    grant on the building it is filed under. A hidden box, a deleted one and
    one not filed under a building are the administrator's alone. Used by
    the box's page and every data feed behind it; nothing that changes a
    box goes through here."""
    b = LegacyBox.query.get_or_404(box_id)
    if not is_superuser() and not (b.is_active and not b.deleted_at and can_view_building(b.building)):
        abort(403)
    if b.deleted_at:
        abort(404)
    return b


def _bloom_asset_or_403(asset_id):
    """A Bloom building this request may READ, by the same rule as a 2010 box."""
    a = BloomAsset.query.get_or_404(asset_id)
    if not is_superuser() and not (a.is_active and not a.deleted_at and can_view_building(a.building)):
        abort(403)
    return a


@admin_bp.route("/live/<int:box_id>/history.json")
def live_history(box_id):
    b = _live_box_or_404(box_id)
    from app import analytics
    days = analytics.RANGES.get(request.args.get("days", "2"), 2)
    return jsonify(analytics.box_series(b.id, days))


@admin_bp.route("/fleet/<int:controller_id>/series.json")
def fleet_series(controller_id):
    """The chart window for an EchoTherm controller (app/ctrl_history.py)."""
    ctrl = Controller.query.get_or_404(controller_id)
    require_role("viewer", ctrl)
    from app import ctrl_history
    days = {"1": 1, "2": 2, "7": 7, "30": 30}.get(request.args.get("days", "1"), 1)
    keys = [k for k in request.args.get("keys", "").split(",") if k]
    data = ctrl_history.series(ctrl, days, keys)
    data["name"] = ctrl.name + (f" ({ctrl.building.name})" if ctrl.building else "")
    data["url"] = url_for("admin.fleet_controller", controller_id=ctrl.id)
    return jsonify(data)


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


# ─── Skyline / Bloom wall (app/bloom.py) ─────────────────────────────────

def _bloom_rows(now, assets=None):
    """One display row per Bloom asset: its card, freshness and whether it is
    a problem (dark, a severe alarm, or a heat call with a cold loop)."""
    from app import bloom
    if assets is None:
        assets = (BloomAsset.query
                  .filter(BloomAsset.is_active.is_(True), BloomAsset.deleted_at.is_(None))
                  .order_by(BloomAsset.name).all())
    stale = timedelta(minutes=current_app.config.get("BLOOM_POLL_INTERVAL_SECONDS", 60) // 60 + 10)
    rows = []
    for a in assets:
        st = a.status
        c = bloom.card(st.data, a.view, a.plant_tag) if st and st.ok and st.data else None
        last_ok = st.last_ok_at if st else None
        if not last_ok or now - last_ok > stale:
            state = "dark"
            why = (st.error if st and st.error else "never read") if not last_ok else "not read recently"
        else:
            state, why = "live", None
        # A dark box, or a crit card (calling for heat with a cold loop, or a
        # boiler/pump fault alarm — see app/bloom.card) is a problem to chase.
        problem = state == "dark" or bool(c and c["tone"] == "crit")
        rows.append({"asset": a, "st": st, "c": c, "state": state, "why": why,
                     "last_ok": last_ok, "problem": problem,
                     "age": fleet_health.ago((now - last_ok).total_seconds()) if last_ok else None})
    return rows


@admin_bp.route("/skyline")
def skyline_page():
    if not is_superuser():
        abort(403)
    now = datetime.utcnow()
    rows = _bloom_rows(now)
    summary = {"total": len(rows), "live": sum(r["state"] == "live" for r in rows),
               "dark": sum(r["state"] == "dark" for r in rows),
               "heating": sum(1 for r in rows if r["c"] and r["c"]["tone"] == "heat"),
               "alarms": sum((r["c"]["n_alarms"] if r["c"] else 0) for r in rows),
               "problems": sum(r["problem"] for r in rows)}
    show, q = request.args.get("show", "all"), (request.args.get("q") or "").strip().lower()
    if show == "problems":
        rows = [r for r in rows if r["problem"]]
    elif show == "heating":
        rows = [r for r in rows if r["c"] and r["c"]["tone"] == "heat"]
    elif show == "dark":
        rows = [r for r in rows if r["state"] == "dark"]
    else:
        show = "all"
    if q:
        rows = [r for r in rows if q in r["asset"].name.lower()]
    rows.sort(key=lambda r: (not r["problem"], r["asset"].name.lower()))
    return render_template("skyline.html", title="Skyline", rows=rows, summary=summary, show=show,
                           q=request.args.get("q", ""),
                           poll_min=round(current_app.config.get("BLOOM_POLL_INTERVAL_SECONDS", 60) / 60) or 1)


@admin_bp.route("/fans")
def fans_page():
    """Roof exhaust fans on every Bloom building that has them: running or not,
    the cold-weather schedule, and whether that schedule can engage. Read
    view-only (app/bloom_fans.py)."""
    from app import bloom_fans, legacy
    now = datetime.utcnow()
    hour = now.replace(tzinfo=timezone.utc).astimezone(legacy.LOCAL).hour
    cold_f = current_app.config.get("FAN_COLD_F", 35.0)
    cfg = current_app.config
    pl = bloom_fans.plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))
    hyst = cfg.get("FAN_PLAN_HYST_F", 2.0)
    rows = _fan_rows(now)                             # only the buildings this person may see
    pg = bloom_fans.page(rows, show=request.args.get("show", "all"), q=request.args.get("q", ""),
                         sort=request.args.get("sort", "property"))
    from app.models import BloomFanAuto
    auto_props = {a.prop for a in BloomFanAuto.query.filter_by(enabled=True).all()}
    return render_template("fans.html", title="Fans", q=request.args.get("q", ""), cold_f=cold_f, hour=hour, plan=pl, hyst=hyst,
                           hold=cfg.get("FAN_PLAN_HOLD_MIN", 30.0),
                           auto_props=auto_props, unfiled=bloom_fans.UNFILED, manage=is_superuser(),
                           poll_min=round(current_app.config.get("BLOOM_POLL_INTERVAL_SECONDS", 60) / 60) or 1,
                           **pg)


def _fan_index():
    """The lookup that files a fan site under a building of the portal's own
    hierarchy, by name (bloom_fans.place). The value is (property name, owner
    name, building id, property id). Built from EVERY building so a site
    resolves to the same building for everyone; who may see it is decided
    from that building afterwards."""
    from sqlalchemy.orm import joinedload
    from app import bloom_fans
    return bloom_fans.building_index(
        (b.name, (b.property.name, b.property.client.name if b.property.client else None, b.id, b.property_id))
        for b in Building.query.options(joinedload(Building.property).joinedload(Property.client)).all()
        if b.property)


def _fan_rows(now):
    """One summary row per fan site this request may see: all of them for the
    administrator, otherwise only sites filed under a building the person
    holds a grant on. A site not filed under any building is the
    administrator's alone."""
    from sqlalchemy.orm import joinedload
    from app import bloom_fans, legacy
    from app.models import BloomFanSite
    local = now.replace(tzinfo=timezone.utc).astimezone(legacy.LOCAL)
    cfg = current_app.config
    pl = bloom_fans.plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))
    index, ids = _fan_index(), visible_building_ids()
    rows = []
    for site in BloomFanSite.query.options(joinedload(BloomFanSite.account)).all():
        if not site.account or not site.account.is_active:
            continue
        placed = bloom_fans.place(index, site.name, site.asset_key)
        if ids is not None and (placed is None or placed[2] not in ids):
            continue
        prop, client, _building_id, prop_id = placed or (None, None, None, None)
        s = bloom_fans.summary(site.data, now=now, cold_f=cfg.get("FAN_COLD_F", 35.0), hour=local.hour,
                               minute=local.minute, pl=pl, hyst=cfg.get("FAN_PLAN_HYST_F", 2.0))
        s.update(id=site.id, name=site.name, account=site.account.label, prop=prop, client=client, prop_id=prop_id,
                 heard=bloom_fans.age_words(s["heard_age"]),
                 url=f"{site.account.base_url.rstrip('/')}/property/{site.asset_key}/fanSchedule")
        rows.append(s)
    return rows


def _fan_site_or_403(site_id):
    """A fan site this request may READ (its history chart), by the rule in _fan_rows."""
    from app import bloom_fans
    from app.models import BloomFanSite
    site = BloomFanSite.query.get_or_404(site_id)
    ids = visible_building_ids()
    if ids is not None:
        placed = bloom_fans.place(_fan_index(), site.name, site.asset_key)
        if placed is None or placed[2] not in ids:
            abort(403)
    return site


def _fans_live_building(site):
    """This fan site's building object fresh from Bloom — the full one, whose
    points carry the ids a write needs. Returns (building or None, session, token)."""
    import requests
    from app import bloom
    acct = site.account
    sess = requests.Session()
    tok = acct.token or bloom.login(acct, sess)
    data, tok2 = bloom.fetch_json(acct, sess, tok, "/api/assets",
                                  current_app.config.get("BLOOM_POLL_TIMEOUT_SECONDS", 20))
    if tok2 and tok2 != acct.token:
        acct.token, acct.token_at = tok2, datetime.utcnow()
        db.session.commit()
    for b in bloom.buildings(data):
        if str(bloom.asset_key(b)) == site.asset_key:
            return b, sess, tok2
    return None, sess, tok2


def _fans_property_sites(name):
    """The fan sites filed under one property — by the same name matching the
    Fans page and the poller use (bloom_fans.place) — in street order."""
    from sqlalchemy.orm import joinedload
    from app import bloom_fans
    from app.models import BloomFanSite
    index = bloom_fans.building_index(
        (b.name, b.property.name) for b in Building.query.options(joinedload(Building.property)).all() if b.property)
    sites = [s for s in BloomFanSite.query.options(joinedload(BloomFanSite.account)).all()
             if s.account and s.account.is_active and bloom_fans.place(index, s.name, s.asset_key) == name]
    return sorted(sites, key=lambda s: bloom_fans._natural(s.name))


def _fans_live_accounts(sites):
    """One fresh /api/assets read per Bloom login these sites use:
    {account id: {"by": {locid: full building object}, "sess", "tok"}}."""
    import requests
    from app import bloom
    out = {}
    for acct in {s.account.id: s.account for s in sites}.values():
        sess = requests.Session()
        tok = acct.token or bloom.login(acct, sess)
        data, tok2 = bloom.fetch_json(acct, sess, tok, "/api/assets",
                                      current_app.config.get("BLOOM_POLL_TIMEOUT_SECONDS", 20))
        if tok2 and tok2 != acct.token:
            acct.token, acct.token_at = tok2, datetime.utcnow()
            db.session.commit()
        out[acct.id] = {"by": {str(bloom.asset_key(b)): b for b in bloom.buildings(data)}, "sess": sess, "tok": tok2}
    return out


@admin_bp.route("/fans/property", methods=["GET", "POST"])
def fans_property():
    """Fan schedules for a whole property, in the order they are done: load the
    plan's three schedules into every building's slots 3-5, point the fan
    groups at the slot for the property's temperature, and switch on the
    portal doing that second step by itself. Plus the way back (every group to
    Schedule 1). Every step that writes shows what will change first, is sent
    only if that is still the change, and is logged per building."""
    if not is_superuser():
        abort(403)
    import time as _time
    from app import bloom, bloom_fans, legacy
    from app.models import BloomFanAuto, BloomFanWrite
    name = (request.values.get("name") or "").strip()
    sites = _fans_property_sites(name) if name else []
    if not sites:
        abort(404)
    cfg = current_app.config
    pl = bloom_fans.plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))

    def local(ts):
        return ts.replace(tzinfo=timezone.utc).astimezone(legacy.LOCAL).strftime("%b %d, %I:%M:%S %p").replace(" 0", " ")

    # The property's place in the plan is the poller's — one shared temperature,
    # hysteresis carried from poll to poll — so this page and the automatic
    # switching can never disagree about which slot is the target.
    portal = next((p for p in ((s.data or {}).get("portal") or {} for s in sites) if p.get("band")), {})
    band = next((b for b in pl if b["key"] == portal.get("band")), None)
    target = bloom_fans.PLAN_SLOTS[band["key"]] if band else None
    # A move to another band that is waiting out the hold (bloom_fans.shared_bands).
    pending, due = None, bloom_fans._iso(portal.get("due"))
    nxt_band = next((b for b in pl if b["key"] == portal.get("pend")), None)
    if band and nxt_band and due and nxt_band["key"] != band["key"]:
        mins = max(1, int(-(-(due - datetime.utcnow()).total_seconds() // 60)))
        pending = {"band": nxt_band, "slot": bloom_fans.PLAN_SLOTS[nxt_band["key"]], "mins": mins}
    STEP = {"load": "push", "switch": "switch", "restore": "restore"}
    # Where the portal loaded the schedules before, a slot fans now follow may
    # have its hours revised (the plan was changed); anywhere else it is refused.
    revisable = bloom_fans.portal_loaded(
        BloomFanWrite.query.filter(BloomFanWrite.site_id.in_([s.id for s in sites]), BloomFanWrite.ok.is_(True)).all())

    def build(accts):
        now = datetime.utcnow()
        rows = []
        for s in sites:
            b = accts[s.account.id]["by"].get(s.asset_key)
            if b is None:
                rows.append({"site": s, "live": False, "stale": True, "push": None, "switch": None, "restore": None})
                continue
            sm = bloom_fans.summary(bloom_fans.trim(b), now=now)
            rows.append({"site": s, "live": True, "stale": sm["stale"], "on": sm["on"], "total": sm["total"],
                         "hand": sm.get("hand") or 0,
                         "push": bloom_fans.plan_push(b, pl, revise=s.id in revisable),
                         "switch": bloom_fans.plan_assign(b, pl, target_key=band["key"]) if band else None,
                         "restore": bloom_fans.plan_assign(b, pl, slot=1)})
        act = {}
        for k, part in STEP.items():
            todo = [r for r in rows if r["live"] and not r["stale"] and r[part] and r[part]["points"]]
            act[k] = {"todo": todo, "n": len(todo), "changes": sum(r[part]["n_changes"] for r in todo),
                      "digest": bloom_fans.combined_digest(f"{r['site'].id}:{r[part]['digest']}" for r in todo)}
        # a load that rewrites hours fan groups are following right now
        act["load"]["live"] = [(r["site"].name, sl) for r in act["load"]["todo"] for sl in r["push"]["slots"] if sl["live"]]
        return rows, act

    error = result = None
    rows, act = [], {k: {"todo": [], "n": 0, "changes": 0, "digest": "", "live": []} for k in STEP}
    accts = {}
    try:
        accts = _fans_live_accounts(sites)
        rows, act = build(accts)
    except Exception as e:
        db.session.rollback()
        error = str(e)[:300]

    action = request.form.get("action", "") if request.method == "POST" else ""
    note = (request.form.get("note") or "").strip()[:500]

    def set_auto(on):
        row = db.session.get(BloomFanAuto, name) or BloomFanAuto(prop=name)
        row.enabled, row.by, row.at = on, _who(), datetime.utcnow()
        db.session.add(row)
        db.session.commit()

    if action in ("auto_on", "auto_off"):
        set_auto(action == "auto_on")
        result = {"state": "auto", "on": action == "auto_on"}
    elif action in STEP and not error:
        a, part = act[action], STEP[action]
        if not a["n"]:
            result = {"state": "nothing"}
        elif request.form.get("digest", "") != a["digest"]:
            result = {"state": "changed"}
        elif not note:
            result = {"state": "note"}
        else:
            if action == "restore":
                set_auto(False)          # or the portal would switch them straight back
            written, failed = [], []
            for r in a["todo"]:
                site, acct = r["site"], accts[r["site"].account.id]
                w = BloomFanWrite(site_id=site.id, by=_who(), note=note, changes=r[part]["log"], ok=False)
                try:
                    data, acct["tok"] = bloom.put_points(site.account, acct["sess"], acct["tok"], site.asset_key,
                                                         r[part]["points"], note)
                    w.ok, w.detail = bloom_fans.reply_summary(data)
                except Exception as e:
                    db.session.rollback()
                    w.detail = str(e)[:500]
                if not w.ok:
                    failed.append((site.name, bloom_fans.tidy_detail(w.detail) or "no reply"))
                db.session.add(w)
                db.session.commit()
                written.append(w)
            landed = 0
            try:                                     # read everything back: did the values land?
                _time.sleep(3)
                accts = _fans_live_accounts(sites)
                rows, act = build(accts)
                after = {r["site"].id: r for r in rows}
                for w in written:
                    r = after.get(w.site_id)
                    if not w.ok or r is None or not r["live"]:
                        continue
                    w.landed = (not r[part]["blocked"]) and r[part]["n_changes"] == 0
                    landed += bool(w.landed)
                db.session.commit()
            except Exception as e:
                db.session.rollback()
            result = {"state": "sent", "action": action, "n": len(written), "landed": landed,
                      "ok": sum(1 for w in written if w.ok), "failed": failed}

    auto = db.session.get(BloomFanAuto, name)
    out = portal.get("out")
    notes = {"load": (f"Fleet portal: update the plan's hours in slots 3-5 at {name}" if act["load"].get("live")
                      else f"Fleet portal: load the plan's schedules into slots 3-5 at {name}"),
             "switch": (f"Fleet portal: {name} at {out:.0f}° — fan groups to the '{band['label']}' schedule (slot {target})"
                        if band and out is not None else ""),
             "restore": f"Fleet portal: {name} fan groups back to Schedule 1"}
    by_id = {s.id: s.name for s in sites}
    log = [{"when": local(w.at), "site": by_id.get(w.site_id, "?"), "by": w.by, "ok": w.ok, "landed": w.landed,
            "detail": bloom_fans.tidy_detail(w.detail), "summary": bloom_fans.write_summary(w.changes), "note": w.note}
           for w in (BloomFanWrite.query.filter(BloomFanWrite.site_id.in_(list(by_id)))
                     .order_by(BloomFanWrite.at.desc()).limit(60).all())]
    return render_template("fans_property.html", title=f"{name} — fan schedules", name=name, rows=rows, act=act,
                           error=error, result=result, band=band, target=target, out=out,
                           n_sensors=portal.get("n") or 0, notes=notes, log=log, pending=pending,
                           hold=cfg.get("FAN_PLAN_HOLD_MIN", 30.0),
                           auto_on=bool(auto and auto.enabled), auto_by=auto.by if auto else None,
                           auto_at=local(auto.at) if auto else None,
                           auto_master=bool(cfg.get("FAN_AUTO_ENABLED", True)),
                           read_at=local(datetime.utcnow()))


@admin_bp.route("/fans/<int:site_id>/push", methods=["GET", "POST"])
def fans_push(site_id):
    """Load the plan's schedules into one building's Bloom schedule slots — the
    one place the portal WRITES to Bloom. GET reads the building live and shows
    exactly what would change; POST sends it only if the change is still the
    one that was shown, and logs it (app/bloom_fans.plan_push, bloom.put_points)."""
    if not is_superuser():
        abort(403)
    import time as _time
    from app import bloom, bloom_fans, legacy
    from app.models import BloomFanSite, BloomFanWrite
    site = BloomFanSite.query.get_or_404(site_id)
    cfg = current_app.config
    pl = bloom_fans.plan(cfg.get("FAN_PLAN_MILD_FROM_F", 36.0), cfg.get("FAN_PLAN_FREE_FROM_F", 56.0))

    def local(ts):
        return ts.replace(tzinfo=timezone.utc).astimezone(legacy.LOCAL).strftime("%b %d, %I:%M:%S %p").replace(" 0", " ")

    error = preview = result = None
    note = (request.form.get("note") or "").strip()[:500] if request.method == "POST" else \
        "Fleet portal: load the plan's schedules into slots 3-5 (no fan group follows them)"
    # only where the portal loaded the slots before may a followed slot's hours be revised
    revise = site.id in bloom_fans.portal_loaded(BloomFanWrite.query.filter_by(site_id=site.id, ok=True).all())
    try:
        building, sess, tok = _fans_live_building(site)
        if building is None:
            error = "the building is not in Bloom's feed for this login"
        else:
            preview = bloom_fans.plan_push(building, pl, revise=revise)
            if request.method != "POST" and preview["live"]:
                note = "Fleet portal: update the plan's hours in slots 3-5 (fan groups follow a revised slot)"
    except Exception as e:
        db.session.rollback()
        error = str(e)[:300]

    if request.method == "POST" and preview is not None:
        if not preview["points"]:
            pass                                     # nothing to send: the page says so
        elif request.form.get("digest", "") != preview["digest"]:
            result = {"state": "changed"}
        elif not note:
            result = {"state": "note"}
        else:
            row = BloomFanWrite(site_id=site.id, by=_who(), note=note, changes=preview["log"], ok=False)
            n = preview["n_changes"]
            try:
                data, tok = bloom.put_points(site.account, sess, tok, site.asset_key, preview["points"], note)
                row.ok, row.detail = bloom_fans.reply_summary(data)      # Bloom's own verdict and sentence
            except Exception as e:
                row.detail = str(e)[:500]
            if row.ok:
                try:                                 # read it back: did the values land?
                    _time.sleep(3)
                    again, sess, tok = _fans_live_building(site)
                    if again is not None:
                        row.landed = bloom_fans.push_landed(again, pl, revise=revise)
                        preview = bloom_fans.plan_push(again, pl, revise=revise)
                except Exception as e:
                    db.session.rollback()
                    row.landed = None
            db.session.add(row)
            db.session.commit()
            result = {"state": "sent" if row.ok else "failed", "landed": bool(row.landed), "n": n, "detail": row.detail}

    log = []
    for w in (BloomFanWrite.query.filter_by(site_id=site.id).order_by(BloomFanWrite.at.desc()).limit(15).all()):
        ch = w.changes or []
        summary = bloom_fans.write_summary(ch)
        log.append({"when": local(w.at), "by": w.by, "ok": w.ok, "landed": w.landed,
                    "detail": bloom_fans.tidy_detail(w.detail),
                    "n": len(ch), "summary": summary, "note": w.note})
    return render_template("fans_push.html", title=f"{site.name} — push schedules", site=site, p=preview,
                           error=error, result=result, note=note, log=log, read_at=local(datetime.utcnow()),
                           bloom_url=f"{site.account.base_url.rstrip('/')}/property/{site.asset_key}/fanSchedule")


@admin_bp.route("/fans/<int:site_id>/raw")
def fans_raw(site_id):
    """The audit view for one building's fans: every stored Bloom point beside
    what the Fans page shows for it (bloom_fans.raw_view). ?format=json returns
    the stored slice itself."""
    if not is_superuser():
        abort(403)
    from app import bloom_fans, legacy
    from app.models import BloomFanSite
    site = BloomFanSite.query.get_or_404(site_id)
    if request.args.get("format") == "json":
        return jsonify({"name": site.name, "asset_key": site.asset_key,
                        "pulled_at_utc": site.polled_at.isoformat() + "Z", "data": site.data})
    now = datetime.utcnow()
    view = bloom_fans.raw_view(site.data, now=now, tz=legacy.LOCAL)
    pulled = site.polled_at.replace(tzinfo=timezone.utc).astimezone(legacy.LOCAL)
    return render_template("fans_raw.html", title=f"{site.name} — raw fan values", site=site, v=view,
                           pulled=pulled.strftime("%b %d, %I:%M:%S %p").replace(" 0", " "),
                           pulled_age=bloom_fans.age_words((now - site.polled_at).total_seconds() / 60),
                           bloom_url=f"{site.account.base_url.rstrip('/')}/property/{site.asset_key}")


@admin_bp.route("/fans/<int:site_id>/series.json")
def fans_series(site_id):
    """History for one building's fans — outdoor, fans running, and an on/off
    row per fan (app/bloom_fans.series) — drawn in the expanded row on /fans."""
    from app import bloom_fans
    site = _fan_site_or_403(site_id)
    days = {"1": 1, "2": 2, "7": 7, "30": 30}.get(request.args.get("days", "1"), 1)
    data = bloom_fans.series(site, days)
    data["name"] = site.name
    return jsonify(data)


@admin_bp.route("/skyline/<int:asset_id>")
def skyline_building(asset_id):
    """Everything Bloom has for one Skyline building, with a Back button
    (returns to wherever you came from, else Live). See app/bloom.py::detail."""
    from urllib.parse import urlparse
    from app import bloom
    a = _bloom_asset_or_403(asset_id)
    st = a.status
    d = bloom.detail(st.data, a.view, a.plant_tag) if st and st.ok and st.data else None
    read_ago = (fleet_health.ago((datetime.utcnow() - st.last_ok_at).total_seconds())
                if st and st.last_ok_at else None)
    # DHW System A/B, scraped from the Reliable Controls MACH-ProWeb BAS
    # (app/rc_dhw.py) — not in Bloom. Attached by the building this asset is on.
    dhw = dhw_ago = None
    if a.building_id:
        from app.models import RcDhwTower
        tw = (RcDhwTower.query
              .filter_by(building_id=a.building_id, is_active=True).first())
        if tw and tw.status and tw.status.ok:
            dhw = tw.status
            dhw_ago = (fleet_health.ago((datetime.utcnow() - dhw.last_ok_at).total_seconds())
                       if dhw.last_ok_at else None)
    ref = request.referrer or ""
    back = ref if (ref and urlparse(ref).netloc == request.host
                   and "/skyline/" not in urlparse(ref).path) else url_for(
        "admin.live_page" if is_superuser() else "admin.home_page")
    # Same Anagha cell colours as the Live wall (supply/return loop, outdoor
    # band, comfort cell, hot water), computed from the card's readings.
    tints = {}
    if d:
        from app.legacy import tint as _tint, dhw_tint as _dhw_tint
        cc = d["card"]
        role = {"out": cc.get("outside_f"), "supp": cc.get("supply_f"),
                "retn": cc.get("return_f"), "mix": cc.get("dhw_supply_f")}
        tints = _tint(role, cc.get("space_avg_f"), cc.get("setpoint_f"), None, [])
        if dhw and dhw.supply_f is not None:            # Reliable Controls DHW tower
            rc = _dhw_tint(dhw.supply_f)
            if rc:
                tints["dhw_rc"] = rc
    from app import heat_overnight
    heat = heat_overnight.for_bloom([a.id]).get(a.id)
    return render_template("bloom_detail.html", title=a.name, a=a, d=d, st=st, back=back,
                           read_ago=read_ago, dhw=dhw, dhw_ago=dhw_ago, tints=tints, heat=heat)


@admin_bp.route("/skyline/<int:asset_id>/series.json")
def skyline_series(asset_id):
    """The Live page's pop-up chart for a Bloom building (app/bloom.series).
    Same shape as a legacy box's /live/<id>/series.json."""
    from app import bloom
    a = _bloom_asset_or_403(asset_id)
    days = {"1": 1, "2": 2, "7": 7, "30": 30}.get(request.args.get("days", "1"), 1)
    keys = [k for k in request.args.get("keys", "").split(",") if k]
    data = bloom.series(a, days, keys)
    data["name"] = a.name
    data["url"] = url_for("admin.skyline_building", asset_id=a.id)
    return jsonify(data)


@admin_bp.route("/portfolio")
def portfolio_page():
    """The whole fleet by owner -> property, across all sources (app/portfolio.py)."""
    if not is_superuser():
        abort(403)
    import json
    from app import portfolio
    owners = portfolio.tree()
    legacy = portfolio.unassigned()
    return render_template("portfolio.html", title="Portfolio",
                           owners_json=json.dumps(owners), legacy_json=json.dumps(legacy),
                           legacy_count=len(legacy))


def _portfolio_destination(data):
    """Resolve the target Property for a file/move: an existing property_id, or
    a new Owner/Property created on the spot. Returns (property, error_response)
    — exactly one is non-None. Shared by assign and move."""
    prop_id = data.get("property_id")
    if prop_id:
        prop = Property.query.get(int(prop_id))
        return (prop, None) if prop else (None, (jsonify(error="That property no longer exists."), 404))
    owner_name = (data.get("owner_name") or "").strip()
    prop_name = (data.get("property_name") or "").strip()
    if not owner_name or not prop_name:
        return None, (jsonify(error="Enter both an owner and a property name."), 400)
    client = Client.query.filter_by(name=owner_name).first()
    if client is None:
        client = Client(name=owner_name, billing_method="invoice")
        db.session.add(client)
        db.session.flush()
    prop = Property.query.filter_by(client_id=client.id, name=prop_name).first()
    if prop is None:
        prop = Property(client_id=client.id, name=prop_name)
        db.session.add(prop)
        db.session.flush()
    return prop, None


@admin_bp.route("/portfolio/assign", methods=["POST"])
def portfolio_assign():
    """File 2010 boxes into a property: one Building per box (named after the
    box), with the box's building_id set. See app/portfolio.py."""
    if not is_superuser():
        abort(403)
    data = request.get_json(silent=True) or {}
    box_ids = [int(b) for b in (data.get("box_ids") or []) if str(b).isdigit()]
    if not box_ids:
        return jsonify(error="No boxes selected."), 400
    prop, err = _portfolio_destination(data)
    if err:
        return err
    filed = 0
    for bid in box_ids:
        box = LegacyBox.query.get(bid)
        if box is None or box.building_id is not None or box.deleted_at is not None:
            continue
        building = Building(property_id=prop.id, name=box.name)
        db.session.add(building)
        db.session.flush()
        box.building_id = building.id
        filed += 1
    db.session.commit()
    return jsonify(ok=True, filed=filed, where=f"{prop.client.name} / {prop.name}")


@admin_bp.route("/portfolio/move", methods=["POST"])
def portfolio_move():
    """Move already-placed buildings to another property/owner (i.e. change
    their ownership) — reassigns Building.property_id. Destination is an
    existing property or a new Owner/Property. See app/portfolio.py."""
    if not is_superuser():
        abort(403)
    data = request.get_json(silent=True) or {}
    building_ids = [int(b) for b in (data.get("building_ids") or []) if str(b).isdigit()]
    if not building_ids:
        return jsonify(error="No buildings selected."), 400
    prop, err = _portfolio_destination(data)
    if err:
        return err
    moved = 0
    for bid in building_ids:
        b = Building.query.get(bid)
        if b is None or b.property_id == prop.id:
            continue
        b.property_id = prop.id
        moved += 1
    db.session.commit()
    return jsonify(ok=True, moved=moved, where=f"{prop.client.name} / {prop.name}")


@admin_bp.route("/portfolio/delete", methods=["POST"])
def portfolio_delete():
    """Delete a property (and its buildings) or a whole owner (all its
    properties). 2010 boxes under it return to Unassigned and Bloom assets
    detach (both recoverable); REFUSED when a managed EchoTherm controller is
    under it, since that would delete the controller. Superuser only."""
    if not is_superuser():
        abort(403)
    data = request.get_json(silent=True) or {}
    kind, _id = data.get("kind"), data.get("id")
    if kind == "property":
        target = Property.query.get(int(_id)) if _id else None
        props = [target] if target else []
        where = f"{target.client.name} / {target.name}" if target else None
    elif kind == "owner":
        target = Client.query.get(int(_id)) if _id else None
        props = list(target.properties) if target else []
        where = target.name if target else None
    else:
        return jsonify(error="Unknown delete target."), 400
    if target is None:
        return jsonify(error="That item no longer exists."), 404

    bldg_ids = [b.id for p in props for b in p.buildings]
    ctrls = sum(1 for p in props for b in p.buildings for c in b.controllers if c.is_active)
    if ctrls:
        return jsonify(error=f"Can't delete: {ctrls} managed EchoTherm controller(s) are under it. "
                             f"Remove them from the Controllers page first."), 400
    boxes = LegacyBox.query.filter(LegacyBox.building_id.in_(bldg_ids)).count() if bldg_ids else 0
    if bldg_ids:   # detach devices first so they survive the building delete
        LegacyBox.query.filter(LegacyBox.building_id.in_(bldg_ids)).update({"building_id": None}, synchronize_session=False)
        BloomAsset.query.filter(BloomAsset.building_id.in_(bldg_ids)).update({"building_id": None}, synchronize_session=False)
    db.session.delete(target)
    db.session.commit()
    return jsonify(ok=True, kind=kind, where=where, boxes=boxes)


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
            ("desired_avg_f", "dit"), ("target_supply_avg_f", "target_avg"), ("supply_while_calling_avg_f", "supp_call_avg"),
            ("hours_hot_water_cold", "short_h"), ("below_minimum_hours", "law_hours"),
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


def _access_summary(user) -> str:
    """'Berry, DCA (view)' — what a person can see, for the Users list."""
    by_role = {}
    for p in user.permissions:
        model = _SCOPE_MODELS.get(p.scope_type)
        entity = model.query.get(p.scope_id) if model else None
        by_role.setdefault(p.role, []).append(entity.name if entity else f"#{p.scope_id} (deleted)")
    words = {"viewer": "view", "operator": "operate", "admin": "admin"}
    return "; ".join(f"{', '.join(sorted(by_role[r]))} ({words[r]})" for r in ("admin", "operator", "viewer")
                     if r in by_role) or "nothing yet"


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
            "cells": [u.name, u.email, u.login, _access_summary(u)],
            "can_edit": True,
            "can_archive": True,
            "extra_url": url_for("admin.user_permissions", user_id=u.id),
            "see_as_url": url_for("auth.view_as", user_id=u.id) if u.is_active else None,
        }
        for u in users
    ]
    return render_template(
        "entity_list.html",
        title="Users",
        columns=["Name", "Email", "Login", "Can see"],
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
        flash(f"User {user.name!r} created. Now choose what they can see.", "success")
        return redirect(url_for("admin.user_permissions", user_id=user.id))
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
    # The simple way to share: one tick per property (view-only).
    held = {p.scope_id: p.role for p in grants if p.scope_type == "property"}
    by_client = {p.scope_id: p.role for p in grants if p.scope_type == "client"}
    share = []
    for c in clients:
        props = [{"id": p.id, "name": p.name, "role": held.get(p.id), "via_client": by_client.get(c.id),
                  "n": sum(1 for b in p.buildings if b.is_active)}
                 for p in sorted(c.properties, key=lambda x: x.name.lower()) if p.is_active]
        if props:
            share.append({"client": c.name, "props": props})
    return render_template(
        "user_permissions.html", title=f"Permissions: {user.name}", user=user, grants=grant_rows,
        clients=clients, properties=properties, buildings=buildings, share=share,
    )


@admin_bp.route("/users/<int:user_id>/properties", methods=["POST"])
@superuser_required
def set_user_properties(user_id):
    """Share properties view-only by ticking them: the person's 'viewer'
    grants at property level become exactly the ticked set. Operator and
    admin grants, and grants on a client or a single building, are not touched."""
    user = User.query.get_or_404(user_id)
    want = {int(v) for v in request.form.getlist("property_id") if v.isdigit()}
    valid = {p.id for p in Property.query.filter(Property.id.in_(want or [-1])).all()}
    have = {p.scope_id: p for p in Permission.query.filter_by(user_id=user.id, scope_type="property").all()}
    added = removed = 0
    for pid in valid - set(have):
        db.session.add(Permission(user_id=user.id, scope_type="property", scope_id=pid, role="viewer"))
        added += 1
    for pid, grant in have.items():
        if pid not in valid and grant.role == "viewer":
            db.session.delete(grant)
            removed += 1
    db.session.commit()
    flash(f"View access saved: {added} propert{'y' if added == 1 else 'ies'} added, {removed} removed.", "success")
    return redirect(url_for("admin.user_permissions", user_id=user.id))


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


# ─── Directory: managers from the violations portal (app/directory.py) ───

def _clip(v, n):
    v = str(v).strip() if v is not None else ""
    return v[:n] or None


def _directory_state():
    from app.models import DirectoryState
    st = db.session.get(DirectoryState, 1)
    if st is None:
        st = DirectoryState(id=1, ok=False)
        db.session.add(st)
    return st


def directory_sync(now=None, fetch=None):
    """Read the violations portal's directory and store it (DirectoryProperty).
    Links the administrator has made are kept; a property the directory no
    longer lists is marked not present, never deleted. Returns (ok, message)."""
    from app import directory
    from app.models import DirectoryProperty
    cfg, now = current_app.config, now or datetime.utcnow()
    try:
        data = (fetch or directory.fetch)(cfg.get("DIRECTORY_URL", ""), cfg.get("DIRECTORY_TOKEN", ""))
    except Exception as e:
        db.session.rollback()
        st = _directory_state()
        st.attempted_at, st.ok, st.error = now, False, str(e)[:500]
        db.session.commit()
        return False, st.error
    have = {d.ext_id: d for d in DirectoryProperty.query.all()}
    seen = set()
    for p in data["properties"]:
        try:
            ext = int(p["id"])
        except (KeyError, TypeError, ValueError):
            continue
        row = have.get(ext) or DirectoryProperty(ext_id=ext)
        row.name = _clip(p.get("name"), 200) or f"#{ext}"
        row.region, row.unit_count = _clip(p.get("region"), 100), p.get("unit_count") if isinstance(p.get("unit_count"), int) else None
        row.pm_name, row.pm_email = _clip(p.get("pm_name"), 200), _clip(p.get("pm_email"), 255)
        row.rpm_name, row.rpm_email = _clip(p.get("rpm_name"), 200), _clip(p.get("rpm_email"), 255)
        row.site_email = _clip(p.get("site_email"), 255)
        row.buildings = [{k: b.get(k) for k in ("address", "city", "state", "zip", "bin")}
                         for b in (p.get("buildings") or []) if isinstance(b, dict) and b.get("address")]
        row.open_violations = p.get("open_violations") if isinstance(p.get("open_violations"), int) else None
        row.overdue_obligations = p.get("overdue_obligations") if isinstance(p.get("overdue_obligations"), int) else None
        row.present, row.seen_at = True, now
        db.session.add(row)
        seen.add(ext)
    for ext, row in have.items():
        if ext not in seen:
            row.present = False
    st = _directory_state()
    st.attempted_at, st.fetched_at, st.ok, st.error = now, now, True, None
    st.org, st.n = _clip(data.get("org"), 200), len(seen)
    db.session.commit()
    return True, f"Read {len(seen)} properties from the violations portal."


def _directory_view():
    """Everything the Directory page shows, worked out fresh: the directory's
    properties with their link (or a suggested one), the managers on linked
    properties with what approving each would change, and removals on offer."""
    from sqlalchemy.orm import joinedload
    from app import directory
    from app.models import DirectoryProperty
    domains = current_app.config.get("DIRECTORY_EMAIL_DOMAINS") or ()
    rows = DirectoryProperty.query.filter_by(present=True).order_by(DirectoryProperty.name).all()
    props = (Property.query.options(joinedload(Property.client), joinedload(Property.buildings))
             .filter(Property.is_active.is_(True)).order_by(Property.name).all())
    pname = {p.id: p.name for p in props}
    fleet = [{"id": p.id, "name": p.name, "addresses": [b.name for b in p.buildings if b.is_active]} for p in props]
    suggest = directory.suggest_links(
        [{"id": r.id, "name": r.name, "addresses": [b.get("address") for b in (r.buildings or [])]}
         for r in rows if r.property_id not in pname], fleet)
    links = []
    for r in rows:
        linked = r.property_id if r.property_id in pname else None
        s = suggest.get(r.id)
        has_mgr = bool(directory.norm_email(r.pm_email) or directory.norm_email(r.rpm_email))
        links.append({"row": r, "linked": linked, "suggest": s, "suggest_name": pname.get(s["property_id"]) if s else None,
                      "has_mgr": has_mgr,
                      # offered ready-chosen only when the NAMES agree; a streets-only match is a hint
                      "choice": linked or (s["property_id"] if s and s["confidence"] == "name" else None),
                      # to decide = something here looks like it; the rest have no counterpart in this portal
                      "group": "linked" if linked else ("todo" if s else "rest")})
    persons = directory.people(
        [{"name": r.name, "property_id": r.property_id if r.property_id in pname else None, "pm_name": r.pm_name,
          "pm_email": r.pm_email, "rpm_name": r.rpm_name, "rpm_email": r.rpm_email, "site_email": r.site_email}
         for r in rows], domains)
    users = {u.email.strip().lower(): {"id": u.id, "active": u.is_active} for u in User.query.all()}
    grants = {}
    for gr in Permission.query.filter_by(scope_type="property").all():
        grants.setdefault(gr.user_id, {})[gr.scope_id] = gr.source
    plan = directory.access_plan(persons, users, grants)
    for p in plan:
        p["sees"] = sorted(pname.get(i, f"#{i}") for i in p["property_ids"])
        p["add_names"] = [pname.get(i, f"#{i}") for i in p["add"]]
        p["drop_names"] = [pname.get(i, f"#{i}") for i in p["drop"]]
    orphans = directory.orphans(persons, users, grants)
    for o in orphans:
        o["drop_names"] = [pname.get(i, f"#{i}") for i in o["drop"]]
    return links, plan, orphans, props


@admin_bp.route("/directory", methods=["GET", "POST"])
@superuser_required
def directory_page():
    """Managers and properties from the violations portal: line its properties
    up with this portal's, see who would be able to watch what, and approve.
    Nothing is granted until the administrator presses Approve; a grant made
    here is marked so it can be offered for removal if the directory changes."""
    from app.admin_auth import NO_PASSWORD, cf_email as _cf_email
    from app.models import DirectoryProperty
    cfg, now = current_app.config, datetime.utcnow()
    configured = bool(cfg.get("DIRECTORY_URL") and cfg.get("DIRECTORY_TOKEN"))
    action = request.form.get("action", "") if request.method == "POST" else ""

    if action == "sync":
        if not configured:
            flash("The violations portal's address and token are not set yet.", "error")
        else:
            ok, msg = directory_sync(now)
            flash(msg if ok else f"Could not read the violations portal: {msg}", "success" if ok else "error")
        return redirect(url_for("admin.directory_page"))

    if action == "links":
        valid = {p.id for p in Property.query.filter(Property.is_active.is_(True)).all()}
        changed = 0
        for r in DirectoryProperty.query.filter_by(present=True).all():
            key = f"link_{r.id}"
            if key not in request.form:
                continue
            raw = request.form.get(key, "")
            want = int(raw) if raw.isdigit() and int(raw) in valid else None
            if want != r.property_id:
                r.property_id, r.linked_by, r.linked_at = want, (_who() if want else None), (now if want else None)
                changed += 1
        db.session.commit()
        flash(f"Links saved: {changed} changed.", "success")
        return redirect(url_for("admin.directory_page"))

    if action == "approve":
        chosen = {e.strip().lower() for e in request.form.getlist("email")}
        _links, plan, _orphans, _props = _directory_view()
        made = granted = 0
        for p in plan:
            if p["email"] not in chosen or p["state"] not in ("new", "add"):
                continue
            user = db.session.get(User, p["user_id"]) if p["user_id"] else None
            if user is None:
                login = p["email"][:80]
                if login == cfg["ADMIN_USERNAME"] or User.query.filter_by(login=login).first() is not None:
                    import secrets
                    login = f"{p['email'].split('@')[0][:60]}-{secrets.token_hex(3)}"
                user = User(name=p["name"][:200], email=p["email"], login=login, password_hash=NO_PASSWORD)
                db.session.add(user)
                db.session.flush()
                made += 1
            for pid in p["add"]:
                db.session.add(Permission(user_id=user.id, scope_type="property", scope_id=pid, role="viewer",
                                          source="directory"))
                granted += 1
        db.session.commit()
        flash(f"Approved: {made} account{'s' if made != 1 else ''} created, {granted} propert{'y' if granted == 1 else 'ies'} shared.",
              "success")
        return redirect(url_for("admin.directory_page"))

    if action == "drop":
        removed = 0
        for item in request.form.getlist("drop"):
            uid, _, pid = item.partition(":")
            if uid.isdigit() and pid.isdigit():
                removed += Permission.query.filter_by(user_id=int(uid), scope_type="property", scope_id=int(pid),
                                                      source="directory").delete()
        db.session.commit()
        flash(f"Removed {removed} propert{'y' if removed == 1 else 'ies'} that the directory no longer backs.", "success")
        return redirect(url_for("admin.directory_page"))

    st = _directory_state()
    if configured and (st.attempted_at is None or now - st.attempted_at > timedelta(hours=6)):
        directory_sync(now)                              # a local call; keeps the page current without a button
        st = _directory_state()
    links, plan, orphans, props = _directory_view()
    from app import history
    return render_template(
        "directory.html", title="Directory", configured=configured, state=st, links=links, plan=plan, orphans=orphans,
        props=props, domains=cfg.get("DIRECTORY_EMAIL_DOMAINS") or (),
        fetched=history.local(st.fetched_at).strftime("%b %d, %I:%M %p").replace(" 0", " ") if st.fetched_at else None,
        email_signin=bool(cfg.get("CF_ACCESS_TEAM_DOMAIN") and cfg.get("CF_ACCESS_AUD")),
        # a check the administrator can read: does the token on THEIR OWN request verify?
        cf_token=bool(request.headers.get("Cf-Access-Jwt-Assertion")), cf_me=_cf_email(),
        counts={"todo": sum(1 for x in links if x["group"] == "todo"), "linked": sum(1 for x in links if x["group"] == "linked"),
                "rest": sum(1 for x in links if x["group"] == "rest"),
                "pending": sum(1 for p in plan if p["state"] in ("new", "add")),
                "blocked": sum(1 for p in plan if p["state"] == "blocked"),
                "drops": sum(len(p["drop"]) for p in plan) + sum(len(o["drop"]) for o in orphans)})


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
