"""Per-session isolation: each visitor only sees and acts on the invoices they created.

All demo visitors share one PayPal sandbox merchant, so the session remembers the ids of
the invoices it created (and tags them with a reference); every read or action checks it.
"""

from __future__ import annotations

from .paypal import FINAL_STATUSES, PayPalError

MAX_INVOICES_PER_SESSION = 30


class ScopedBackend:
    def __init__(self, backend, session_tag: str) -> None:
        self.backend = backend
        self.tag = session_tag
        self.currency = getattr(backend, "currency", "USD")
        self.ids: list[str] = []
        self._final: dict[str, dict] = {}  # invoices that can no longer change: no need to ask PayPal again

    def _check_owned(self, invoice_id: str) -> None:
        if invoice_id not in self.ids:
            raise PayPalError("That invoice does not belong to this session. Use list_invoices to find the right id.")

    def create_invoice(self, recipient_name, recipient_email, items, note=""):
        if len(self.ids) >= MAX_INVOICES_PER_SESSION:
            raise PayPalError("Demo limit reached: this session already created 30 invoices.")
        inv = self.backend.create_invoice(recipient_name, recipient_email, items, note, reference=self.tag)
        self.ids.append(inv["id"])
        return inv

    def send_invoice(self, invoice_id):
        self._check_owned(invoice_id)
        return self.backend.send_invoice(invoice_id)

    def get_invoice(self, invoice_id):
        self._check_owned(invoice_id)
        return self.backend.get_invoice(invoice_id)

    def list_invoices(self):
        # Fetch each owned invoice by id: the shared merchant's list endpoint would mix other sessions' invoices.
        out = []
        for invoice_id in list(self.ids):
            inv = self._final.get(invoice_id) or self.backend.get_invoice(invoice_id)
            if inv.get("status") in FINAL_STATUSES:
                self._final[invoice_id] = inv
            out.append(inv)
        return out

    def remind_invoice(self, invoice_id, note=""):
        self._check_owned(invoice_id)
        return self.backend.remind_invoice(invoice_id, note)

    def cancel_invoice(self, invoice_id, note=""):
        self._check_owned(invoice_id)
        return self.backend.cancel_invoice(invoice_id, note)

    def record_payment(self, invoice_id, amount, method="CASH", note=""):
        self._check_owned(invoice_id)
        return self.backend.record_payment(invoice_id, amount, method, note)

    def qr_code(self, invoice_id):
        self._check_owned(invoice_id)
        return self.backend.qr_code(invoice_id)
