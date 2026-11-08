"""In-memory stand-in for PayPal, used by tests and by local demo mode (no credentials)."""

from __future__ import annotations

import itertools


class FakePayPal:
    def __init__(self) -> None:
        self.invoices: dict[str, dict] = {}
        self._seq = itertools.count(1)

    def create_invoice(self, recipient_name, recipient_email, items, note=""):
        n = next(self._seq)
        total = sum(float(i["quantity"]) * float(i["unit_price"]) for i in items)
        given, _, surname = recipient_name.partition(" ")
        inv = {
            "id": f"INV2-DEMO-{n:04d}",
            "status": "DRAFT",
            "detail": {"invoice_number": f"{n:04d}", "currency_code": "USD", "note": note},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname}, "email_address": recipient_email}}],
            "amount": {"value": f"{total:.2f}", "currency_code": "USD"},
            "links": [],
        }
        self.invoices[inv["id"]] = inv
        return inv

    def send_invoice(self, invoice_id):
        inv = self.invoices[invoice_id]
        inv["status"] = "SENT"
        inv["links"] = [{"rel": "payer-view", "href": f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"}]
        return {"links": inv["links"]}

    def get_invoice(self, invoice_id):
        return self.invoices[invoice_id]

    def list_invoices(self, page_size=20):
        return list(self.invoices.values())[-page_size:]

    def remind_invoice(self, invoice_id, note=""):
        self.invoices[invoice_id]  # raises KeyError if unknown
        return {}

    def cancel_invoice(self, invoice_id, note=""):
        self.invoices[invoice_id]["status"] = "CANCELLED"
        return {}

    def mark_paid(self, invoice_id):  # demo helper
        self.invoices[invoice_id]["status"] = "PAID"
