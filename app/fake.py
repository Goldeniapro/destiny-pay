"""In-memory stand-in for PayPal, used by tests and by demo mode (no credentials)."""

from __future__ import annotations

import base64
import itertools

from .paypal import PAID_STATUSES, UNPAID_STATUSES, PayPalError

# 1x1 transparent PNG: demo mode has no real QR to show
_DEMO_QR = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c50000000049454e44ae426082")
).decode()


class FakePayPal:
    currency = "USD"

    def __init__(self) -> None:
        self.invoices: dict[str, dict] = {}
        self._seq = itertools.count(1)

    def _get(self, invoice_id: str) -> dict:
        if invoice_id not in self.invoices:
            raise PayPalError("PayPal 404: INVOICE_NOT_FOUND")
        return self.invoices[invoice_id]

    def create_invoice(self, recipient_name, recipient_email, items, note="", reference=""):
        n = next(self._seq)
        total = sum(float(i["quantity"]) * float(i["unit_price"]) for i in items)
        given, _, surname = recipient_name.partition(" ")
        inv = {
            "id": f"INV2-DEMO-{n:04d}",
            "status": "DRAFT",
            "detail": {"invoice_number": f"{n:04d}", "currency_code": "USD", "note": note, "reference": reference or None},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname}, "email_address": recipient_email}}],
            "amount": {"value": f"{total:.2f}", "currency_code": "USD"},
            "due_amount": {"value": f"{total:.2f}", "currency_code": "USD"},
            "links": [],
        }
        self.invoices[inv["id"]] = inv
        return inv

    def send_invoice(self, invoice_id):
        inv = self._get(invoice_id)
        if inv["status"] != "DRAFT":
            raise PayPalError("PayPal 422: INVOICE_ALREADY_SENT")
        inv["status"] = "UNPAID"
        inv["links"] = [{"rel": "payer-view", "href": f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"}]
        return {"links": inv["links"]}

    def get_invoice(self, invoice_id):
        return self._get(invoice_id)

    def list_invoices(self, page_size=50):
        return list(self.invoices.values())[-page_size:]

    def remind_invoice(self, invoice_id, note=""):
        if self._get(invoice_id)["status"] in PAID_STATUSES | {"DRAFT", "CANCELLED"}:
            raise PayPalError("PayPal 422: CANNOT_REMIND_INVOICE")
        return {}

    def cancel_invoice(self, invoice_id, note=""):
        inv = self._get(invoice_id)
        if inv["status"] in PAID_STATUSES | {"DRAFT"}:
            raise PayPalError("PayPal 422: CANNOT_CANCEL_INVOICE")
        inv["status"] = "CANCELLED"
        return {}

    def record_payment(self, invoice_id, amount, method="CASH", note=""):
        inv = self._get(invoice_id)
        due = float(inv["due_amount"]["value"])
        if float(amount) > due + 1e-9:
            raise PayPalError("PayPal 422: CANT_PAY_MORE_THAN_INVOICE_AMOUNT")
        due -= float(amount)
        inv["due_amount"]["value"] = f"{due:.2f}"
        inv["status"] = "MARKED_AS_PAID" if due < 0.005 else "PARTIALLY_PAID"
        return {"payment_id": f"DEMO-PAY-{invoice_id}"}

    def qr_code(self, invoice_id, size=300):
        if self._get(invoice_id)["status"] == "DRAFT":
            raise PayPalError("PayPal 422: INVOICE_NOT_SENT")
        return _DEMO_QR

    def mark_paid(self, invoice_id):  # demo helper: simulates the customer paying online
        inv = self._get(invoice_id)
        if inv["status"] not in UNPAID_STATUSES:
            raise PayPalError("PayPal 422: INVOICE_NOT_PAYABLE")
        inv["status"] = "PAID"
        inv["due_amount"]["value"] = "0.00"
