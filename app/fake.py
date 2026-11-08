"""In-memory stand-in for PayPal, used by tests and by demo mode (no credentials).

With a Store it persists its invoices, so a demo survives a server restart.
"""

from __future__ import annotations

import base64
import itertools
import threading

from .paypal import PAID_STATUSES, UNPAID_STATUSES, PayPalError

# 1x1 transparent PNG: demo mode has no real QR to show
_DEMO_QR = base64.b64encode(
    bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000d4944415478da63f8ffff3f0005fe02fea7d6a4c50000000049454e44ae426082")
).decode()


class FakePayPal:
    currency = "USD"
    base_url = "demo"

    def __init__(self, store=None) -> None:
        self.store = store
        self.invoices: dict[str, dict] = {}
        self.payouts: dict[str, dict] = {}
        self._lock = threading.Lock()
        start = 1
        if store is not None:
            meta = store.fake_get("__meta__") or {}
            start = int(meta.get("next", 1))
        self._seq = itertools.count(start)

    def _next(self) -> int:
        with self._lock:
            n = next(self._seq)
            if self.store is not None:
                self.store.fake_put("__meta__", {"next": n + 1})
            return n

    def _save(self, inv: dict) -> None:
        self.invoices[inv["id"]] = inv
        if self.store is not None:
            self.store.fake_put(inv["id"], inv)

    def _get(self, invoice_id: str) -> dict:
        inv = self.invoices.get(invoice_id)
        if inv is None and self.store is not None:
            inv = self.store.fake_get(invoice_id)
            if inv is not None and not invoice_id.startswith("__"):
                self.invoices[invoice_id] = inv
        if inv is None or invoice_id.startswith("__"):
            raise PayPalError("PayPal 404: INVOICE_NOT_FOUND")
        return inv

    def create_invoice(self, recipient_name, recipient_email, items, note="", reference=""):
        n = self._next()
        total = sum(float(i["quantity"]) * float(i["unit_price"]) for i in items)
        given, _, surname = recipient_name.partition(" ")
        inv = {
            "id": f"INV2-DEMO-{n:04d}",
            "status": "DRAFT",
            "detail": {"invoice_number": f"{n:04d}", "currency_code": "USD", "note": note, "reference": reference or None},
            "primary_recipients": [{"billing_info": {"name": {"given_name": given, "surname": surname}, "email_address": recipient_email}}],
            "items": [{"name": i["name"], "quantity": str(i["quantity"]), "unit_amount": {"currency_code": "USD", "value": f"{float(i['unit_price']):.2f}"}} for i in items],
            "amount": {"value": f"{total:.2f}", "currency_code": "USD"},
            "due_amount": {"value": f"{total:.2f}", "currency_code": "USD"},
            "links": [],
        }
        self._save(inv)
        return inv

    def send_invoice(self, invoice_id):
        inv = self._get(invoice_id)
        if inv["status"] != "DRAFT":
            raise PayPalError("PayPal 422: INVOICE_ALREADY_SENT")
        inv["status"] = "UNPAID"
        inv["links"] = [{"rel": "payer-view", "href": f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"}]
        self._save(inv)
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
        self._save(inv)
        return {}

    def record_payment(self, invoice_id, amount, method="CASH", note=""):
        inv = self._get(invoice_id)
        if inv["status"] in {"DRAFT", "CANCELLED"} | PAID_STATUSES:
            raise PayPalError("PayPal 422: CANNOT_RECORD_PAYMENT")
        due = float(inv["due_amount"]["value"])
        if float(amount) > due + 1e-9:
            raise PayPalError("PayPal 422: CANT_PAY_MORE_THAN_INVOICE_AMOUNT")
        due -= float(amount)
        inv["due_amount"]["value"] = f"{due:.2f}"
        inv["status"] = "MARKED_AS_PAID" if due < 0.005 else "PARTIALLY_PAID"
        self._save(inv)
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
        self._save(inv)

    # --- payouts -------------------------------------------------------------
    def create_payout(self, sender_batch_id, receiver_email, amount, note="", subject=""):
        with self._lock:
            if sender_batch_id in self.payouts:
                raise PayPalError("PayPal 400: SENDER_BATCH_ID_ALREADY_USED")
            batch = {"batch_header": {"payout_batch_id": f"DEMO-BATCH-{len(self.payouts) + 1:04d}", "batch_status": "SUCCESS",
                                      "sender_batch_header": {"sender_batch_id": sender_batch_id}},
                     "receiver": receiver_email, "amount": f"{float(amount):.2f}"}
            self.payouts[sender_batch_id] = batch
            return batch

    def get_payout(self, payout_batch_id):
        for b in self.payouts.values():
            if b["batch_header"]["payout_batch_id"] == payout_batch_id:
                return b
        raise PayPalError("PayPal 404: BATCH_NOT_FOUND")
