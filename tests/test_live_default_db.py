"""
tests/test_live_default_db.py — how the Live page opens (Postgres tier)

Live opens as the table, sorted by address, with each run of digits read as a
number: 10-12 Bedell St comes before 101-109 Lowell St, which a plain text sort
gets the other way round. The cards and "Problems first" are one click away.
"""
import re

import pytest

from tests.conftest import requires_postgres

pytestmark = requires_postgres

# in the order the page must list them
ADDRESSES = ["1-8 Meehan Lane", "9-14 Meehan Lane", "10-12 Bedell St", "11 Bedell St", "14 Rockvale Circ",
             "14-16 Bedell St", "101-109 Lowell st", "1012 Garrison Ave", "Ind-1 85 Taylor N", "RP 10AH"]


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as s:
        s["superuser"] = True
    return c


@pytest.fixture
def boxes(session):
    import app.models as m
    for i, name in enumerate(reversed(ADDRESSES)):
        session.add(m.LegacyBox(name=name, host="192.0.2.7", port=8100 + i, identity="unreachable"))
    session.commit()


def _listed(page):
    """The building names in the order the table shows them."""
    return re.findall(r'<td class="pin2"><a href="[^"]*" title="([^"]*)"', page)


def test_the_address_key_reads_numbers_as_numbers():
    from app.routes import _address_key
    assert sorted(reversed(ADDRESSES), key=_address_key) == ADDRESSES
    assert sorted(["b", None, "", "a"], key=_address_key) == [None, "", "a", "b"]   # an empty name cannot break the page


def test_live_opens_as_the_table_sorted_by_address(boxes, client):
    page = client.get("/live").data.decode()
    assert '<table class="lv-table">' in page and '<div class="lv-grid">' not in page
    assert _listed(page) == ADDRESSES
    assert '<th class="pin2 s-asc">Building</th>' in page                 # the arrow on the heading it is sorted by
    assert re.search(r'<a class="f lv-sortpill on"[^>]*>Address</a>', page)
    # the links on the page keep the table and offer the cards
    assert re.search(r'href="[^"]*view=cards[^"]*">Cards</a>', page)
    # an unknown sort or view falls back to the same thing
    assert _listed(client.get("/live?sort=nonsense&view=nonsense").data.decode()) == ADDRESSES


def test_the_cards_and_the_other_sorts_are_still_there(boxes, client):
    cards = client.get("/live?view=cards").data.decode()
    assert '<table class="lv-table">' not in cards and '<div class="lv-grid">' in cards
    assert re.search(r'href="[^"]*view=table[^"]*">Table</a>', cards)
    probs = client.get("/live?sort=problems").data.decode()
    assert re.search(r'<a class="f lv-sortpill on"[^>]*>Problems first</a>', probs)
    assert '<th class="pin2">Building</th>' in probs                      # no arrow: not sorted by this column
    assert _listed(probs) == ADDRESSES                                     # all equally dark, so the address breaks the tie
