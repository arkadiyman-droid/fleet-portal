"""
app/stripe_billing.py — Phase 5 Stripe Checkout integration.

Best-effort / optional: STRIPE_SECRET_KEY / STRIPE_WEBHOOK_SECRET are
optional config (see config.py). Every function here either checks
stripe_configured() itself or is only ever called after a caller has
checked it — Phases 1-4 keep working with zero Stripe config present.

Checkout Sessions are created fresh on demand (not a persistent Stripe
Payment Link object) so Checkout's ~24h expiry is a non-issue — a new
session is created every time create_checkout_session() runs, and
overwrites Invoice.stripe_checkout_session_id / stripe_checkout_url.

The webhook (app/routes.py::stripe_webhook, POST /webhooks/stripe) is the
ONLY thing that marks an invoice paid via Stripe — the Checkout
success_url redirect is a UX nicety only (spoofable/replayable), never
trusted to change state. The webhook defers the actual DB write to
app/billing.py::_finalize_payment() — the exact same helper the manual
'Record Payment' route uses — so the two paths can never drift.
"""

import logging
from datetime import datetime

import stripe
from flask import current_app, url_for

from app.billing import _finalize_payment
from app.extensions import db
from app.models import Client, Invoice

log = logging.getLogger(__name__)

_METHOD_MAP = {"card": "card", "us_bank_account": "ach"}


class WebhookVerificationError(Exception):
    """Raised by verify_stripe_webhook() on a missing/invalid signature or
    unconfigured secret — routes.py maps this to a 400 response. Kept as a
    plain exception (not a stripe.error.* type) so routes.py never needs
    to import the stripe SDK itself."""


def stripe_configured() -> bool:
    return bool(current_app.config.get("STRIPE_SECRET_KEY"))


def _api_key() -> str:
    key = current_app.config.get("STRIPE_SECRET_KEY")
    if not key:
        raise RuntimeError("stripe_billing called with Stripe not configured — caller must check stripe_configured() first")
    return key


def get_or_create_customer(client: Client) -> str:
    """Lazy Stripe Customer creation. Persists the id on
    Client.billing_customer_id so every later Checkout Session for the
    same Client reuses it, grouping their payment history in Stripe's
    dashboard."""
    if client.billing_customer_id:
        return client.billing_customer_id
    stripe.api_key = _api_key()
    customer = stripe.Customer.create(name=client.name, email=client.contact_email or None)
    client.billing_customer_id = customer.id
    db.session.commit()
    return customer.id


def create_checkout_session(invoice: Invoice):
    """Creates a fresh Stripe Checkout Session for `invoice`, persists its
    id + url onto the Invoice, and returns (session_id, url). Single
    inline price_data line item — no pre-existing Stripe Price/Product,
    since the amount is dynamic per building/period. Raises
    stripe.error.StripeError on any Stripe-side failure; callers catch
    broadly and flash rather than 500ing."""
    stripe.api_key = _api_key()
    building = invoice.building
    client = building.property.client
    customer_id = get_or_create_customer(client)
    session = stripe.checkout.Session.create(
        mode="payment",
        customer=customer_id,
        client_reference_id=str(invoice.id),
        line_items=[{
            "price_data": {
                "currency": "usd",
                "unit_amount": invoice.amount_cents,
                "product_data": {
                    "name": f"{building.name} — {invoice.period_start.isoformat()} to {invoice.period_end.isoformat()}",
                },
            },
            "quantity": 1,
        }],
        success_url=url_for("admin.invoice_detail", invoice_id=invoice.id, paid="1", _external=True),
        cancel_url=url_for("admin.invoice_detail", invoice_id=invoice.id, _external=True),
    )
    invoice.stripe_checkout_session_id = session.id
    invoice.stripe_checkout_url = session.url
    db.session.commit()
    return session.id, session.url


def verify_stripe_webhook(payload: bytes, sig_header: str):
    """Verifies payload/sig_header against STRIPE_WEBHOOK_SECRET, returns
    the parsed stripe.Event. Raises WebhookVerificationError on any
    failure (missing secret, missing/invalid signature)."""
    webhook_secret = current_app.config.get("STRIPE_WEBHOOK_SECRET")
    if not webhook_secret:
        raise WebhookVerificationError("STRIPE_WEBHOOK_SECRET is not configured")
    try:
        return stripe.Webhook.construct_event(payload, sig_header, webhook_secret)
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        raise WebhookVerificationError(str(e)) from e


def _map_payment_method(payment_intent) -> str:
    for t in getattr(payment_intent, "payment_method_types", None) or []:
        if t in _METHOD_MAP:
            return _METHOD_MAP[t]
    return "other"  # never invent a new Payment.method value — reuse the existing check constraint's set


def mark_paid_from_stripe_event(event) -> None:
    """Core webhook handler logic. Called by routes.py::stripe_webhook()
    after signature verification. Always treated as a 200 by the caller —
    Stripe expects any non-error response to stop retries, and every
    'nothing to do' branch below (event type we don't care about, session
    not actually paid yet, unknown invoice, already-settled invoice) is a
    legitimate no-op, not a delivery failure."""
    event_type = event["type"]
    if event_type not in ("checkout.session.completed", "checkout.session.async_payment_succeeded"):
        return
    session_obj = event["data"]["object"]
    if session_obj.get("payment_status") != "paid":
        return  # e.g. an async method's session that hasn't cleared funds yet

    try:
        invoice_id = int(session_obj.get("client_reference_id"))
    except (TypeError, ValueError):
        log.warning("Stripe webhook: session %s has no usable client_reference_id", session_obj.get("id"))
        return
    invoice = Invoice.query.get(invoice_id)
    if invoice is None:
        log.warning("Stripe webhook: session %s references unknown invoice_id %s", session_obj.get("id"), invoice_id)
        return

    payment_intent_id = session_obj.get("payment_intent")
    method = "other"
    if payment_intent_id:
        stripe.api_key = _api_key()
        intent = stripe.PaymentIntent.retrieve(payment_intent_id)
        method = _map_payment_method(intent)

    amount_cents = session_obj.get("amount_total") or invoice.amount_cents
    payment = _finalize_payment(
        invoice, amount_cents=amount_cents, paid_date=datetime.utcnow().date(),
        method=method, stripe_payment_intent_id=payment_intent_id,
    )
    if payment is None:
        log.info(
            "Stripe webhook: invoice %s not finalized (already paid, or intent %s already recorded) — no-op",
            invoice_id, payment_intent_id,
        )
