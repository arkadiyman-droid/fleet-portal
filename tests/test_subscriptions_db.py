"""
tests/test_subscriptions_db.py — owners pay by the apartment unit, monthly or
quarterly, with the payment method on file (Postgres tier, Stripe faked)

The fake below stands in for Stripe at exactly the points app/subscriptions.py
calls it (the api_* functions): prices, Checkout, subscriptions, invoices and
the Customer Portal.
"""
import copy
import datetime as dt

import pytest

from app import subscriptions as subs
from tests.conftest import requires_postgres
from tests.test_viewer_access_db import _as

pytestmark = requires_postgres
BASE = "https://fleet.test"


class FakeStripe:
    __test__ = False

    def __init__(self):
        self.subs, self.sessions, self.invoices = {}, {}, {}
        self.checkouts, self.modifies, self.portals = [], [], []

    @staticmethod
    def price(price_id):
        _, interval, cents = price_id.split("_")
        return {"id": price_id, "unit_amount": int(cents),
                "recurring": {"interval": "month", "interval_count": 3 if interval == "quarter" else 1}}

    def customer(self, client):
        from app.extensions import db
        if not client.billing_customer_id:
            client.billing_customer_id = f"cus_{client.id}"
            db.session.commit()
        return client.billing_customer_id

    def price_id(self, interval, cents):
        return f"price_{interval}_{cents}"

    def checkout(self, customer_id, price_id, units, client_id, success_url, cancel_url):
        sid = f"cs_{len(self.sessions) + 1}"
        self.sessions[sid] = {"id": sid, "mode": "subscription", "customer": customer_id, "subscription": None,
                              "client_reference_id": f"owner:{client_id}", "price": price_id, "units": units}
        self.checkouts.append({"customer": customer_id, "price": price_id, "units": units, "client_id": client_id,
                               "success_url": success_url, "cancel_url": cancel_url})
        return f"https://checkout.stripe.test/{sid}"

    def pay(self, sid, status="active"):
        """The person completes Checkout: Stripe makes the subscription."""
        s = self.sessions[sid]
        sub_id = f"sub_{len(self.subs) + 1}"
        self.subs[sub_id] = {"id": sub_id, "status": status, "cancel_at_period_end": False,
                             "metadata": {"client_id": s["client_reference_id"].split(":")[1]},
                             "current_period_end": 1800000000,
                             "items": {"data": [{"id": f"si_{sub_id}", "quantity": s["units"], "price": self.price(s["price"])}]}}
        s["subscription"] = sub_id
        return sub_id

    def retrieve_checkout(self, sid):
        return copy.deepcopy(self.sessions[sid])

    def retrieve_subscription(self, sub_id):
        return copy.deepcopy(self.subs[sub_id])

    def modify(self, sub_id, item_id, proration, price_id=None, units=None):
        self.modifies.append({"sub": sub_id, "item": item_id, "proration": proration, "price": price_id, "units": units})
        item = self.subs[sub_id]["items"]["data"][0]
        assert item["id"] == item_id
        if price_id:
            item["price"] = self.price(price_id)
        if units is not None:
            item["quantity"] = units
        return copy.deepcopy(self.subs[sub_id])

    def invoice(self, sub_id, paid=True, amount=25000, iid=None):
        iid = iid or f"in_{len(self.invoices) + 1}"
        self.invoices[iid] = {"id": iid, "subscription": sub_id, "number": f"ECHO-{len(self.invoices) + 1:04d}",
                              "status": "paid" if paid else "open", "paid": paid, "attempted": True,
                              "amount_paid": amount if paid else 0, "amount_due": amount, "created": 1790000000,
                              "hosted_invoice_url": f"https://invoice.stripe.test/{iid}",
                              "status_transitions": {"paid_at": 1790000100 if paid else None}}
        return iid

    def retrieve_invoice(self, iid):
        return copy.deepcopy(self.invoices[iid])

    def portal(self, customer_id, return_url):
        self.portals.append((customer_id, return_url))
        return f"https://billing.stripe.test/p/{customer_id}"


@pytest.fixture
def fake(app, monkeypatch):
    f = FakeStripe()
    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    app.config["PORTAL_PUBLIC_URL"] = BASE
    for name, fn in (("api_customer", f.customer), ("api_price_id", f.price_id), ("api_checkout", f.checkout),
                     ("api_retrieve_checkout", f.retrieve_checkout), ("api_retrieve_subscription", f.retrieve_subscription),
                     ("api_modify_subscription", f.modify), ("api_retrieve_invoice", f.retrieve_invoice),
                     ("api_portal", f.portal)):
        monkeypatch.setattr(subs, name, fn)
    return f


@pytest.fixture
def owner(app, session):
    """Acme: two buildings with 40 and 60 units and one without a unit count;
    Bea may manage its billing; Pat only sees one of its properties."""
    import app.models as m
    from werkzeug.security import generate_password_hash
    app.config["WTF_CSRF_ENABLED"] = False
    c = m.Client(name="Acme Housing", contact_email="ap@acme.test")
    session.add(c)
    session.flush()
    p = m.Property(client_id=c.id, name="Acme Gardens")
    session.add(p)
    session.flush()
    b1, b2, b3 = (m.Building(property_id=p.id, name=n) for n in ("1 Garden St", "2 Garden St", "3 Garden St"))
    session.add_all([b1, b2, b3])
    session.flush()
    session.add_all([m.Contract(building_id=b1.id, unit_count=40, start_date=dt.date(2026, 1, 1)),
                     m.Contract(building_id=b2.id, unit_count=60, start_date=dt.date(2026, 1, 1))])
    bea = m.User(name="Bea Billing", email="bea@acme.test", login="bea", password_hash=generate_password_hash("x" * 12))
    pat = m.User(name="Pat Viewer", email="pat@acme.test", login="pat", password_hash=generate_password_hash("x" * 12))
    session.add_all([bea, pat])
    session.flush()
    session.add(m.BillingControl(user_id=bea.id, client_id=c.id))
    session.add(m.Permission(user_id=pat.id, scope_type="property", scope_id=p.id, role="viewer"))
    session.commit()
    return {"client": c.id, "b1": b1.id, "b2": b2.id, "b3": b3.id, "bea": bea.id, "pat": pat.id, "property": p.id}


def _url(o, tail=""):
    return f"/billing/owner/{o['client']}{tail}"


def _subscribe(app, fake, o, interval="quarter"):
    bea = _as(app, user_id=o["bea"])
    r = bea.post(_url(o, "/subscribe"), data={"interval": interval})
    sid = r.headers["Location"].rsplit("/", 1)[-1]
    fake.pay(sid)
    bea.get(_url(o) + f"?session_id={sid}")
    return bea, sid


# ── prices and units ─────────────────────────────────────────────────────────

def test_the_price_is_units_times_the_rate_for_the_interval(app, owner, session):
    import app.models as m
    assert subs.current_rates() == {"month": 85, "quarter": 250}
    client = session.get(m.Client, owner["client"])
    assert subs.owner_units(client) == 100
    assert [(b.name, u) for b, u in subs.owner_buildings(client)] == [("1 Garden St", 40), ("2 Garden St", 60), ("3 Garden St", None)]
    session.add(m.SubscriptionRate(by="x", monthly_cents=90, quarterly_cents=260))
    session.commit()
    assert subs.current_rates() == {"month": 90, "quarter": 260}


def test_the_owners_page_shows_buildings_units_and_both_prices(app, owner, fake):
    page = _as(app, user_id=owner["bea"]).get(_url(owner)).get_data(as_text=True)
    assert "Billing" in page and "Acme Housing" in page
    assert "$250.00" in page and "$85.00" in page                 # 100 units: quarterly and monthly
    assert "$1,000.00 a year" in page and "$1,020.00 a year" in page
    assert "3 Garden St" in page and "not set yet" in page
    assert "Apple Pay" in page and "PayPal, where offered" in page
    assert "Subscribe and add a payment method" in page and fake.checkouts == []


# ── subscribing ──────────────────────────────────────────────────────────────

def test_subscribing_goes_to_stripe_with_the_units_and_comes_back_subscribed(app, owner, fake, session):
    import app.models as m
    bea = _as(app, user_id=owner["bea"])
    r = bea.post(_url(owner, "/subscribe"), data={"interval": "quarter"})
    assert r.status_code == 303 and r.headers["Location"].startswith("https://checkout.stripe.test/cs_")
    co, = fake.checkouts
    assert (co["price"], co["units"], co["client_id"], co["customer"]) == ("price_quarter_250", 100, owner["client"], f"cus_{owner['client']}")
    assert co["success_url"] == f"{BASE}/billing/owner/{owner['client']}?session_id={{CHECKOUT_SESSION_ID}}"
    assert co["cancel_url"] == f"{BASE}/billing/owner/{owner['client']}?cancelled=1"
    assert m.Subscription.query.count() == 0                 # nothing until Stripe says so

    sid = r.headers["Location"].rsplit("/", 1)[-1]
    fake.pay(sid)
    page = bea.get(_url(owner) + f"?session_id={sid}").get_data(as_text=True)
    assert "you're subscribed" in page and "Active" in page and "Quarterly · $2.50 per unit" in page
    row = m.Subscription.query.one()
    assert (row.status, row.interval, row.units, row.cents_per_unit, row.client_id) == ("active", "quarter", 100, 250, owner["client"])
    assert row.current_period_end == dt.datetime.utcfromtimestamp(1800000000)

    r = bea.post(_url(owner, "/subscribe"), data={"interval": "month"})          # a second one is refused
    assert r.status_code == 302 and len(fake.checkouts) == 1
    assert "already has a subscription" in bea.get(_url(owner)).get_data(as_text=True)


def test_a_checkout_that_is_not_this_owners_is_ignored(app, owner, fake, session):
    import app.models as m
    bea = _as(app, user_id=owner["bea"])
    r = bea.post(_url(owner, "/subscribe"), data={"interval": "month"})
    sid = r.headers["Location"].rsplit("/", 1)[-1]
    fake.pay(sid)
    fake.sessions[sid]["client_reference_id"] = "owner:99999"
    page = bea.get(_url(owner) + f"?session_id={sid}").get_data(as_text=True)
    assert m.Subscription.query.count() == 0 and "still confirming" in page
    assert "Nothing was charged" in bea.get(_url(owner) + "?cancelled=1").get_data(as_text=True)


def test_nothing_to_price_or_stripe_off_means_no_subscribing(app, owner, fake, session):
    import app.models as m
    bea = _as(app, user_id=owner["bea"])
    app.config["STRIPE_SECRET_KEY"] = ""
    page = bea.get(_url(owner)).get_data(as_text=True)
    assert "not switched on for this portal yet" in page
    bea.post(_url(owner, "/subscribe"), data={"interval": "month"})
    assert fake.checkouts == []
    app.config["STRIPE_SECRET_KEY"] = "sk_test_x"
    for c in m.Contract.query.all():
        session.delete(c)
    session.commit()
    bea.post(_url(owner, "/subscribe"), data={"interval": "month"})
    assert fake.checkouts == [] and "nothing to price" in bea.get(_url(owner)).get_data(as_text=True)


# ── after subscribing ────────────────────────────────────────────────────────

def test_manage_opens_stripes_customer_page(app, owner, fake):
    bea, _ = _subscribe(app, fake, owner)
    r = bea.post(_url(owner, "/manage"))
    assert r.status_code == 303 and r.headers["Location"] == f"https://billing.stripe.test/p/cus_{owner['client']}"
    assert fake.portals == [(f"cus_{owner['client']}", f"{BASE}/billing/owner/{owner['client']}")]


def test_switching_monthly_and_quarterly_credits_the_unused_part(app, owner, fake, session):
    import app.models as m
    bea, _ = _subscribe(app, fake, owner, "quarter")
    r = bea.post(_url(owner, "/switch"), data={"interval": "month"}, follow_redirects=True)
    assert "Switched to monthly billing" in r.get_data(as_text=True)
    assert fake.modifies[-1] == {"sub": "sub_1", "item": "si_sub_1", "proration": "always_invoice", "price": "price_month_85", "units": 100}
    row = m.Subscription.query.one()
    assert (row.interval, row.cents_per_unit) == ("month", 85)
    bea.post(_url(owner, "/switch"), data={"interval": "month"})                 # already monthly
    assert len(fake.modifies) == 1


def test_a_unit_count_change_reaches_the_subscription_prorated(app, owner, fake, session):
    import app.models as m
    _subscribe(app, fake, owner)
    root = _as(app, superuser=True)
    r = root.post(f"/buildings/{owner['b1']}/contract", data={"unit_count": "50", "start_date": "2026-11-21"},
                  follow_redirects=True)
    assert "now bills 110 units (was 100)" in r.get_data(as_text=True)
    assert fake.modifies[-1] == {"sub": "sub_1", "item": "si_sub_1", "proration": "create_prorations", "price": None, "units": 110}
    assert m.Subscription.query.one().units == 110
    # a building given its first unit count: the daily check bills it too
    session.add(m.Contract(building_id=owner["b3"], unit_count=12, start_date=dt.date(2026, 11, 21)))
    session.commit()
    assert subs.reconcile_all() == 1
    assert fake.modifies[-1]["units"] == 122 and m.Subscription.query.one().units == 122


def test_new_prices_move_existing_subscriptions_only_when_asked_and_only_from_renewal(app, owner, fake, session):
    import app.models as m
    _subscribe(app, fake, owner, "quarter")
    root = _as(app, superuser=True)
    r = root.post("/billing/rates", data={"monthly": "0.90", "quarterly": "2.70"}, follow_redirects=True)
    assert "Prices saved: $0.90 per unit monthly, $2.70 per unit quarterly." in r.get_data(as_text=True)
    assert fake.modifies == [] and m.Subscription.query.one().cents_per_unit == 250
    r = root.post("/billing/rates", data={"monthly": "0.90", "quarterly": "2.70", "apply_existing": "1"}, follow_redirects=True)
    assert "Moved to them from the next renewal: Acme Housing." in r.get_data(as_text=True)
    assert fake.modifies[-1] == {"sub": "sub_1", "item": "si_sub_1", "proration": "none", "price": "price_quarter_270", "units": None}
    assert m.SubscriptionRate.query.count() == 2 and subs.current_rates() == {"month": 90, "quarter": 270}
    root.post("/billing/rates", data={"monthly": "0", "quarterly": "2.70"})
    assert m.SubscriptionRate.query.count() == 2                                # refused


# ── what Stripe tells the portal ─────────────────────────────────────────────

def _webhook(app, monkeypatch, event):
    from app import routes
    monkeypatch.setattr(routes, "verify_stripe_webhook", lambda payload, sig: event)
    return app.test_client().post("/webhooks/stripe", data=b"{}", headers={"Stripe-Signature": "t"})


def test_payments_and_failures_arrive_by_webhook_once_each(app, owner, fake, session, monkeypatch):
    import app.models as m
    bea, _ = _subscribe(app, fake, owner)
    iid = fake.invoice("sub_1", paid=True, amount=25000)
    event = {"type": "invoice.paid", "data": {"object": {"id": iid}}}
    assert _webhook(app, monkeypatch, event).status_code == 200
    assert _webhook(app, monkeypatch, event).status_code == 200                # Stripe retries: recorded once
    ch, = m.SubscriptionCharge.query.all()
    assert (ch.status, ch.amount_cents, ch.number) == ("paid", 25000, "ECHO-0001")

    failed = fake.invoice("sub_1", paid=False, amount=25000)
    fake.subs["sub_1"]["status"] = "past_due"
    _webhook(app, monkeypatch, {"type": "invoice.payment_failed", "data": {"object": {"id": failed}}})
    assert m.SubscriptionCharge.query.filter_by(stripe_invoice_id=failed).one().status == "failed"
    page = bea.get(_url(owner)).get_data(as_text=True)
    assert "The last payment did not go through." in page and "did not go through" in page and "Payment overdue" in page

    fake.subs["sub_1"].update(status="active", cancel_at_period_end=True)
    _webhook(app, monkeypatch, {"type": "customer.subscription.updated", "data": {"object": {"id": "sub_1"}}})
    assert m.Subscription.query.one().cancel_at_period_end is True
    fake.subs["sub_1"]["status"] = "canceled"
    _webhook(app, monkeypatch, {"type": "customer.subscription.deleted", "data": {"object": {"id": "sub_1"}}})
    assert subs.live_subscription(session.get(m.Client, owner["client"])) is None
    assert "Subscribe and add a payment method" in bea.get(_url(owner)).get_data(as_text=True)    # can start again


def test_a_subscription_checkout_never_settles_an_invoice(app, owner, fake, session, monkeypatch):
    import app.models as m
    c = m.Contract.query.filter_by(building_id=owner["b1"]).one()
    inv = m.Invoice(building_id=owner["b1"], contract_id=c.id, period_start=dt.date(2026, 7, 1), period_end=dt.date(2026, 12, 31),
                    unit_count=40, rate_per_unit_cents=1000, amount_cents=20000, status="pending")
    session.add(inv)
    session.commit()
    event = {"type": "checkout.session.completed",
             "data": {"object": {"id": "cs_x", "mode": "subscription", "payment_status": "paid",
                                 "client_reference_id": str(inv.id), "subscription": None}}}
    assert _webhook(app, monkeypatch, event).status_code == 200
    session.refresh(inv)
    assert inv.status == "pending" and m.Payment.query.count() == 0


# ── who may ──────────────────────────────────────────────────────────────────

def test_only_the_administrator_or_a_person_ticked_for_the_owner(app, owner, fake, session):
    import app.models as m
    pat = _as(app, user_id=owner["pat"])
    for path in ("", "/subscribe", "/manage", "/switch"):
        r = pat.get(_url(owner, path)) if not path else pat.post(_url(owner, path), data={"interval": "month"})
        assert r.status_code == 403, path
    assert fake.checkouts == []
    assert '>Billing</a>' not in pat.get("/home").get_data(as_text=True)
    bea = _as(app, user_id=owner["bea"])
    assert bea.get("/billing/mine").headers["Location"].endswith(_url(owner))
    assert '>Billing</a>' in bea.get(_url(owner)).get_data(as_text=True)
    assert pat.post("/billing/rates", data={"monthly": "1", "quarterly": "3"}).status_code == 403
    assert bea.post(_url(owner, "/units")).status_code == 403

    root = _as(app, superuser=True)
    page = root.get(f"/users/{owner['pat']}/permissions").get_data(as_text=True)
    assert "may manage Acme Housing's subscription and payment" in page
    root.post(f"/users/{owner['pat']}/properties",
              data={"property_id": [owner["property"]], "bill_offered": [owner["client"]], "bill_client_id": [owner["client"]]})
    assert m.BillingControl.query.filter_by(user_id=owner["pat"]).count() == 1
    assert pat.get(_url(owner)).status_code == 200
    root.post(f"/users/{owner['pat']}/properties", data={"property_id": [owner["property"]], "bill_offered": [owner["client"]]})
    assert pat.get(_url(owner)).status_code == 403


def test_the_administrators_billing_page_lists_every_owner_and_the_prices(app, owner, fake):
    _subscribe(app, fake, owner)
    page = _as(app, superuser=True).get("/billing").get_data(as_text=True)
    assert "Subscriptions — monthly or quarterly, card on file" in page
    assert "Acme Housing" in page and "Quarterly · $2.50/unit" in page and "$250.00" in page
    assert 'name="monthly" value="0.85"' in page and 'name="quarterly" value="2.50"' in page
    assert "webhook secret is not set" in page
