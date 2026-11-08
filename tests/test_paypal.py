import json

import httpx
import pytest

from app.paypal import PayPalClient, PayPalError, summarize

ITEMS = [{"name": "Té", "quantity": 3, "unit_price": 12}]


# ---------------------------------------------------------------- PayPal client
def make_paypal(handler, **kw):
    return PayPalClient("id", "secret", transport=httpx.MockTransport(handler), retry_delay=0, **kw)


def test_paypal_create_send_flow_caches_token_and_sends_reference():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/v1/oauth2/token":
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path == "/v2/invoicing/invoices" and req.method == "POST":
            assert req.headers["prefer"] == "return=representation"
            assert req.headers["paypal-request-id"]
            body = json.loads(req.content)
            assert body["detail"]["currency_code"] == "USD"
            assert body["detail"]["reference"] == "dp-session"
            assert body["items"][0]["unit_amount"]["value"] == "12.00"
            assert body["items"][0]["quantity"] == "3"
            assert body["primary_recipients"][0]["billing_info"]["email_address"] == "juan@correo.com"
            assert "note" not in body["detail"]  # None values are dropped
            assert "invoice_number" not in body["detail"]  # PayPal auto-numbers: no race between sessions
            return httpx.Response(201, json={"id": "INV2-1", "status": "DRAFT"})
        if req.url.path == "/v2/invoicing/invoices/INV2-1/send":
            return httpx.Response(200, json={"links": [{"rel": "payer-view", "href": "https://pay"}]})
        return httpx.Response(404, json={})

    pp = make_paypal(handler)
    inv = pp.create_invoice("Juan Pérez", "juan@correo.com", ITEMS, reference="dp-session")
    pp.send_invoice(inv["id"])
    assert [p for _, p in calls].count("/v1/oauth2/token") == 1


def test_paypal_refreshes_token_on_401_and_retries_5xx():
    tokens, hits = iter(["t1", "t2"]), {"n": 0}

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        hits["n"] += 1
        if req.headers["authorization"] == "Bearer t1":
            return httpx.Response(401, json={"error": "invalid_token"})
        if hits["n"] == 2:
            return httpx.Response(503, text="busy")
        return httpx.Response(200, json={"id": "INV2-9", "status": "PAID"})

    assert make_paypal(handler).get_invoice("INV2-9")["status"] == "PAID"
    assert hits["n"] == 3


def test_paypal_retries_reuse_the_same_request_id():
    seen = []

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        seen.append(req.headers["paypal-request-id"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"payment_id": "P1"})

    make_paypal(handler).record_payment("INV2-1", 5)
    assert len(seen) == 2 and seen[0] == seen[1]


def test_paypal_create_falls_back_to_explicit_number():
    bodies = []

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if req.url.path.endswith("generate-next-invoice-number"):
            return httpx.Response(200, json={"invoice_number": "0042"})
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return httpx.Response(422, json={"details": [{"field": "/detail/invoice_number", "issue": "MISSING"}]})
        return httpx.Response(201, json={"id": "INV2-2", "status": "DRAFT"})

    assert make_paypal(handler).create_invoice("Ana", "ana@correo.com", ITEMS)["id"] == "INV2-2"
    assert bodies[1]["detail"]["invoice_number"] == "0042"


def test_paypal_network_error_becomes_paypal_error():
    def handler(req):
        raise httpx.ConnectError("down")

    with pytest.raises(PayPalError, match="Could not reach PayPal"):
        make_paypal(handler).get_invoice("X")


def test_paypal_4xx_is_reported():
    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY"})

    with pytest.raises(PayPalError, match="PayPal 422"):
        make_paypal(handler).send_invoice("X")


def test_paypal_qr_accepts_multipart_body():
    b64 = "iVBORw0KGgo" + "A" * 300

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        body = f"--boundary\r\nContent-Type: text/plain\r\n\r\n{b64[:150]}\r\n{b64[150:]}\r\n--boundary--"
        return httpx.Response(200, text=body, headers={"content-type": "multipart/related; boundary=boundary"})

    assert make_paypal(handler).qr_code("INV2-1") == b64


def test_live_mode_is_blocked_without_explicit_opt_in(monkeypatch):
    monkeypatch.setenv("PAYPAL_CLIENT_ID", "x")
    monkeypatch.setenv("PAYPAL_CLIENT_SECRET", "y")
    monkeypatch.setenv("PAYPAL_ENV", "live")
    monkeypatch.delenv("ALLOW_LIVE", raising=False)
    with pytest.raises(PayPalError, match="Live mode is disabled"):
        PayPalClient.from_env()


def test_summarize_payer_link_rules():
    base = {"id": "I", "detail": {"metadata": {"recipient_view_url": "https://www.sandbox.paypal.com/invoice/p/#I"}}}
    assert summarize({**base, "status": "UNPAID"})["payer_link"].startswith("https://")
    assert summarize({**base, "status": "DRAFT"})["payer_link"] is None
    bad = {"id": "I", "status": "UNPAID", "links": [{"rel": "payer-view", "href": "javascript:alert(1)"}]}
    assert summarize(bad)["payer_link"] is None


def test_payout_body_and_webhook_verification():
    seen = {}

    def handler(req):
        if req.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        body = json.loads(req.content)
        seen[req.url.path] = body
        if req.url.path == "/v1/payments/payouts":
            return httpx.Response(201, json={"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    pp = make_paypal(handler)
    out = pp.create_payout("dpabc", "supplier@example.com", 180, note="Yarn")
    item = seen["/v1/payments/payouts"]["items"][0]
    assert out["batch_header"]["payout_batch_id"] == "B1"
    assert item["receiver"] == "supplier@example.com" and item["amount"] == {"value": "180.00", "currency": "USD"}
    assert seen["/v1/payments/payouts"]["sender_batch_header"]["sender_batch_id"] == "dpabc"
    headers = {"PAYPAL-AUTH-ALGO": "SHA256withRSA", "PAYPAL-CERT-URL": "https://api.paypal.com/cert", "PAYPAL-TRANSMISSION-ID": "t1",
               "PAYPAL-TRANSMISSION-SIG": "sig", "PAYPAL-TRANSMISSION-TIME": "2026-11-08T00:00:00Z"}
    assert pp.verify_webhook(headers, {"id": "WH-1"}, "WEBHOOK1") is True
    assert seen["/v1/notifications/verify-webhook-signature"]["webhook_id"] == "WEBHOOK1"
    assert pp.verify_webhook({}, {"id": "WH-1"}, "WEBHOOK1") is False  # missing headers: rejected without asking PayPal
