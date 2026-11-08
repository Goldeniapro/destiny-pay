import httpx
import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace as NS

from app import main
from app.agent import DestinyPayAgent
from app.voice import Voice


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.beta = NS(messages=NS(create=lambda **kw: self.responses.pop(0)))


def text(t):
    return NS(stop_reason="end_turn", content=[NS(type="text", text=t)], usage=None)


@pytest.fixture
def api(monkeypatch):
    for name in list(main.limits):
        monkeypatch.setitem(main.limits, name, main.RateLimiter(1000))
    fresh = DestinyPayAgent(main.business_factory, client=Scripted([]))
    monkeypatch.setattr(main, "agent", fresh)
    return TestClient(main.app), fresh


def new_ws(client, lang="es", sample=True):
    r = client.post("/api/workspace", json={"lang": lang, "tz": "America/Lima", "sample": sample})
    assert r.status_code == 200
    return r.json()["workspace_id"]


def test_status_and_page(api):
    client, _ = api
    s = client.get("/api/status").json()
    assert s["demo_mode"] and s["langs"] == ["es", "en", "pt", "zh", "fr"] and s["voice"] == {"tts": False, "stt": False}
    assert "Destiny" in client.get("/").text


def test_workspace_state_and_briefing(api):
    client, _ = api
    ws = new_ws(client, "en")
    st = client.get("/api/state", params={"ws": ws, "lang": "en"}).json()
    assert st["business"]["name"] == "Inti Crafts" and len(st["bills"]) == 4 and len(st["contacts"]) == 4
    assert len(st["cashflow"]) == 30 and st["overview"]["bills_next_7_days"] > 0
    b = client.get("/api/briefing", params={"ws": ws, "lang": "en"}).json()
    assert "Textiles Andinos" in b["text"] and "overdue bill: Empaques Lima" in b["text"] and b["speech_id"] is None  # no voice key: the browser speaks
    assert client.get("/api/state", params={"ws": "0" * 32}).status_code == 404
    assert client.get("/api/state", params={"ws": "not-a-workspace"}).status_code == 422


def test_chat_confirm_and_alerts(api):
    client, agent = api
    ws = new_ws(client)
    from app.business import Business
    biz = main.business_factory(ws)
    inv = biz.draft_invoice("Sarah Miller", "", [{"name": "Chompa", "quantity": 1, "unit_price": 45}], "")
    agent._client.responses += [NS(stop_reason="tool_use", usage=None, content=[NS(type="tool_use", id="t1", name="request_send_invoice", input={"invoice_id": inv["id"]})]),
                                text("¿Confirmas?")]
    r = client.post("/api/chat", json={"workspace_id": ws, "conversation_id": "conv-api-1", "message": "envíale la factura a Sarah", "lang": "es"}).json()
    cid = r["ui"][0]["confirmation"]["confirmation_id"]
    st = client.get("/api/state", params={"ws": ws}).json()
    assert st["confirmations"][0]["id"] == cid
    c = client.post("/api/confirm", json={"workspace_id": ws, "conversation_id": "conv-api-1", "confirmation_id": cid, "approve": True, "lang": "es"}).json()
    assert c["done"] and "Sarah Miller" in c["reply"]
    assert client.post(f"/api/demo/pay/{inv['id']}", params={"ws": ws}).status_code == 200
    st = client.get("/api/state", params={"ws": ws, "lang": "es", "since": 0}).json()
    assert st["alerts"][-1]["text"].startswith("¡Sarah Miller te pagó")
    assert client.get("/api/state", params={"ws": ws, "since": st["last_alert_id"]}).json()["alerts"] == []


def test_isolation_between_workspaces(api):
    client, _ = api
    a, b = new_ws(client, sample=False), new_ws(client, sample=False)
    inv = main.business_factory(a).draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 5}], "")
    assert client.post(f"/api/demo/pay/{inv['id']}", params={"ws": b}).status_code == 404
    assert client.get("/api/state", params={"ws": b}).json()["invoices"] == []


def test_validation_and_limits(api, monkeypatch):
    client, agent = api
    ws = new_ws(client)
    assert client.post("/api/chat", json={"workspace_id": ws, "conversation_id": "bad id!", "message": "hi"}).status_code == 422
    assert client.post("/api/chat", json={"workspace_id": ws, "conversation_id": "conv-ok-1", "message": ""}).status_code == 422
    monkeypatch.setitem(main.limits, "chat", main.RateLimiter(2))
    agent._client.responses += [text("ok"), text("ok")]
    codes = [client.post("/api/chat", json={"workspace_id": ws, "conversation_id": "conv-ok-1", "message": "hola"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    monkeypatch.setitem(main.limits, "workspace", main.RateLimiter(0))
    assert client.post("/api/workspace", json={}).status_code == 429


def test_webhook_requires_configuration_and_signature(api, monkeypatch):
    client, _ = api
    assert client.post("/api/paypal/webhook", json={"event_type": "INVOICING.INVOICE.PAID"}).status_code == 404
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main, "WEBHOOK_ID", "WH")
    monkeypatch.setattr(main.paypal, "verify_webhook", lambda h, e, w: False, raising=False)
    assert client.post("/api/paypal/webhook", json={"event_type": "INVOICING.INVOICE.PAID"}).status_code == 400


def test_webhook_marks_invoice_paid(api, monkeypatch):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    inv = biz.draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 5}], "")
    biz.send_invoice(inv["id"])
    main.paypal.mark_paid(inv["id"])  # PayPal's side changed
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main, "WEBHOOK_ID", "WH")
    monkeypatch.setattr(main.paypal, "verify_webhook", lambda h, e, w: True, raising=False)
    r = client.post("/api/paypal/webhook", json={"event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": inv["id"]}}})
    assert r.status_code == 200 and biz.invoice_view(inv["id"])["status"] == "PAID"


def test_speech_endpoint_streams_only_registered_text(api, monkeypatch):
    client, _ = api

    def fish(req):
        assert req.headers["authorization"] == "Bearer k" and req.headers["model"] == "s2.1-pro"
        return httpx.Response(200, content=b"ID3mp3bytes")

    v = Voice(api_key="k", voice_id="voice1", transport=httpx.MockTransport(fish))
    monkeypatch.setattr(main, "voice", v)
    sid = v.register("Hola, soy Destiny Pay.", "es")
    r = client.get(f"/api/speech/{sid}")
    assert r.status_code == 200 and r.content == b"ID3mp3bytes" and r.headers["content-type"] == "audio/mpeg"
    assert client.get("/api/speech/made-up-id").status_code == 404


def test_scheduler_tick_fires_reminders_and_refreshes(api):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    import time
    from datetime import timedelta
    r = biz.create_reminder("Pagar luz", (biz.now() + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M"))
    main.store.x("UPDATE reminders SET due_at=? WHERE id=?", (time.time() - 1, r["id"]))
    biz.add_bill("Luz del Sur", "Luz", 30, (biz.today() + timedelta(days=1)).isoformat())
    main.scheduler_tick()
    keys = [a["key"] for a in main.store.alerts_since(ws, 0)]
    assert "a_reminder" in keys and "a_bill_due" in keys
    main.scheduler_tick()
    assert len(main.store.alerts_since(ws, 0)) == len(keys)  # nothing fires twice
