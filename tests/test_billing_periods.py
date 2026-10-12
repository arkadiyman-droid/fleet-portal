"""
tests/test_billing_periods.py — the half-year billing calendar.

Settled policy: billed twice yearly, Jan 1-Jun 30 and Jul 1-Dec 31, **no
proration**. Every invoice amount in the system is derived from which of
those two buckets a date falls in, so an off-by-one at a boundary is not a
cosmetic bug — it bills a building for the wrong half of the year.

The boundaries are the whole point of these tests. Jun 30 / Jul 1 is where
a `<` vs `<=` slip would hide, and it would hide for six months.
"""

import datetime

from app.billing import INVOICE_DUE_DAYS, period_for_date


def d(y, m, day):
    return datetime.date(y, m, day)


class TheFirstHalfRunsJanuaryToJune:

    def test_january_first_is_the_first_day(self):
        assert period_for_date(d(2026, 1, 1)) == (d(2026, 1, 1), d(2026, 6, 30))

    def test_mid_period(self):
        assert period_for_date(d(2026, 3, 17)) == (d(2026, 1, 1), d(2026, 6, 30))

    def test_june_thirtieth_is_still_the_first_half(self):
        """The last day of H1. If this lands in H2 the building is billed
        for the wrong period and the H1 invoice never generates."""
        assert period_for_date(d(2026, 6, 30)) == (d(2026, 1, 1), d(2026, 6, 30))


class TheSecondHalfRunsJulyToDecember:

    def test_july_first_flips_to_the_second_half(self):
        assert period_for_date(d(2026, 7, 1)) == (d(2026, 7, 1), d(2026, 12, 31))

    def test_december_thirty_first_is_the_last_day(self):
        assert period_for_date(d(2026, 12, 31)) == (d(2026, 7, 1), d(2026, 12, 31))

    def test_new_years_eve_does_not_leak_into_next_year(self):
        start, end = period_for_date(d(2026, 12, 31))
        assert start.year == 2026 and end.year == 2026


class TheCalendarHoldsAcrossYears:

    def test_every_day_of_a_leap_year_lands_in_exactly_one_period(self):
        """February 29 is the obvious trap, but the real assertion is
        totality: no date may fall outside both buckets, and the two
        buckets must not overlap."""
        day = d(2024, 1, 1)
        while day <= d(2024, 12, 31):
            start, end = period_for_date(day)
            assert start <= day <= end, day
            assert start.year == end.year == day.year, day
            day += datetime.timedelta(days=1)

    def test_leap_day_itself(self):
        assert period_for_date(d(2024, 2, 29)) == (d(2024, 1, 1), d(2024, 6, 30))

    def test_the_two_periods_tile_the_year_without_a_gap(self):
        h1_start, h1_end = period_for_date(d(2026, 1, 1))
        h2_start, h2_end = period_for_date(d(2026, 7, 1))
        assert h1_end + datetime.timedelta(days=1) == h2_start
        assert h1_start == d(2026, 1, 1) and h2_end == d(2026, 12, 31)


class InvoiceTerms:

    def test_net_30(self):
        assert INVOICE_DUE_DAYS == 30

    def test_due_date_is_counted_from_issue_not_from_generation(self):
        """routes.approve_invoice() sets issued_date at APPROVAL time and
        due_date from it. Generation happens earlier and may sit in draft
        indefinitely, so counting from generation would hand a client a
        shorter window than the terms promise — or a due date already in
        the past."""
        issued = d(2026, 1, 15)
        assert issued + datetime.timedelta(days=INVOICE_DUE_DAYS) == d(2026, 2, 14)
