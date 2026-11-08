"""Per-session isolation: each visitor only sees and acts on the invoices they created.

All demo visitors share one PayPal sandbox merchant, so the session remembers the ids of
the invoices it created (and tags them with a reference); every read or action checks it.
"""

from __future__ import annotations

import time

from .paypal import FINAL_STATUSES, PayPalError

MAX_INVOICES_PER_SESSION = 30
LIST_CACHE_SECONDS = 3.0  # the page polls every few seconds; don't hit PayPal more often than this


class ScopedBackend:
    def __init__(self, backend, session_tag: str) -> None:
        self.backend = backend
        self.tag = session_tag
        self.currency = getattr(backend, "currency", "USD")
        self.ids: list[str] = []
        self._final: dict[str, dict] = {}  # invoices that can no longer change: no need to ask PayPal again
        self._listed: tuple[float, list[dict]] | None = None

    def _changed(self) -> None:
        self._listed = None

    def _check_owned(self, invoice_id: str) -> None:
        if invoice_id not in self.ids:
            raise PayPalError("That invoice does not belong to this session. Use list_invoices to find the right id.")

    def create_invoice(self, recipient_name, recipient_email, items, note=""):
        if len(self.ids) >= MAX_INVOICES_PER_SESSION:
            raise PayPalError("Demo limit reached: this session already created 30 invoices.")
        inv = self.backend.create_invoice(recipient_name, recipient_email, items, note, reference=self.tag)
        self.ids.append(inv["id"])
        self._changed()
        return inv

    def send_invoice(self, invoice_id):
        self._check_owned(invoice_id)
        self._changed()
        return self.backend.send_invoice(invoice_id)

    def get_invoice(self, invoice_id):
        self._check_owned(invoice_id)
        return self.backend.get_invoice(invoice_id)

    def list_invoices(self):
        # Fetch each owned invoice by id: the shared merchant's list endpoint would mix other sessions' invoices.
        if self._listed and time.time() - self._listed[0] < LIST_CACHE_SECONDS:
            return self._listed[1]
        out = []
        for invoice_id in list(self.ids):
            inv = self._final.get(invoice_id) or self.backend.get_invoice(invoice_id)
            if inv.get("status") in FINAL_STATUSES:
                self._final[invoice_id] = inv
            out.append(inv)
        self._listed = (time.time(), out)
        return out

    def remind_invoice(self, invoice_id, note=""):
        self._check_owned(invoice_id)
        self._changed()
        return self.backend.remind_invoice(invoice_id, note)

    def cancel_invoice(self, invoice_id, note=""):
        self._check_owned(invoice_id)
        self._changed()
        return self.backend.cancel_invoice(invoice_id, note)

    def record_payment(self, invoice_id, amount, method="CASH", note=""):
        self._check_owned(invoice_id)
        self._changed()
        return self.backend.record_payment(invoice_id, amount, method, note)

    def qr_code(self, invoice_id):
        self._check_owned(invoice_id)
        return self.backend.qr_code(invoice_id)

    def mark_paid(self, invoice_id):  # demo mode only: simulates the customer paying online
        self._check_owned(invoice_id)
        self._changed()
        return self.backend.mark_paid(invoice_id)
