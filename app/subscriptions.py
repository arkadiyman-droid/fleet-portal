"""
app/subscriptions.py — owners pay by the apartment unit, monthly or quarterly, card on file

One Stripe subscription per owner (Client). Its quantity is the owner's
apartment units — the unit counts on its buildings' current contracts — and
its price is the rate per unit for the interval chosen:

    monthly    current_rates()["month"]    cents per unit per month   (default 85)
    quarterly  current_rates()["quarter"]  cents per unit per quarter (default 250)

THE PAYER'S SIDE. The owner's billing page (routes.owner_billing) shows the
buildings, the units and both prices. "Subscribe" opens Stripe Checkout in
subscription mode: Stripe shows every payment method switched on in its own
dashboard (cards, Apple Pay and Google Pay, bank debit, PayPal where Stripe
offers it), keeps what was used on file and charges it every period. "Manage
billing" opens Stripe's Customer Portal: change the card, see and download
invoices, cancel. No card number ever passes through the portal.

STRIPE IS THE SOURCE OF TRUTH. The local Subscription row is refreshed from
Stripe — never from what a browser says — on return from Checkout (the
session is fetched server-side), on every webhook, when the billing page is
opened, and once a day (reconcile_all). So it stays right even while Stripe's
webhooks cannot reach the portal (it sits behind Cloudflare Access).

WHAT MOVES THE PRICE. A building's unit count changing, or a building
leaving: sync_units() sets the new quantity, prorated on the next charge.
Switching monthly <-> quarterly: switch_interval(), which credits the unused
part of the current period and charges the new one at once. A new rate:
set_rates(), which applies to new subscriptions and — when asked — to the
existing ones from their next renewal, never in the middle of a period.

Every call to Stripe goes through one of the api_* functions below, so the
tests replace Stripe with a fake at exactly those points.
"""
import datetime as _dt
import logging

import stripe
from flask import current_app

from app.extensions import db

log = logging.getLogger(__name__)

INTERVALS = {"month": ("Monthly", 1), "quarter": ("Quarterly", 3)}
PRODUCT_ID = "echotherm_fleet_monitoring"
PRODUCT_NAME = "EchoTherm building monitoring"
LIVE = ("active", "trialing", "past_due", "unpaid", "incomplete")      # anything not over
PORTAL_TAG = "echotherm_fleet"


# ── prices and units (no Stripe) ─────────────────────────────────────────────

def current_rates(cfg=None) -> dict:
    """{'month': cents per unit, 'quarter': cents per unit} in force now."""
    from app.models import SubscriptionRate
    row = SubscriptionRate.query.order_by(SubscriptionRate.id.desc()).first()
    if row is not None:
        return {"month": row.monthly_cents, "quarter": row.quarterly_cents}
    cfg = cfg or current_app.config
    return {"month": int(cfg.get("SUB_MONTHLY_CENTS_PER_UNIT", 85)),
            "quarter": int(cfg.get("SUB_QUARTERLY_CENTS_PER_UNIT", 250))}


def owner_buildings(client) -> list:
    """[(building, units or None)] for every active building of the owner's
    active properties, in name order; units from the current contract."""
    from app.billing import current_contract
    out = []
    for prop in sorted((p for p in client.properties if p.is_active), key=lambda p: p.name.lower()):
        for b in sorted((b for b in prop.buildings if b.is_active), key=lambda b: b.name.lower()):
            c = current_contract(b)
            out.append((b, c.unit_count if c else None))
    return out


def owner_units(client) -> int:
    return sum(u or 0 for _b, u in owner_buildings(client))


def money(cents) -> str:
    return f"${(cents or 0) / 100:,.2f}"


def live_subscription(client):
    """The owner's subscription that is not over, or None."""
    from app.models import Subscription
    return (Subscription.query.filter(Subscription.client_id == client.id, Subscription.status.in_(LIVE))
            .order_by(Subscription.id.desc()).first())


def configured() -> bool:
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


# ── the Stripe calls (the only places that talk to Stripe) ───────────────────

def _key():
    stripe.api_key = current_app.config["STRIPE_SECRET_KEY"]


def _plain(obj):
    """A Stripe object as plain dicts and lists."""
    if obj is None or isinstance(obj, (dict, list, str, int, float, bool)):
        return obj
    for name in ("to_dict_recursive", "to_dict"):
        f = getattr(obj, name, None)
        if f:
            return f()
    return dict(obj)


def api_customer(client) -> str:
    """The owner's Stripe customer id, made on first use."""
    from app.stripe_billing import get_or_create_customer
    return get_or_create_customer(client)


def api_price_id(interval, cents) -> str:
    """The Stripe Price for this many cents per unit per interval, made on
    first use (Prices cannot be edited in Stripe, so a new rate is a new
    Price). Found again by its lookup key."""
    _key()
    lookup = f"{PRODUCT_ID}_{interval}_{cents}"
    found = stripe.Price.list(lookup_keys=[lookup], active=True, limit=1).data
    if found:
        return found[0].id
    try:
        stripe.Product.retrieve(PRODUCT_ID)
    except stripe.error.InvalidRequestError:
        stripe.Product.create(id=PRODUCT_ID, name=PRODUCT_NAME)
    label, months = INTERVALS[interval]
    price = stripe.Price.create(product=PRODUCT_ID, currency="usd", unit_amount=cents, lookup_key=lookup,
                                nickname=f"{label}, {money(cents)} per unit",
                                recurring={"interval": "month", "interval_count": months})
    return price.id


def api_checkout(customer_id, price_id, units, client_id, success_url, cancel_url) -> str:
    """A Stripe Checkout session that starts the subscription; returns its URL.
    No payment_method_types: Stripe offers every method switched on in its
    dashboard, Apple Pay, Google Pay and PayPal included."""
    _key()
    session = stripe.checkout.Session.create(
        mode="subscription", customer=customer_id,
        line_items=[{"price": price_id, "quantity": units}],
        client_reference_id=f"owner:{client_id}",
        subscription_data={"metadata": {"client_id": str(client_id)}},
        metadata={"client_id": str(client_id)},
        success_url=success_url, cancel_url=cancel_url)
    return session.url


def api_retrieve_checkout(session_id) -> dict:
    _key()
    return _plain(stripe.checkout.Session.retrieve(session_id))


def api_retrieve_subscription(sub_id) -> dict:
    _key()
    return _plain(stripe.Subscription.retrieve(sub_id))


def api_retrieve_invoice(invoice_id) -> dict:
    _key()
    return _plain(stripe.Invoice.retrieve(invoice_id))


def api_modify_subscription(sub_id, item_id, proration, price_id=None, units=None) -> dict:
    _key()
    item = {"id": item_id}
    if price_id:
        item["price"] = price_id
    if units is not None:
        item["quantity"] = units
    return _plain(stripe.Subscription.modify(sub_id, items=[item], proration_behavior=proration))


def api_portal(customer_id, return_url) -> str:
    """Stripe's Customer Portal for this owner; returns its URL. Uses one
    portal setup made by the portal (tagged in its metadata): change the
    card, see invoices, cancel at the end of the period. Changing the plan or
    the number of units there is OFF — units come from the buildings, and
    monthly/quarterly is switched on the owner's billing page."""
    _key()
    config_id = None
    for c in stripe.billing_portal.Configuration.list(active=True, limit=50).data:
        if (c.get("metadata") or {}).get(PORTAL_TAG) == "1":
            config_id = c.id
            break
    if config_id is None:
        config_id = stripe.billing_portal.Configuration.create(
            business_profile={"headline": "EchoTherm: billing for building monitoring"},
            features={"payment_method_update": {"enabled": True},
                      "invoice_history": {"enabled": True},
                      "customer_update": {"enabled": True, "allowed_updates": ["email", "address"]},
                      "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
                      "subscription_update": {"enabled": False}},
            metadata={PORTAL_TAG: "1"}).id
    return stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url,
                                                configuration=config_id).url


# ── keeping the local row in step with Stripe ────────────────────────────────

def _ts(v):
    try:
        return _dt.datetime.utcfromtimestamp(int(v)) if v else None
    except (TypeError, ValueError):
        return None


def apply(sub: dict):
    """Store a Stripe subscription (as fetched from Stripe) on its owner's
    row. Returns the row, or None when it is not one of ours."""
    from app.models import Client, Subscription
    if not isinstance(sub, dict) or not sub.get("id"):
        return None
    row = Subscription.query.filter_by(stripe_subscription_id=sub["id"]).first()
    client_id = None
    try:
        client_id = int((sub.get("metadata") or {}).get("client_id"))
    except (TypeError, ValueError):
        pass
    if row is None:
        if client_id is None or db.session.get(Client, client_id) is None:
            log.warning("Stripe subscription %s names no owner of ours", sub.get("id"))
            return None
        row = Subscription(client_id=client_id, stripe_subscription_id=sub["id"], status="incomplete",
                           interval="month", units=0, cents_per_unit=0)
        db.session.add(row)
    items = ((sub.get("items") or {}).get("data") or [])
    item = items[0] if items else {}
    price = item.get("price") or {}
    months = int(((price.get("recurring") or {}).get("interval_count")) or 1)
    row.stripe_item_id = item.get("id") or row.stripe_item_id
    row.status = sub.get("status") or row.status
    row.interval = "quarter" if months == 3 else "month"
    row.units = int(item.get("quantity") or 0)
    row.cents_per_unit = int(price.get("unit_amount") or 0)
    # newer Stripe API versions keep the period end on the item, older ones on the subscription
    row.current_period_end = _ts(sub.get("current_period_end") or item.get("current_period_end"))
    row.cancel_at_period_end = bool(sub.get("cancel_at_period_end"))
    return row


def refresh(row):
    """Fetch the subscription from Stripe and store it. Never raises: a
    failure is kept on the row for the page to show."""
    try:
        apply(api_retrieve_subscription(row.stripe_subscription_id))
        row.last_error = None
    except Exception as e:
        row.last_error = f"could not read it from Stripe: {str(e)[:200]}"
    return row


def record_invoice(inv: dict):
    """Store one Stripe invoice of one of our subscriptions as a charge."""
    from app.models import Subscription, SubscriptionCharge
    sub_id = inv.get("subscription")
    if not sub_id:                                    # newer API versions file it under parent
        sub_id = (((inv.get("parent") or {}).get("subscription_details") or {}).get("subscription"))
    if isinstance(sub_id, dict):
        sub_id = sub_id.get("id")
    row = Subscription.query.filter_by(stripe_subscription_id=sub_id).first() if sub_id else None
    if row is None:
        return None
    ch = SubscriptionCharge.query.filter_by(stripe_invoice_id=inv["id"]).first()
    if ch is None:
        ch = SubscriptionCharge(subscription=row, stripe_invoice_id=inv["id"], amount_cents=0, status="open",
                                created_at=_ts(inv.get("created")) or _dt.datetime.utcnow())
        db.session.add(ch)
    paid = inv.get("status") == "paid" or bool(inv.get("paid"))
    ch.number = inv.get("number") or ch.number
    ch.amount_cents = int((inv.get("amount_paid") if paid else inv.get("amount_due")) or 0)
    ch.hosted_url = inv.get("hosted_invoice_url") or ch.hosted_url
    if paid:
        ch.status = "paid"
        ch.paid_at = _ts(((inv.get("status_transitions") or {}).get("paid_at"))) or ch.paid_at or _dt.datetime.utcnow()
    elif inv.get("attempted") and inv.get("status") == "open":
        ch.status = "failed"
    return ch


def handle_event(event) -> None:
    """A verified Stripe webhook event. Only ids are taken from it: the
    subscription or invoice is then fetched from Stripe, so the event's own
    shape (which follows the webhook's API version) does not matter."""
    etype = event["type"]
    obj = (event.get("data") or {}).get("object") or {}
    if etype in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        if obj.get("mode") == "subscription" and obj.get("subscription"):
            sub_id = obj["subscription"] if isinstance(obj["subscription"], str) else obj["subscription"].get("id")
            apply(api_retrieve_subscription(sub_id))
    elif etype.startswith("customer.subscription."):
        apply(api_retrieve_subscription(obj["id"]))
    elif etype in ("invoice.paid", "invoice.payment_failed", "invoice.payment_succeeded", "invoice.finalized"):
        inv = api_retrieve_invoice(obj["id"])
        ch = record_invoice(inv)
        if ch is not None:
            refresh(ch.subscription)
    else:
        return
    db.session.commit()


# ── what a person asks for ───────────────────────────────────────────────────

class Refused(Exception):
    """Something the billing page should say instead of doing."""


def start_checkout(client, interval, base_url) -> str:
    """The Stripe Checkout URL that starts this owner's subscription."""
    if interval not in INTERVALS:
        raise Refused("choose monthly or quarterly")
    if live_subscription(client) is not None:
        raise Refused("this owner already has a subscription; change it with Manage billing")
    units = owner_units(client)
    if units <= 0:
        raise Refused("none of this owner's buildings has a unit count yet, so there is nothing to price")
    rate = current_rates()[interval]
    page = f"{base_url}/billing/owner/{client.id}"
    return api_checkout(api_customer(client), api_price_id(interval, rate), units, client.id,
                        success_url=page + "?session_id={CHECKOUT_SESSION_ID}", cancel_url=page + "?cancelled=1")


def finish_checkout(client, session_id):
    """Back from Checkout: fetch the session from Stripe and store the
    subscription it started — only if it is this owner's."""
    s = api_retrieve_checkout(session_id)
    ours = (s.get("client_reference_id") == f"owner:{client.id}"
            and (not client.billing_customer_id or s.get("customer") == client.billing_customer_id))
    if not ours or s.get("mode") != "subscription" or not s.get("subscription"):
        return None
    sub_id = s["subscription"] if isinstance(s["subscription"], str) else s["subscription"].get("id")
    row = apply(api_retrieve_subscription(sub_id))
    db.session.commit()
    return row


def portal_url(client, base_url) -> str:
    if not client.billing_customer_id:
        raise Refused("this owner has not subscribed yet")
    return api_portal(client.billing_customer_id, f"{base_url}/billing/owner/{client.id}")


def sync_units(client) -> dict:
    """Bill the units the owner's buildings have now. Prorated: the next
    charge carries the difference for the rest of the period."""
    row = live_subscription(client)
    if row is None or row.status not in ("active", "trialing", "past_due"):
        return {"changed": False, "why": "no running subscription"}
    units = owner_units(client)
    if units <= 0:
        return {"changed": False, "why": "no unit counts left; the subscription was left as it is"}
    if units == row.units:
        return {"changed": False, "why": "already billed for these units"}
    old = row.units
    apply(api_modify_subscription(row.stripe_subscription_id, row.stripe_item_id, "create_prorations", units=units))
    db.session.commit()
    return {"changed": True, "old": old, "new": units}


def switch_interval(client, interval) -> dict:
    """Monthly <-> quarterly, at the current rate. The unused part of the
    current period is credited and the new period is charged now."""
    if interval not in INTERVALS:
        raise Refused("choose monthly or quarterly")
    row = live_subscription(client)
    if row is None or row.status not in ("active", "trialing", "past_due"):
        raise Refused("there is no running subscription to change")
    if row.interval == interval:
        raise Refused("it is already " + INTERVALS[interval][0].lower())
    units = owner_units(client) or row.units
    price = api_price_id(interval, current_rates()[interval])
    apply(api_modify_subscription(row.stripe_subscription_id, row.stripe_item_id, "always_invoice",
                                  price_id=price, units=units))
    db.session.commit()
    return {"interval": interval}


def set_rates(monthly_cents, quarterly_cents, by, apply_existing) -> dict:
    """Save new prices per unit. New subscriptions use them at once; with
    apply_existing, every running subscription moves to them from its NEXT
    renewal (no proration, nothing charged now)."""
    from app.models import SubscriptionRate
    if monthly_cents <= 0 or quarterly_cents <= 0:
        raise Refused("prices must be above zero")
    db.session.add(SubscriptionRate(by=by, monthly_cents=monthly_cents, quarterly_cents=quarterly_cents))
    db.session.commit()
    moved, failed = [], []
    if apply_existing:
        from app.models import Subscription
        rates = {"month": monthly_cents, "quarter": quarterly_cents}
        for row in Subscription.query.filter(Subscription.status.in_(("active", "trialing", "past_due"))).all():
            if row.cents_per_unit == rates[row.interval]:
                continue
            try:
                apply(api_modify_subscription(row.stripe_subscription_id, row.stripe_item_id, "none",
                                              price_id=api_price_id(row.interval, rates[row.interval])))
                moved.append(row.client.name)
            except Exception as e:
                failed.append(f"{row.client.name} ({str(e)[:80]})")
        db.session.commit()
    return {"moved": moved, "failed": failed}


def reconcile_all() -> int:
    """Once a day: every running subscription re-read from Stripe and billed
    for the units its owner has now. Returns how many were looked at."""
    from app.models import Subscription
    rows = Subscription.query.filter(Subscription.status.in_(LIVE)).all()
    for row in rows:
        refresh(row)
        db.session.commit()
        try:
            sync_units(row.client)
        except Exception as e:
            db.session.rollback()
            row.last_error = f"could not update the units: {str(e)[:200]}"
            db.session.commit()
    return len(rows)
