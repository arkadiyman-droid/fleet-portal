"""
tests/test_settlement.py — the one path an invoice may become paid.

Both the manual route and the Stripe webhook funnel through
billing._finalize_payment(), specifically so the two can never drift. The
guard inside it is what stops a double-settlement, and it runs BEFORE any
database work — which is why it can be tested here with a stub and no
Postgres.

Also covers the Stripe method mapping, where the failure mode is subtle:
inventing a Payment.method value that the check constraint rejects would
turn a successful customer payment into a 500 inside a webhook.
"""

import types

import pytest

from app.billing import _finalize_payment
from app.stripe_billing import _map_payment_method, stripe_configured


def invoice(status):
    """Enough of an Invoice for the status guard. Nothing past the guard
    is reached for a non-pending invoice, so no database is involved."""
    return types.SimpleNamespace(id=1, status=status, amount_cents=12000)


class OnlyPendingInvoicesCanBeSettled:

    @pytest.mark.parametrize("status", ["draft", "paid", "void"])
    def test_other_statuses_are_a_no_op_not_an_error(self, status):
        """A no-op rather than a raise, deliberately: the webhook caller
        treats any non-error as 'stop retrying', and a retried delivery
        against an already-paid invoice is legitimate, not a failure."""
        inv = invoice(status)
        result = _finalize_payment(inv, amount_cents=12000,
                                   paid_date=None, method="card")
        assert result is None
        assert inv.status == status, "a rejected settlement must not mutate the invoice"

    def test_an_already_paid_invoice_is_not_re_marked(self):
        inv = invoice("paid")
        _finalize_payment(inv, amount_cents=999, paid_date=None, method="card")
        assert inv.status == "paid"


class StripeMethodMapping:
    """Payment.method is constrained to check/ach/wire/card/other. The
    mapper must never emit anything outside that set, whatever Stripe
    sends."""

    def test_card(self):
        pi = types.SimpleNamespace(payment_method_types=["card"])
        assert _map_payment_method(pi) == "card"

    def test_us_bank_account_maps_to_ach(self):
        pi = types.SimpleNamespace(payment_method_types=["us_bank_account"])
        assert _map_payment_method(pi) == "ach"

    def test_an_unknown_method_falls_back_to_other(self):
        """Stripe adds payment method types over time. A new one must land
        on 'other', not crash the webhook or violate the constraint."""
        pi = types.SimpleNamespace(payment_method_types=["sepa_debit", "boleto"])
        assert _map_payment_method(pi) == "other"

    def test_missing_and_empty_are_handled(self):
        assert _map_payment_method(types.SimpleNamespace()) == "other"
        assert _map_payment_method(types.SimpleNamespace(payment_method_types=None)) == "other"
        assert _map_payment_method(types.SimpleNamespace(payment_method_types=[])) == "other"

    def test_the_result_is_always_a_legal_payment_method(self):
        legal = {"check", "ach", "wire", "card", "other"}
        for types_list in ([], ["card"], ["us_bank_account"], ["anything_new"],
                           ["boleto", "card"]):
            pi = types.SimpleNamespace(payment_method_types=types_list)
            assert _map_payment_method(pi) in legal


class StripeIsOptional:
    """*"the whole feature degrades to manual/pay-by-hand invoicing when
    unset."* A portal with no Stripe keys must still run — that is the
    state it ships in."""

    def test_not_configured_without_a_secret_key(self, monkeypatch):
        from app import stripe_billing
        monkeypatch.setattr(stripe_billing, "current_app",
                            types.SimpleNamespace(config={"STRIPE_SECRET_KEY": ""}))
        assert stripe_configured() is False

    def test_configured_with_a_secret_key(self, monkeypatch):
        from app import stripe_billing
        monkeypatch.setattr(stripe_billing, "current_app",
                            types.SimpleNamespace(config={"STRIPE_SECRET_KEY": "sk_test_x"}))
        assert stripe_configured() is True
