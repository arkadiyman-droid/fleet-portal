"""
tests/test_billing_policy.py — the settled policies, as executable assertions.

These are the decisions from the architecture plan that are easy to erode
by accident, because nothing visibly breaks when they do. Each test below
guards one of them at the schema or source level, where it cannot be
quietly reverted without a failure.

The source-reading tests follow the same pattern the EMS repo uses for
deliberately-absent features: a policy of "we do NOT do X" cannot be
tested by exercising X, only by asserting the code has no way to.
"""

import io
import os

# Imported as MODULES, not as names. `python_classes = *` in pytest.ini
# means pytest tries to collect any class in a test module's namespace, so
# `from app.models import Invoice` would have it warning about every model
# and constraint type it finds. Keeping them behind `m.` / `sa.` keeps the
# namespace free of classes.
import sqlalchemy as sa

import app.models as m

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")


def source(name):
    with io.open(os.path.join(APP_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def check_constraint_sql(model, name):
    for c in model.__table__.constraints:
        if isinstance(c, sa.CheckConstraint) and c.name == name:
            return str(c.sqltext)
    raise AssertionError("no CheckConstraint named %r on %s" % (name, model.__tablename__))


class NonPaymentNeverRestrictsAccess:
    """The strongest policy in the plan: *"Non-payment — never restricts
    access. Escalating polite reminders only. The system runs live heating
    equipment — nobody gets locked out mid-winter over an invoice."*

    This is the one a future "just gate the dashboard until they pay"
    change would quietly break, so it is asserted structurally: the
    authorization layer must have no way to see billing at all.
    """

    def test_the_source_files_are_actually_being_read(self):
        """Guard against the negative assertions below passing vacuously.
        If APP_DIR drifted and source() returned something empty, "Invoice
        not in src" would be trivially true and this whole class would go
        green while guarding nothing."""
        assert "ROLE_RANK" in source("permissions.py")
        assert "def " in source("admin_auth.py")

    def test_permissions_layer_cannot_see_invoices_or_payments(self):
        src = source("permissions.py")
        for forbidden in ("Invoice", "Payment", "Contract"):
            assert forbidden not in src, (
                "permissions.py references %s — authorization must not be able to "
                "consider billing state" % forbidden
            )

    def test_login_cannot_see_invoices_or_payments(self):
        src = source("admin_auth.py")
        for forbidden in ("Invoice", "Payment", "Contract"):
            assert forbidden not in src, (
                "admin_auth.py references %s — login must not depend on billing "
                "state" % forbidden
            )

    def test_no_suspended_or_locked_state_exists_on_a_client(self):
        """There is deliberately no account-suspension flag to switch on.
        `is_active` is the archive/soft-delete mechanism shared by every
        entity, not a billing lever."""
        columns = set(m.Client.__table__.columns.keys())
        for forbidden in ("suspended", "locked", "delinquent", "access_blocked"):
            assert forbidden not in columns


class InvoiceStatusesAreAClosedSet:
    """draft -> pending -> paid, plus void. 'Overdue' is deliberately NOT
    stored — it is derived at render time as pending && due_date < today,
    so it can never go stale against the clock."""

    def test_exactly_four_statuses(self):
        sql = check_constraint_sql(m.Invoice, "ck_invoices_status")
        for status in ("draft", "pending", "paid", "void"):
            assert status in sql
        assert "overdue" not in sql, "overdue must stay derived, never stored"

    def test_amount_cannot_be_negative(self):
        assert "amount_cents >= 0" in check_constraint_sql(m.Invoice, "ck_invoices_amount_nonneg")


class OneInvoicePerBuildingPerPeriod:
    """*"One Invoice per Building per period — never consolidated per
    Client."* The unique constraint is also what makes generate_invoices()
    safely re-runnable, which the daily scheduler depends on."""

    def test_unique_constraint_exists_on_building_and_period(self):
        cols = None
        for c in m.Invoice.__table__.constraints:
            if isinstance(c, sa.UniqueConstraint) and c.name == "uq_invoices_building_period":
                cols = [col.name for col in c.columns]
        assert cols == ["building_id", "period_start"]

    def test_it_is_not_scoped_to_the_client(self):
        """Consolidating per client would mean a client_id in this key."""
        for c in m.Invoice.__table__.constraints:
            if isinstance(c, sa.UniqueConstraint) and c.name == "uq_invoices_building_period":
                assert "client_id" not in [col.name for col in c.columns]


class ContractsAreAppendOnly:

    def test_default_rate_is_ten_dollars_per_unit(self):
        assert m.Contract.__table__.c.rate_per_unit_cents.default.arg == 1000

    def test_rate_is_per_contract_not_a_global_constant(self):
        """A future rate change must not rewrite history on old contracts,
        so the rate is snapshotted per row."""
        assert "rate_per_unit_cents" in m.Contract.__table__.columns

    def test_invoice_snapshots_units_and_rate(self):
        """Same reasoning one level down: editing a contract must never
        retroactively change an invoice that was already generated."""
        for col in ("unit_count", "rate_per_unit_cents", "amount_cents"):
            assert col in m.Invoice.__table__.columns

    def test_at_most_one_current_contract_is_a_partial_index(self):
        """The 'current' contract is end_date IS NULL. The uniqueness must
        apply ONLY to those rows — a plain unique index on building_id
        would forbid a second contract ever existing."""
        idx = [i for i in m.Contract.__table__.indexes if i.name == "uq_contracts_building_current"]
        assert len(idx) == 1
        index = idx[0]
        assert index.unique is True
        where = index.dialect_options["postgresql"].get("where")
        assert where is not None and "end_date IS NULL" in str(where)


class PaymentsSettleInFull:
    """*"Deliberately no partial-payment reconciliation: one Payment
    settles an Invoice in full."*"""

    def test_payment_methods_are_a_closed_set(self):
        sql = check_constraint_sql(m.Payment, "ck_payments_method")
        for method in ("check", "ach", "wire", "card", "other"):
            assert method in sql

    def test_amount_must_be_positive(self):
        assert "amount_cents > 0" in check_constraint_sql(m.Payment, "ck_payments_amount_positive")

    def test_stripe_intent_is_unique_so_a_retried_webhook_cannot_double_record(self):
        """Stripe explicitly retries deliveries. Without this, one payment
        becomes two and the invoice history is wrong."""
        names = [c.name for c in m.Payment.__table__.constraints if isinstance(c, sa.UniqueConstraint)]
        assert "uq_payments_stripe_payment_intent_id" in names


class PermissionScopesAreAClosedSet:

    def test_three_scope_levels(self):
        sql = check_constraint_sql(m.Permission, "ck_permissions_scope_type")
        for scope in ("client", "property", "building"):
            assert scope in sql

    def test_three_roles(self):
        sql = check_constraint_sql(m.Permission, "ck_permissions_role")
        for role in ("viewer", "operator", "admin"):
            assert role in sql
