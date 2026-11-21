"""
app/permissions.py — Phase 3 authorization.

Three distinct query shapes, kept separate on purpose — collapsing them
would silently leak the wrong direction of implication:

  - effective_role_for_*(user_id, entity): walks UP from one already-loaded
    entity to find the highest applicable role. Used by require_role() (the
    route-level IDOR guard) and for per-row Edit/Archive button gating.
  - visible_*_query(user_id): every entity the user may SEE in a list view
    — a direct grant, or an ancestor's grant (bleeds down), or being an
    ancestor of a descendant's grant (bleeds up, so a Building-scoped user
    can still navigate Clients -> Properties -> their Building). Viewer
    semantics only.
  - creatable_*_query(user_id, min_role): parent entities the user may
    create a CHILD under — a direct or ancestor grant at >= min_role.
    Never derived from a descendant or sibling grant: a Building-level
    operator grant lets you create a Controller under that Building, but
    must never let you create a sibling Property under its parent Client.

Role hierarchy for this phase's scope (record CRUD + Fleet Status viewing
— not control actions, those are Phase 6): viewer = read; operator =
viewer + create/edit; admin = operator + archive/restore. A Client-level
grant implies that role on every Property/Building/Controller beneath it.
"""

from functools import wraps

from flask import abort, g, session
from sqlalchemy import and_, or_

from app.extensions import db
from app.models import BillingControl, Building, Client, Controller, FanControl, Permission, Property, SettingControl, User

ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}


def _highest_role(rows):
    roles = [r.role for r in rows]
    return max(roles, key=lambda r: ROLE_RANK[r]) if roles else None


def _roles_at_least(min_role):
    threshold = ROLE_RANK[min_role]
    return [r for r, rank in ROLE_RANK.items() if rank >= threshold]


# ─── identity (wired into admin_auth.py's before_request hook) ───────────

def load_identity() -> bool:
    """Populates g.user (User|None) and g.is_superuser for this request.
    Returns False if the session references a deleted/deactivated DB user
    — the caller (admin_auth._require_login) must clear the session and
    redirect to login in that case."""
    g.viewing_as = False
    if session.get("superuser"):
        # "See as": the administrator looking at the portal as one of the
        # users (admin_auth.view_as). For this request they ARE that user —
        # every page applies that person's own access rule — and
        # admin_auth refuses anything that would change something.
        as_id = session.get("view_as")
        if as_id is not None:
            seen = User.query.get(as_id)
            if seen is not None and seen.is_active:
                g.is_superuser, g.user, g.viewing_as = False, seen, True
                return True
            session.pop("view_as", None)             # that account is gone: back to the administrator
        g.is_superuser, g.user = True, None
        return True
    user_id = session.get("user_id")
    user = User.query.get(user_id) if user_id is not None else None
    if user is None or not user.is_active:
        g.is_superuser, g.user = False, None
        return False
    g.is_superuser, g.user = False, user
    return True


def current_user():
    return getattr(g, "user", None)


def is_superuser() -> bool:
    return getattr(g, "is_superuser", False)


def superuser_required(view):
    """Decorator for the /users* management routes — that whole feature is
    Superuser-only in this phase, deliberately not delegated."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not is_superuser():
            abort(403)
        return view(*args, **kwargs)
    return wrapped


# ─── effective_role_for_*: walk UP from one loaded entity ────────────────

def effective_role_for_client(user_id, client):
    rows = Permission.query.filter_by(user_id=user_id, scope_type="client", scope_id=client.id).all()
    return _highest_role(rows)


def effective_role_for_property(user_id, prop):
    rows = Permission.query.filter(
        Permission.user_id == user_id,
        or_(
            and_(Permission.scope_type == "property", Permission.scope_id == prop.id),
            and_(Permission.scope_type == "client", Permission.scope_id == prop.client_id),
        ),
    ).all()
    return _highest_role(rows)


def effective_role_for_building(user_id, building):
    """Checks Building-level, Property-level (building.property_id), and
    Client-level (building.property.client_id) grants in one query."""
    rows = Permission.query.filter(
        Permission.user_id == user_id,
        or_(
            and_(Permission.scope_type == "building", Permission.scope_id == building.id),
            and_(Permission.scope_type == "property", Permission.scope_id == building.property_id),
            and_(Permission.scope_type == "client", Permission.scope_id == building.property.client_id),
        ),
    ).all()
    return _highest_role(rows)


def effective_role(entity):
    """Dispatches by entity type. Controller inherits its Building's role —
    Permission never has scope_type='controller'."""
    user = current_user()
    if user is None:
        return None
    if isinstance(entity, Client):
        return effective_role_for_client(user.id, entity)
    if isinstance(entity, Property):
        return effective_role_for_property(user.id, entity)
    if isinstance(entity, Building):
        return effective_role_for_building(user.id, entity)
    if isinstance(entity, Controller):
        return effective_role_for_building(user.id, entity.building)
    raise TypeError(f"effective_role: unsupported entity type {type(entity)}")


def require_role(min_role, entity) -> None:
    """Call immediately after loading `entity` via get_or_404, before any
    mutation or template render. Superuser always passes (no query). On
    failure: abort(403) — this is an IDOR guard on an already-identified
    target record, not a soft validation error."""
    if is_superuser():
        return
    role = effective_role(entity)
    if role is None or ROLE_RANK[role] < ROLE_RANK[min_role]:
        abort(403)


def user_has_min_role_anywhere(min_role) -> bool:
    """Used to decide whether to show a '+ New' button at all when no
    parent-id filter is active in list_property/list_building/list_controller."""
    user = current_user()
    if user is None:
        return False
    threshold = ROLE_RANK[min_role]
    return any(ROLE_RANK[p.role] >= threshold for p in Permission.query.filter_by(user_id=user.id).all())


# ─── visible_*_query: list-view visibility (viewer, with ancestor bleed-up) ─

def visible_client_query(user_id):
    direct = db.session.query(Permission.scope_id).filter_by(user_id=user_id, scope_type="client")
    via_property = db.session.query(Property.client_id).join(
        Permission, and_(Permission.scope_type == "property", Permission.scope_id == Property.id)
    ).filter(Permission.user_id == user_id)
    via_building = db.session.query(Property.client_id).select_from(Permission).join(
        Building, and_(Permission.scope_type == "building", Permission.scope_id == Building.id)
    ).join(Property, Property.id == Building.property_id).filter(Permission.user_id == user_id)
    return Client.query.filter(Client.id.in_(direct.union(via_property, via_building)))


def visible_property_query(user_id):
    direct = db.session.query(Permission.scope_id).filter_by(user_id=user_id, scope_type="property")
    via_client = db.session.query(Property.id).join(
        Permission, and_(Permission.scope_type == "client", Permission.scope_id == Property.client_id)
    ).filter(Permission.user_id == user_id)
    via_building = db.session.query(Building.property_id).join(
        Permission, and_(Permission.scope_type == "building", Permission.scope_id == Building.id)
    ).filter(Permission.user_id == user_id)
    return Property.query.filter(Property.id.in_(direct.union(via_client, via_building)))


def visible_building_query(user_id):
    direct = db.session.query(Permission.scope_id).filter_by(user_id=user_id, scope_type="building")
    via_property = db.session.query(Building.id).join(
        Permission, and_(Permission.scope_type == "property", Permission.scope_id == Building.property_id)
    ).filter(Permission.user_id == user_id)
    via_client = db.session.query(Building.id).select_from(Permission).join(
        Property, and_(Permission.scope_type == "client", Permission.scope_id == Property.client_id)
    ).join(Building, Building.property_id == Property.id).filter(Permission.user_id == user_id)
    return Building.query.filter(Building.id.in_(direct.union(via_property, via_client)))


def visible_controller_query(user_id):
    visible_building_ids = visible_building_query(user_id).with_entities(Building.id)
    return Controller.query.filter(Controller.building_id.in_(visible_building_ids))


# ─── creatable_*_query: parent-dropdown eligibility (operator+/admin+, no bleed-up) ─

def creatable_property_client_query(user_id, min_role="operator"):
    roles = _roles_at_least(min_role)
    ids = db.session.query(Permission.scope_id).filter(
        Permission.user_id == user_id, Permission.scope_type == "client", Permission.role.in_(roles)
    )
    return Client.query.filter(Client.id.in_(ids))


def creatable_building_property_query(user_id, min_role="operator"):
    roles = _roles_at_least(min_role)
    direct = db.session.query(Permission.scope_id).filter(
        Permission.user_id == user_id, Permission.scope_type == "property", Permission.role.in_(roles)
    )
    via_client = db.session.query(Property.id).join(
        Permission, and_(Permission.scope_type == "client", Permission.scope_id == Property.client_id)
    ).filter(Permission.user_id == user_id, Permission.role.in_(roles))
    return Property.query.filter(Property.id.in_(direct.union(via_client)))


def creatable_controller_building_query(user_id, min_role="operator"):
    roles = _roles_at_least(min_role)
    direct = db.session.query(Permission.scope_id).filter(
        Permission.user_id == user_id, Permission.scope_type == "building", Permission.role.in_(roles)
    )
    via_property = db.session.query(Building.id).join(
        Permission, and_(Permission.scope_type == "property", Permission.scope_id == Building.property_id)
    ).filter(Permission.user_id == user_id, Permission.role.in_(roles))
    via_client = db.session.query(Building.id).select_from(Permission).join(
        Property, and_(Permission.scope_type == "client", Permission.scope_id == Property.client_id)
    ).join(Building, Building.property_id == Property.id).filter(
        Permission.user_id == user_id, Permission.role.in_(roles)
    )
    return Building.query.filter(Building.id.in_(direct.union(via_property, via_client)))


# ─── watching: the read-only monitoring pages ────────────────────────────
#
# The pages that show what a building is doing — My properties, a 2010 box's
# page, a Bloom building's page, Fans, Alerts — are open to anyone holding a
# grant (any role) on the building, its property or its client. Everything
# that changes something stays with the administrator or an operator.

def can_view_building(building) -> bool:
    """May this request read what `building` is doing? The administrator
    always; otherwise a grant on it or above it. A reading that is not filed
    under any building (None) belongs to the administrator alone."""
    if is_superuser():
        return True
    if building is None or current_user() is None:
        return False
    return effective_role(building) is not None


def visible_building_ids():
    """None for the administrator (meaning every building); otherwise the set
    of building ids this person may see, possibly empty."""
    if is_superuser():
        return None
    user = current_user()
    if user is None:
        return set()
    return {bid for (bid,) in visible_building_query(user.id).with_entities(Building.id).all()}


def is_view_only() -> bool:
    """A signed-in person (not the administrator) with no grant above
    'viewer' anywhere — they are shown the short menu."""
    return current_user() is not None and not user_has_min_role_anywhere("operator")


# ─── fan control: who may run a property's roof fans ─────────────────────
#
# The one thing besides watching that the administrator can hand to a person,
# property by property (models.FanControl). The Fans pages file buildings under
# a property by its NAME, so that is what these answer in.

def fan_control_names():
    """None for the administrator (meaning every property); otherwise the names
    of the properties whose fans this person may run, possibly empty. A tick
    counts only while the person can see the whole property — a grant on the
    property itself or on its owner."""
    if is_superuser():
        return None
    user = current_user()
    if user is None:
        return set()
    names = set()
    for fc in FanControl.query.filter_by(user_id=user.id).all():
        prop = Property.query.get(fc.property_id)
        if prop is not None and prop.is_active and effective_role_for_property(user.id, prop) is not None:
            names.add(prop.name)
    return names


# ─── settings control: who may change a property's heating settings ──────
#
# The second thing the administrator can hand to a person, property by
# property (models.SettingControl): changing the settings of that property's
# buildings, under their own name.

def can_change_settings(building) -> bool:
    """May this request change the settings of this building? The
    administrator always; a person only where they hold the settings tick on
    the building's property AND can still see that property."""
    if is_superuser():
        return True
    user = current_user()
    prop = building.property if building is not None else None
    if user is None or prop is None or not prop.is_active or not building.is_active:
        return False
    if effective_role_for_property(user.id, prop) is None:
        return False
    return SettingControl.query.filter_by(user_id=user.id, property_id=prop.id).first() is not None


# ─── billing control: who may manage an owner's subscription ─────────────

def billing_client_ids():
    """None for the administrator (every owner); otherwise the ids of the
    owners whose subscription this person may manage, possibly empty."""
    if is_superuser():
        return None
    user = current_user()
    if user is None:
        return set()
    return {bc.client_id for bc in BillingControl.query.filter_by(user_id=user.id).all()}


def can_manage_billing(client) -> bool:
    ids = billing_client_ids()
    return client is not None and (ids is None or (client.is_active and client.id in ids))


def can_run_fans(prop_name) -> bool:
    """May this request use the 'Schedules & switching' page of the property
    with this name?"""
    names = fan_control_names()
    return names is None or prop_name in names
