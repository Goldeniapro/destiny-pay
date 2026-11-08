import json
from types import SimpleNamespace as NS

import httpx

from app.agent import DestinyPayAgent, run_tool
from app.fake import FakePayPal
from app.paypal import PayPalClient, summarize


def test_paypal_client_create_send_flow():
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.url.path == "/v1/oauth2/token":
            assert req.headers["authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert req.headers["authorization"] == "Bearer tok"
        if req.url.path == "/v2/invoicing/generate-next-invoice-number":
            return httpx.Response(200, json={"invoice_number": "0001"})
        if req.url.path == "/v2/invoicing/invoices" and req.method == "POST":
            body = json.loads(req.content)
            assert body["detail"]["currency_code"] == "USD"
            assert body["items"][0]["unit_amount"]["value"] == "12.00"
            assert body["primary_recipients"][0]["billing_info"]["email_address"] == "juan@correo.com"
            return httpx.Response(201, json={"id": "INV2-1", "status": "DRAFT"})
        if req.url.path == "/v2/invoicing/invoices/INV2-1/send":
            return httpx.Response(200, json={"links": [{"rel": "payer-view", "href": "https://pay"}]})
        return httpx.Response(404, json={})

    pp = PayPalClient("id", "secret", transport=httpx.MockTransport(handler))
    inv = pp.create_invoice("Juan Pérez", "juan@correo.com", [{"name": "Té", "quantity": 3, "unit_price": 12}])
    pp.send_invoice(inv["id"])
    assert [p for _, p in calls].count("/v1/oauth2/token") == 1  # token cached


def test_run_tool_with_fake_backend():
    fake = FakePayPal()
    out = json.loads(run_tool(fake, "create_and_send_invoice", {
        "recipient_name": "Juan Pérez", "recipient_email": "juan@correo.com",
        "items": [{"name": "Té", "quantity": 3, "unit_price": 12}], "note": "",
    }))
    assert out["status"] == "SENT" and out["total"] == "36.00 USD" and out["payer_link"]
    listed = json.loads(run_tool(fake, "list_invoices", {}))
    assert listed[0]["recipient"] == "Juan Pérez"
    run_tool(fake, "cancel_invoice", {"invoice_id": out["id"], "note": ""})
    assert summarize(fake.get_invoice(out["id"]))["status"] == "CANCELLED"


class ScriptedClient:
    """Mimics client.beta.messages.create with canned responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append(kw)
        return self.responses.pop(0)


def test_agent_tool_loop_and_history():
    fake = FakePayPal()
    tool_call = NS(type="tool_use", id="tu_1", name="create_and_send_invoice", input={
        "recipient_name": "Ana Ruiz", "recipient_email": "ana@correo.com",
        "items": [{"name": "Caja", "quantity": 2, "unit_price": 10}], "note": "",
    })
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[tool_call]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="Listo, le envié a Ana un cobro de 20 dólares.")]),
    ])
    agent = DestinyPayAgent(fake, client=client)
    res = agent.chat("s1", "Cóbrale a Ana Ruiz 2 cajas a 10, ana@correo.com, confirmado")
    assert "20" in res["reply"]
    assert res["actions"][0]["result"]["status"] == "SENT"
    msgs = agent.sessions["s1"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[2]["content"][0]["type"] == "tool_result"
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert all(t["strict"] for t in req["tools"])


def test_agent_tool_error_is_reported_not_raised():
    fake = FakePayPal()
    bad = NS(type="tool_use", id="tu_x", name="get_invoice", input={"invoice_id": "NOPE"})
    client = ScriptedClient([
        NS(stop_reason="tool_use", content=[bad]),
        NS(stop_reason="end_turn", content=[NS(type="text", text="No encontré esa factura.")]),
    ])
    agent = DestinyPayAgent(fake, client=client)
    res = agent.chat("s2", "¿Ya pagó la factura NOPE?")
    assert res["actions"][0]["error"]
    tool_result = agent.sessions["s2"][2]["content"][0]
    assert tool_result["is_error"] is True
