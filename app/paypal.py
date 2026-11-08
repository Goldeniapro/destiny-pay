"""Minimal PayPal Invoicing v2 client (sandbox by default)."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import httpx

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
LIVE_URL = "https://api-m.paypal.com"


class PayPalError(RuntimeError):
    pass


@dataclass
class PayPalClient:
    client_id: str
    client_secret: str
    base_url: str = SANDBOX_URL
    currency: str = "USD"  # PayPal does not support PEN
    transport: httpx.BaseTransport | None = None

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.base_url, timeout=30, transport=self.transport)
        self._token: str | None = None
        self._token_expiry = 0.0

    @classmethod
    def from_env(cls) -> "PayPalClient":
        cid, secret = os.environ.get("PAYPAL_CLIENT_ID"), os.environ.get("PAYPAL_CLIENT_SECRET")
        if not cid or not secret:
            raise PayPalError("Missing PAYPAL_CLIENT_ID / PAYPAL_CLIENT_SECRET")
        live = os.environ.get("PAYPAL_ENV", "sandbox") == "live"
        return cls(cid, secret, LIVE_URL if live else SANDBOX_URL)

    # --- auth -------------------------------------------------------------
    def _auth_header(self) -> dict[str, str]:
        if not self._token or time.time() > self._token_expiry - 60:
            r = self._http.post(
                "/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
            )
            self._check(r)
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3000))
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kw) -> dict:
        headers = {**self._auth_header(), "Content-Type": "application/json", "Prefer": "return=representation"}
        r = self._http.request(method, path, headers=headers, **kw)
        self._check(r)
        return r.json() if r.content else {}

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {r.status_code}: {r.text[:500]}")

    # --- invoicing --------------------------------------------------------
    def create_invoice(self, recipient_name: str, recipient_email: str, items: list[dict], note: str = "") -> dict:
        """items: [{"name": str, "quantity": number, "unit_price": number}]"""
        number = self._request("POST", "/v2/invoicing/generate-next-invoice-number").get("invoice_number")
        given, _, surname = recipient_name.strip().partition(" ")
        body = {
            "detail": {"currency_code": self.currency, "note": note or None, "invoice_number": number},
            "primary_recipients": [
                {"billing_info": {"name": {"given_name": given, "surname": surname or None}, "email_address": recipient_email}}
            ],
            "items": [
                {
                    "name": it["name"],
                    "quantity": str(it.get("quantity", 1)),
                    "unit_amount": {"currency_code": self.currency, "value": f"{float(it['unit_price']):.2f}"},
                }
                for it in items
            ],
        }
        return self._request("POST", "/v2/invoicing/invoices", json=_drop_none(body))

    def send_invoice(self, invoice_id: str) -> dict:
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
        )

    def get_invoice(self, invoice_id: str) -> dict:
        return self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")

    def list_invoices(self, page_size: int = 20) -> list[dict]:
        data = self._request("GET", "/v2/invoicing/invoices", params={"page": 1, "page_size": page_size, "total_required": "true"})
        return data.get("items", [])

    def remind_invoice(self, invoice_id: str, note: str = "") -> dict:
        return self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json=_drop_none({"note": note or None}))

    def cancel_invoice(self, invoice_id: str, note: str = "") -> dict:
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
            json=_drop_none({"note": note or None, "send_to_recipient": True}),
        )


def summarize(inv: dict) -> dict:
    """Compact view of an invoice for the model and the UI."""
    detail = inv.get("detail", {})
    rec = (inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
    name = " ".join(filter(None, [rec.get("name", {}).get("given_name"), rec.get("name", {}).get("surname")]))
    amount = inv.get("amount", {})
    payer_link = next((l["href"] for l in inv.get("links", []) if l.get("rel") == "payer-view"), None)
    return {
        "id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "status": inv.get("status"),
        "recipient": name or rec.get("email_address"),
        "email": rec.get("email_address"),
        "total": f'{amount.get("value", "?")} {amount.get("currency_code", "")}'.strip(),
        "payer_link": payer_link,
    }


def _drop_none(obj):
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj
