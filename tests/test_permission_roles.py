"""
tests/test_permission_roles.py — the role ladder, without a database.

Roles are ordered: viewer < operator < admin. Two helpers depend on that
ordering, and both are used to decide whether a request is allowed to
mutate a record. An inverted comparison here is a privilege escalation,
not a cosmetic bug, so the ordering is pinned explicitly rather than left
implicit in a dict literal.
"""

import itertools

import pytest

from app.permissions import ROLE_RANK, _highest_role, _roles_at_least


def grant(role):
    """Permission rows are only read for `.role` by _highest_role()."""
    class _P:
        def __init__(self, r):
            self.role = r
    return _P(role)


class TheRoleLadder:

    def test_the_three_roles_and_their_order(self):
        assert ROLE_RANK == {"viewer": 1, "operator": 2, "admin": 3}

    def test_admin_outranks_operator_outranks_viewer(self):
        assert ROLE_RANK["admin"] > ROLE_RANK["operator"] > ROLE_RANK["viewer"]


class HighestRoleWins:
    """A user may hold grants at several levels at once — a Client-level
    viewer and a Building-level admin, say. The effective role must be the
    strongest, never the first one the query happened to return."""

    def test_no_grants_is_no_role(self):
        assert _highest_role([]) is None

    def test_single_grant(self):
        assert _highest_role([grant("operator")]) == "operator"

    def test_strongest_wins_regardless_of_order(self):
        for combo in itertools.permutations(["viewer", "operator", "admin"]):
            assert _highest_role([grant(r) for r in combo]) == "admin"

    def test_strongest_of_a_pair(self):
        assert _highest_role([grant("viewer"), grant("operator")]) == "operator"
        assert _highest_role([grant("operator"), grant("viewer")]) == "operator"

    def test_duplicates_do_not_promote(self):
        assert _highest_role([grant("viewer"), grant("viewer")]) == "viewer"


class RolesAtLeastIsInclusive:
    """Used to build the IN (...) clause for 'may create a child here'
    queries. Off-by-one here either locks admins out or lets viewers
    write."""

    def test_viewer_threshold_admits_everyone(self):
        assert set(_roles_at_least("viewer")) == {"viewer", "operator", "admin"}

    def test_operator_threshold_excludes_viewer(self):
        assert set(_roles_at_least("operator")) == {"operator", "admin"}

    def test_admin_threshold_is_admin_only(self):
        assert set(_roles_at_least("admin")) == {"admin"}

    @pytest.mark.parametrize("role", ["viewer", "operator", "admin"])
    def test_a_threshold_always_includes_itself(self, role):
        assert role in _roles_at_least(role), "thresholds are >=, not >"


class ControllersHaveNoScopeOfTheirOwn:
    """Permission.scope_type is constrained to client/property/building.
    A Controller inherits its Building's role — effective_role() dispatches
    Controller to effective_role_for_building(entity.building)."""

    def test_controller_is_not_a_grantable_scope(self):
        from app.models import Permission
        from sqlalchemy import CheckConstraint
        sql = ""
        for c in Permission.__table__.constraints:
            if isinstance(c, CheckConstraint) and c.name == "ck_permissions_scope_type":
                sql = str(c.sqltext)
        assert sql and "controller" not in sql
