"""Minimal PayPal Invoicing v2 client (sandbox by default)."""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date

import httpx

SANDBOX_URL = "https://api-m.sandbox.paypal.com"
LIVE_URL = "https://api-m.paypal.com"

UNPAID_STATUSES = {"SENT", "UNPAID", "PARTIALLY_PAID", "PAYMENT_PENDING", "SCHEDULED"}
PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}
FINAL_STATUSES = PAID_STATUSES | {"CANCELLED", "REFUNDED", "MARKED_AS_REFUNDED"}
PAYMENT_METHODS = {"CASH", "BANK_TRANSFER", "CHECK", "CREDIT_CARD", "DEBIT_CARD", "PAYPAL", "WIRE_TRANSFER", "OTHER"}


class PayPalError(RuntimeError):
    pass


@dataclass
class PayPalClient:
    client_id: str
    client_secret: str
    base_url: str = SANDBOX_URL
    currency: str = "USD"  # PayPal Invoicing does not support PEN, COP, CLP or ARS
    transport: httpx.BaseTransport | None = None
    retry_delay: float = 1.0

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
        if live and os.environ.get("ALLOW_LIVE") != "1":
            raise PayPalError("Live mode is disabled. This demo only runs against the PayPal sandbox.")
        currency = os.environ.get("DP_CURRENCY", "USD").upper()
        return cls(cid, secret, LIVE_URL if live else SANDBOX_URL, currency=currency)

    # --- transport ----------------------------------------------------------
    def _token_header(self, force: bool = False) -> dict[str, str]:
        if force or not self._token or time.time() > self._token_expiry - 60:
            try:
                r = self._http.post(
                    "/v1/oauth2/token",
                    auth=(self.client_id, self.client_secret),
                    data={"grant_type": "client_credentials"},
                )
            except httpx.HTTPError as e:
                raise PayPalError(f"Could not reach PayPal: {e.__class__.__name__}") from e
            self._check(r)
            body = r.json()
            self._token = body["access_token"]
            self._token_expiry = time.time() + int(body.get("expires_in", 3000))
        return {"Authorization": f"Bearer {self._token}"}

    def _request(self, method: str, path: str, **kw) -> dict | str:
        headers = {"Content-Type": "application/json"}
        if method != "GET":
            headers["Prefer"] = "return=representation"
            # Same id on every retry: if PayPal already processed a request that timed out,
            # the retry does not create a second invoice or record a payment twice.
            headers["PayPal-Request-Id"] = str(uuid.uuid4())
        refreshed = force_token = False
        for attempt in range(3):
            try:
                auth = self._token_header(force=force_token)
                force_token = False
                r = self._http.request(method, path, headers={**headers, **auth}, **kw)
            except httpx.HTTPError as e:
                if attempt == 2:
                    raise PayPalError(f"Could not reach PayPal: {e.__class__.__name__}") from e
                time.sleep(self.retry_delay * (attempt + 1))
                continue
            if r.status_code == 401 and not refreshed:
                refreshed = force_token = True  # token revoked or expired early: retry once with a fresh one
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(self.retry_delay * (attempt + 1))
                continue
            break
        self._check(r)
        if not r.content:
            return {}
        if "json" in r.headers.get("content-type", ""):
            return r.json()
        return r.text

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code >= 400:
            raise PayPalError(f"PayPal {r.status_code}: {r.text[:400]}")

    # --- invoicing ----------------------------------------------------------
    def create_invoice(self, recipient_name: str, recipient_email: str, items: list[dict], note: str = "", reference: str = "") -> dict:
        """items: [{"name": str, "quantity": number, "unit_price": number}]

        The invoice number is left to PayPal, which auto-increments it: asking for the next number first
        would race when two sessions create invoices at the same time.
        """
        given, _, surname = recipient_name.strip().partition(" ")
        body = {
            "detail": {
                "currency_code": self.currency,
                "note": note or None,
                "reference": reference or None,
            },
            "primary_recipients": [
                {"billing_info": {"name": {"given_name": given, "surname": surname or None}, "email_address": recipient_email}}
            ],
            "items": [
                {
                    "name": it["name"][:200],
                    "quantity": f"{float(it['quantity']):g}",
                    "unit_amount": {"currency_code": self.currency, "value": f"{float(it['unit_price']):.2f}"},
                }
                for it in items
            ],
        }
        body = _drop_none(body)
        try:
            return self._request("POST", "/v2/invoicing/invoices", json=body)
        except PayPalError as e:
            if "invoice_number" not in str(e).lower():
                raise
            # Fallback in case this account requires an explicit number.
            body["detail"]["invoice_number"] = self._request("POST", "/v2/invoicing/generate-next-invoice-number").get("invoice_number")
            return self._request("POST", "/v2/invoicing/invoices", json=body)

    def send_invoice(self, invoice_id: str) -> dict:
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/send",
            json={"send_to_invoicer": False, "send_to_recipient": True},
        )

    def get_invoice(self, invoice_id: str) -> dict:
        return self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")

    def list_invoices(self, page_size: int = 50) -> list[dict]:
        data = self._request("GET", "/v2/invoicing/invoices", params={"page": 1, "page_size": page_size, "total_required": "true"})
        return data.get("items", []) if isinstance(data, dict) else []

    def remind_invoice(self, invoice_id: str, note: str = "") -> dict:
        return self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json=_drop_none({"note": note or None}))

    def cancel_invoice(self, invoice_id: str, note: str = "") -> dict:
        return self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/cancel",
            json=_drop_none({"note": note or None, "send_to_recipient": True}),
        )

    def record_payment(self, invoice_id: str, amount: float, method: str = "CASH", note: str = "") -> dict:
        """Record a payment received outside PayPal (cash, bank transfer, mobile wallet...)."""
        body = {
            "method": method if method in PAYMENT_METHODS else "OTHER",
            "payment_date": date.today().isoformat(),
            "amount": {"currency_code": self.currency, "value": f"{float(amount):.2f}"},
            "note": note or None,
        }
        return self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/payments", json=_drop_none(body))

    def qr_code(self, invoice_id: str, size: int = 300) -> str:
        """Base64 PNG of the invoice payment QR (invoice must be sent)."""
        out = self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/generate-qr-code", json={"width": size, "height": size, "action": "pay"})
        if isinstance(out, dict):
            return out.get("image") or out.get("qr_code") or ""
        return _extract_base64(out)

    # --- payouts (pay a supplier by email) -----------------------------------------
    def create_payout(self, sender_batch_id: str, receiver_email: str, amount: float, note: str = "", subject: str = "") -> dict:
        """One-recipient payout. PayPal refuses a repeated sender_batch_id, so a bill can never be paid twice."""
        body = {
            "sender_batch_header": {
                "sender_batch_id": sender_batch_id,
                "email_subject": (subject or "You have a payment")[:255],
                "email_message": (note or "Payment sent with Destiny Pay")[:1000],
            },
            "items": [{
                "recipient_type": "EMAIL",
                "receiver": receiver_email,
                "amount": {"value": f"{float(amount):.2f}", "currency": self.currency},
                "note": (note or "Payment sent with Destiny Pay")[:4000],
                "sender_item_id": sender_batch_id[:63],
            }],
        }
        return self._request("POST", "/v1/payments/payouts", json=body)

    def get_payout(self, payout_batch_id: str) -> dict:
        return self._request("GET", f"/v1/payments/payouts/{payout_batch_id}")

    # --- webhooks ------------------------------------------------------------
    def verify_webhook(self, headers: dict, event: dict, webhook_id: str) -> bool:
        """Ask PayPal whether a webhook delivery is genuine (headers are case-insensitive)."""
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo", ""),
            "cert_url": h.get("paypal-cert-url", ""),
            "transmission_id": h.get("paypal-transmission-id", ""),
            "transmission_sig": h.get("paypal-transmission-sig", ""),
            "transmission_time": h.get("paypal-transmission-time", ""),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        if not all(body[k] for k in ("auth_algo", "cert_url", "transmission_id", "transmission_sig", "transmission_time", "webhook_id")):
            return False
        out = self._request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return isinstance(out, dict) and out.get("verification_status") == "SUCCESS"


def summarize(inv: dict) -> dict:
    """Compact view of an invoice for the model and the UI."""
    detail = inv.get("detail", {})
    rec = (inv.get("primary_recipients") or [{}])[0].get("billing_info", {})
    name = " ".join(filter(None, [rec.get("name", {}).get("given_name"), rec.get("name", {}).get("surname")]))
    amount = inv.get("amount", {})
    due = inv.get("due_amount", {})
    payer_link = next((l.get("href") for l in inv.get("links", []) if l.get("rel") == "payer-view"), None)
    payer_link = payer_link or detail.get("metadata", {}).get("recipient_view_url")
    if not payer_link or not str(payer_link).startswith("https://") or inv.get("status") in {"DRAFT", "CANCELLED"}:
        payer_link = None
    return {
        "id": inv.get("id"),
        "number": detail.get("invoice_number"),
        "status": inv.get("status"),
        "recipient": name or rec.get("email_address"),
        "email": rec.get("email_address"),
        "total": f'{amount.get("value", "?")} {amount.get("currency_code", "")}'.strip(),
        "due": f'{due.get("value")} {due.get("currency_code", "")}'.strip() if due.get("value") else None,
        "reference": detail.get("reference"),
        "payer_link": payer_link,
    }


def _extract_base64(text: str) -> str:
    """The QR endpoint may answer with plain base64 or a multipart body; keep the longest base64 run."""
    groups, current = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if line and re.fullmatch(r"[A-Za-z0-9+/=]+", line):
            current.append(line)
        elif current:
            groups.append("".join(current))
            current = []
    if current:
        groups.append("".join(current))
    best = max(groups, key=len, default="")
    return best if len(best) >= 100 else ""


def _drop_none(obj):
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj
