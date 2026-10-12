"""
tests/test_billing_db.py — invoice generation against a real Postgres.

Skipped unless FLEET_TEST_DATABASE_URL is set; see tests/conftest.py for
why SQLite cannot stand in. Everything here depends on behaviour Postgres
provides and SQLite would silently fake: the (building_id, period_start)
unique constraint that makes generation re-runnable, and the PARTIAL
unique index that allows a building many contracts but only one current one.
"""

import datetime

import pytest
import sqlalchemy.exc as sa_exc

from app.billing import current_contract, generate_invoices
# Models imported as a module, not as names — see tests/test_billing_policy.py
# for why (pytest.ini sets python_classes = *).
import app.models as m
from tests.conftest import requires_postgres

H1 = (datetime.date(2026, 1, 1), datetime.date(2026, 6, 30))

pytestmark = requires_postgres


def make_building(db, name="B1", units=20, rate=1000, start=datetime.date(2025, 1, 1),
                  active=True, with_contract=True):
    client = m.Client(name=name + " client")
    db.session.add(client)
    db.session.flush()
    prop = m.Property(client_id=client.id, name=name + " property")
    db.session.add(prop)
    db.session.flush()
    bld = m.Building(property_id=prop.id, name=name, is_active=active)
    db.session.add(bld)
    db.session.flush()
    if with_contract:
        db.session.add(m.Contract(building_id=bld.id, unit_count=units,
                                rate_per_unit_cents=rate, start_date=start))
    db.session.commit()
    return bld


class GenerationProducesOneDraftPerEligibleBuilding:

    def test_a_building_with_a_current_contract_is_invoiced(self, session):
        from app.extensions import db
        bld = make_building(db, units=20)
        created = generate_invoices(*H1)
        assert len(created) == 1
        inv = created[0]
        assert inv.building_id == bld.id
        assert inv.status == "draft"
        assert inv.period_start == H1[0] and inv.period_end == H1[1]

    def test_amount_is_units_times_rate(self, session):
        from app.extensions import db
        make_building(db, units=20, rate=1000)
        inv = generate_invoices(*H1)[0]
        assert inv.unit_count == 20
        assert inv.rate_per_unit_cents == 1000
        assert inv.amount_cents == 20_000  # 20 units x $10.00

    def test_generation_never_issues_anything(self, session):
        """Generation produces drafts only — a human approves. issued_date
        and due_date must stay unset until then."""
        from app.extensions import db
        make_building(db)
        inv = generate_invoices(*H1)[0]
        assert inv.status == "draft"
        assert inv.issued_date is None and inv.due_date is None


class GenerationSkipsWhatItShould:

    def test_a_building_with_no_contract_is_skipped(self, session):
        from app.extensions import db
        make_building(db, with_contract=False)
        assert generate_invoices(*H1) == []

    def test_an_archived_building_is_skipped(self, session):
        from app.extensions import db
        make_building(db, active=False)
        assert generate_invoices(*H1) == []

    def test_a_contract_starting_after_the_period_is_skipped(self, session):
        """A contract signed in November must not produce an invoice for
        the January-June period that already closed."""
        from app.extensions import db
        make_building(db, start=datetime.date(2026, 11, 1))
        assert generate_invoices(*H1) == []

    def test_a_contract_starting_inside_the_period_is_billed_in_full(self, session):
        """No proration is settled policy — a mid-period start still bills
        the whole half-year."""
        from app.extensions import db
        make_building(db, units=10, start=datetime.date(2026, 3, 15))
        inv = generate_invoices(*H1)[0]
        assert inv.amount_cents == 10 * 1000


class GenerationIsIdempotent:
    """The daily scheduler re-runs generation every day inside the 7-day
    window, and a human can click Generate Now at the same moment. Neither
    may produce a second invoice."""

    def test_running_twice_creates_nothing_the_second_time(self, session):
        from app.extensions import db
        make_building(db)
        assert len(generate_invoices(*H1)) == 1
        assert generate_invoices(*H1) == []
        assert m.Invoice.query.count() == 1

    def test_a_building_added_after_the_first_run_is_picked_up(self, session):
        """Why there is no outer 'has this period already run' gate: a
        contract created on day 3 of the window must still be invoiced
        when generation runs again on day 4."""
        from app.extensions import db
        make_building(db, name="first")
        generate_invoices(*H1)
        make_building(db, name="second")
        created = generate_invoices(*H1)
        assert len(created) == 1
        assert m.Invoice.query.count() == 2

    def test_the_unique_constraint_is_the_real_backstop(self, session):
        from app.extensions import db
        bld = make_building(db)
        generate_invoices(*H1)
        contract = current_contract(bld)
        dupe = m.Invoice(building_id=bld.id, contract_id=contract.id,
                       period_start=H1[0], period_end=H1[1], unit_count=1,
                       rate_per_unit_cents=1000, amount_cents=1000, status="draft")
        db.session.add(dupe)
        with pytest.raises(sa_exc.IntegrityError):
            db.session.commit()
        db.session.rollback()


class AnInvoiceIsASnapshot:

    def test_changing_the_contract_later_does_not_alter_an_existing_invoice(self, session):
        from app.extensions import db
        bld = make_building(db, units=20)
        inv = generate_invoices(*H1)[0]
        original = inv.amount_cents

        contract = current_contract(bld)
        contract.end_date = datetime.date(2026, 6, 30)
        db.session.add(m.Contract(building_id=bld.id, unit_count=50,
                                rate_per_unit_cents=1200,
                                start_date=datetime.date(2026, 7, 1)))
        db.session.commit()

        refreshed = m.Invoice.query.get(inv.id)
        assert refreshed.amount_cents == original == 20_000
        assert refreshed.unit_count == 20


class OnlyOneCurrentContractPerBuilding:
    """Enforced by the partial unique index, not by application logic —
    which is exactly why it needs a Postgres test."""

    def test_a_second_open_contract_is_rejected(self, session):
        from app.extensions import db
        bld = make_building(db)
        db.session.add(m.Contract(building_id=bld.id, unit_count=5,
                                start_date=datetime.date(2026, 1, 1)))
        with pytest.raises(sa_exc.IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_closing_the_old_one_first_is_allowed(self, session):
        from app.extensions import db
        bld = make_building(db, units=20)
        current_contract(bld).end_date = datetime.date(2026, 6, 30)
        db.session.commit()
        db.session.add(m.Contract(building_id=bld.id, unit_count=50,
                                start_date=datetime.date(2026, 7, 1)))
        db.session.commit()
        assert m.Contract.query.filter_by(building_id=bld.id).count() == 2
        assert current_contract(bld).unit_count == 50
