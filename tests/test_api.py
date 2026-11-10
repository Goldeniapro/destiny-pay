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
    assert s["demo_mode"] and s["langs"] == ["es", "en", "pt"] and s["voice"] == {"tts": False, "stt": False}
    assert "Destiny" in client.get("/").text


def test_workspace_state_and_briefing(api):
    client, _ = api
    ws = new_ws(client, "en")
    st = client.get("/api/state", params={"lang": "en"}, headers={"x-workspace": ws}).json()
    assert st["business"]["name"] == "Inti Crafts" and len(st["bills"]) == 4 and len(st["contacts"]) == 5 and st["overview"]["receivable"] == 545
    o = st["overview"]
    assert o["cash_on_hand"] > 0 and o["projected_30_days"] == round(o["cash_on_hand"] + o["receivable"] - o["bills_next_30_days"], 2)
    assert len(st["cashflow"]) == 30 and st["overview"]["bills_next_7_days"] > 0
    b = client.get("/api/briefing", params={"lang": "en"}, headers={"x-workspace": ws}).json()
    assert "Textiles Andinos" in b["text"] and "overdue bill: Empaques Lima" in b["text"] and b["speech_id"] is None  # no voice key: the browser speaks
    assert client.get("/api/state", headers={"x-workspace": "0" * 32}).status_code == 404
    bad = client.get("/api/state", headers={"x-workspace": "not-a-workspace"})
    assert bad.status_code == 422 and "not-a-workspace" not in bad.text  # errors never echo the input
    assert "strict-transport-security" in bad.headers


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
    st = client.get("/api/state", headers={"x-workspace": ws}).json()
    assert st["confirmations"][0]["id"] == cid
    fp = next(c for c in client.get("/api/state", headers={"x-workspace": ws}).json()["confirmations"] if c["id"] == cid)["fingerprint"]
    c = client.post("/api/confirm", json={"workspace_id": ws, "conversation_id": "conv-api-1", "confirmation_id": cid, "approve": True, "lang": "es", "fingerprint": fp}).json()
    assert c["done"] and "Sarah Miller" in c["reply"]
    assert client.post(f"/api/demo/pay/{inv['id']}", headers={"x-workspace": ws}).status_code == 200
    st = client.get("/api/state", params={"lang": "es", "since": 0}, headers={"x-workspace": ws}).json()
    assert st["alerts"][-1]["text"].startswith("¡Sarah Miller te pagó")
    assert client.get("/api/state", params={"since": st["last_alert_id"]}, headers={"x-workspace": ws}).json()["alerts"] == []


def test_isolation_between_workspaces(api):
    client, _ = api
    a, b = new_ws(client, sample=False), new_ws(client, sample=False)
    inv = main.business_factory(a).draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 5}], "")
    assert client.post(f"/api/demo/pay/{inv['id']}", headers={"x-workspace": b}).status_code == 404
    assert client.get("/api/state", headers={"x-workspace": b}).json()["invoices"] == []


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
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cert = {"paypal-cert-url": "https://api.sandbox.paypal.com/v1/notifications/certs/CERT-1", "paypal-transmission-time": now}
    event = {"id": "WH-EVT-1", "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": inv["id"]}}}
    r = client.post("/api/paypal/webhook", json=event, headers=cert)
    assert r.status_code == 200 and biz.invoice_view(inv["id"])["status"] == "PAID"
    again = client.post("/api/paypal/webhook", json=event, headers=cert)  # PayPal retries: handled once
    assert again.status_code == 200 and again.json().get("duplicate") is True
    # dedupe lives in the database (survives a restart) and each business sees only its own last notification
    assert main.store.one("SELECT ws FROM webhook_events WHERE id='WH-EVT-1'")["ws"] == ws
    assert client.get("/api/state", headers={"x-workspace": ws}).json()["webhook_last"]["event"] == "INVOICING.INVOICE.PAID"
    other = new_ws(client, sample=False)
    assert client.get("/api/state", headers={"x-workspace": other}).json()["webhook_last"] == {}
    forged = client.post("/api/paypal/webhook", json={**event, "id": "WH-EVT-2"}, headers={"paypal-cert-url": "https://evil.example/cert"})
    assert forged.status_code == 400  # never even reaches PayPal's verify API
    stale = client.post("/api/paypal/webhook", json={**event, "id": "WH-EVT-3"}, headers={**cert, "paypal-transmission-time": "2020-01-01T00:00:00Z"})
    assert stale.status_code == 400  # replayed or forged delivery from long ago


def test_webhook_failure_lets_paypal_retry(api, monkeypatch):
    client, _ = api
    from datetime import datetime, timezone
    hdr = {"paypal-cert-url": "https://api.paypal.com/v1/notifications/certs/C", "paypal-transmission-time": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main, "WEBHOOK_ID", "WH")
    monkeypatch.setattr(main.paypal, "verify_webhook", lambda h, e, w: True, raising=False)
    calls, original = [], main.store.one
    monkeypatch.setattr(main.store, "one", lambda *a, **k: calls.append(1) or (_ for _ in ()).throw(RuntimeError("db down")))
    event = {"id": "WH-RETRY", "event_type": "INVOICING.INVOICE.PAID", "resource": {"invoice": {"id": "INV2-X"}}}
    with pytest.raises(RuntimeError):
        client.post("/api/paypal/webhook", json=event, headers=hdr)
    monkeypatch.setattr(main.store, "one", original)
    assert not main._seen("WH-RETRY")  # the failed delivery was not recorded as handled


def test_per_visitor_payout_and_token_allowances(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(main, "payouts_per_ip", main.AmountQuota(400))
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    a = biz.add_bill("A", "x", 300, biz.today().isoformat(), "none", "a@example.com")
    b = biz.add_bill("B", "x", 300, biz.today().isoformat(), "none", "b@example.com")
    ra, rb = biz.request_confirmation("pay_bill", str(a["id"])), biz.request_confirmation("pay_bill", str(b["id"]))
    body = {"workspace_id": ws, "conversation_id": "conv-quota", "approve": True, "lang": "es"}
    assert client.post("/api/confirm", json={**body, "confirmation_id": ra["confirmation_id"], "fingerprint": ra["fingerprint"]}).status_code == 200
    assert client.post("/api/confirm", json={**body, "confirmation_id": rb["confirmation_id"], "fingerprint": rb["fingerprint"]}).status_code == 429  # 600 > 400
    assert biz.bill_view(b["id"])["status"] == "pending"


def test_ipv6_visitors_counted_per_64(monkeypatch):
    from starlette.requests import Request as R
    monkeypatch.setattr(main, "CLIENT_IP_HEADER", "x-real-ip")
    key = lambda ip: main.client_ip(R({"type": "http", "headers": [(b"x-real-ip", ip.encode())], "client": ("1.1.1.1", 1)}))
    assert key("2001:db8:1:2::1") == key("2001:db8:1:2:ffff::9") != key("2001:db8:1:3::1")


def test_security_headers_body_cap_and_no_docs(api):
    client, _ = api
    r = client.get("/")
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    big = client.post("/api/chat", content=b"{" + b" " * (70 * 1024) + b"}", headers={"content-type": "application/json"})
    assert big.status_code == 413


def test_forwarded_for_uses_the_proxy_hop(api):
    from starlette.requests import Request as R
    req = R({"type": "http", "headers": [(b"x-forwarded-for", b"1.2.3.4, 9.9.9.9")], "client": ("10.0.0.1", 1)})
    assert main.client_ip(req) == "9.9.9.9"  # the client can only forge what comes before the proxy's own hop


def test_trusted_proxy_header_wins_when_configured(monkeypatch):
    from starlette.requests import Request as R
    monkeypatch.setattr(main, "CLIENT_IP_HEADER", "x-real-ip")  # Railway
    req = R({"type": "http", "headers": [(b"x-real-ip", b"160.79.1.1"), (b"x-forwarded-for", b"160.79.1.1, 152.233.30.101")], "client": ("100.64.0.1", 1)})
    assert main.client_ip(req) == "160.79.1.1"  # the visitor, not Railway's edge


def test_daily_quota_per_ip(api, monkeypatch):
    client, _ = api
    monkeypatch.setitem(main.quotas, "workspace_day", main.DailyQuota(2))
    codes = [client.post("/api/workspace", json={"lang": "es", "sample": False}, headers={"x-forwarded-for": f"7.7.7.{i}, 5.5.5.5"}).status_code for i in range(3)]
    assert codes == [200, 200, 429]  # rotating the forged part of the header does not help


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


def test_activity_log_follows_language(api):
    client, _ = api
    ws = new_ws(client, "es")
    es = client.get("/api/state", params={"lang": "es"}, headers={"x-workspace": ws}).json()["activity"]
    en = client.get("/api/state", params={"lang": "en"}, headers={"x-workspace": ws}).json()["activity"]
    pt = client.get("/api/state", params={"lang": "pt"}, headers={"x-workspace": ws}).json()["activity"]
    assert es[0]["text"] == "Negocio de ejemplo cargado" and en[0]["text"] == "Sample business loaded" and pt[0]["text"] == "Negócio de exemplo carregado"
    assert any(a["text"].startswith("Cuenta por pagar: Textiles Andinos SAC 180,00 USD") for a in es)


def test_approval_needs_the_exact_fingerprint(api):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    bill = biz.add_bill("T", "x", 50, biz.today().isoformat(), "none", "t@example.com")
    req = biz.request_confirmation("pay_bill", str(bill["id"]))
    body = {"workspace_id": ws, "conversation_id": "conv-fingerprint", "confirmation_id": req["confirmation_id"], "approve": True, "lang": "en"}
    assert client.post("/api/confirm", json=body).status_code == 400  # no fingerprint: not an approval
    st = client.get("/api/state", params={"lang": "en"}, headers={"x-workspace": ws}).json()
    conf = st["confirmations"][0]
    assert conf["readback"].startswith("To confirm: pay T 50.00 USD") and conf["fingerprint"] == req["fingerprint"]
    rb = client.get(f"/api/confirmation/{req['confirmation_id']}/readback", params={"lang": "en"}, headers={"x-workspace": ws}).json()
    assert rb["text"] == conf["readback"]
    ok = client.post("/api/confirm", json={**body, "fingerprint": conf["fingerprint"]}).json()
    assert ok["done"] is True and "movements" in st


def test_refused_approval_gives_back_the_payout_allowance(api, monkeypatch):
    client, _ = api
    monkeypatch.setattr(main, "payouts_per_ip", main.AmountQuota(200))
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    bill = biz.add_bill("T", "x", 150, biz.today().isoformat(), "none", "t@example.com")
    req = biz.request_confirmation("pay_bill", str(bill["id"]))
    body = {"workspace_id": ws, "conversation_id": "conv-refund-1", "confirmation_id": req["confirmation_id"], "approve": True, "lang": "en"}
    assert client.post("/api/confirm", json={**body, "fingerprint": "0" * 32}).json()["done"] is False
    req2 = biz.request_confirmation("pay_bill", str(bill["id"]))
    ok = client.post("/api/confirm", json={**body, "confirmation_id": req2["confirmation_id"], "fingerprint": req2["fingerprint"]})
    assert ok.status_code == 200 and ok.json()["done"] is True  # the failed attempt did not use up the allowance


def test_privacy_page_and_head_request(api):
    client, _ = api
    r = client.get("/privacy")
    assert r.status_code == 200 and "Anthropic" in r.text and "Fish Audio" in r.text and "Sandbox" in r.text
    assert client.head("/").status_code == 200


def test_first_payment_readback_does_not_invite_a_spoken_yes():
    from app import main
    conf = {"kind": "pay_bill", "new_recipient": True, "details": {"payee": "Textiles", "amount": 180, "currency": "USD", "email": "p@x.example"}}
    assert "botón" in main.readback(conf, "es") and "sí" not in main.readback(conf, "es")
    assert "Say" in main.readback({**conf, "new_recipient": False}, "en")


def test_proposals_from_the_tables_go_through_the_dialog(api):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    bill = biz.add_bill("Marta", "Alquiler", 300, biz.today().isoformat(), "none", "marta@example.com")
    r = client.post("/api/proposals", json={"workspace_id": ws, "kind": "pay_bill", "target": str(bill["id"])})
    assert r.status_code == 200 and biz.bill_view(bill["id"])["status"] == "pending"  # proposed, nothing paid
    st = client.get("/api/state", headers={"x-workspace": ws}).json()
    assert [c["kind"] for c in st["confirmations"]] == ["pay_bill"] and st["authorizations"] == []
    assert client.post("/api/proposals", json={"workspace_id": ws, "kind": "record_movement", "target": "1"}).status_code == 422
    assert client.post("/api/proposals", json={"workspace_id": ws, "kind": "pay_bill", "target": "999"}).status_code in (404, 422)
    other = new_ws(client, sample=False)
    assert client.post("/api/proposals", json={"workspace_id": other, "kind": "pay_bill", "target": str(bill["id"])}).status_code in (404, 422)


def test_authorization_log_and_webhook_diagnostics(api, monkeypatch):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    inv = biz.draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 5}], "")
    cid = client.post("/api/proposals", json={"workspace_id": ws, "kind": "send_invoice", "target": inv["id"]}).json()["confirmation_id"]
    conf = client.get("/api/state", headers={"x-workspace": ws}).json()["confirmations"][0]
    r = client.post("/api/confirm", json={"workspace_id": ws, "conversation_id": "11111111-1111-4111-8111-111111111111",
                                          "confirmation_id": cid, "approve": True, "lang": "en", "fingerprint": conf["fingerprint"]})
    assert r.status_code == 200 and r.json()["done"]
    log = client.get("/api/state", headers={"x-workspace": ws}).json()["authorizations"]
    assert log[0]["status"] == "done" and log[0]["via"] == "button" and log[0]["receipt"] == conf["fingerprint"][:10] and log[0]["ref"]
    monkeypatch.setattr(main, "DEMO_MODE", False)
    monkeypatch.setattr(main, "WEBHOOK_ID", "WH")
    assert client.post("/api/paypal/webhook", json={"id": "x"}, headers={"paypal-cert-url": "https://evil.example/c"}).status_code == 400
    assert "webhook_last" not in client.get("/api/status").json()  # gate audit: no public signal of other businesses' activity
    assert client.get("/api/state", headers={"x-workspace": ws}).json()["webhook_last"] == {}


def test_inbox_proposes_and_one_click_reminder(api):
    client, _ = api
    ws = new_ws(client)  # the sample business
    st = client.get("/api/state", headers={"x-workspace": ws}).json()
    kinds = {i["type"] for i in st["inbox"]}
    assert "bill_overdue" in kinds and any(i["action"].get("kind") == "pay_bill" or i["action"].get("ask") for i in st["inbox"])
    assert st["confirmations"] == []  # proposing in the inbox never creates or runs anything by itself
    inv = next(i for i in st["inbox"] if "remind" in i["action"])
    r = client.post("/api/remind", json={"workspace_id": ws, "invoice_id": inv["action"]["remind"]})
    assert r.status_code == 200
    other = new_ws(client)
    assert client.post("/api/remind", json={"workspace_id": other, "invoice_id": inv["action"]["remind"]}).status_code in (404, 422)


def test_spoken_yes_inside_the_dialog_is_decided_by_the_server(api):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    inv = biz.draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 5}], "")
    cid = client.post("/api/proposals", json={"workspace_id": ws, "kind": "send_invoice", "target": inv["id"]}).json()["confirmation_id"]
    fp = client.get("/api/state", headers={"x-workspace": ws}).json()["confirmations"][0]["fingerprint"]
    base = {"workspace_id": ws, "conversation_id": "11111111-1111-4111-8111-111111111111", "confirmation_id": cid, "fingerprint": fp, "lang": "en"}
    r = client.post("/api/confirm/voice", json={**base, "text": "what is the total"}).json()
    assert r["understood"] is False and biz.invoice_view(inv["id"])["status"] == "DRAFT"
    r = client.post("/api/confirm/voice", json={**base, "text": "yes", "alternatives": ["no"]}).json()
    assert r["understood"] is False  # the recognizer wasn't sure
    r = client.post("/api/confirm/voice", json={**base, "text": "yes send it"}).json()
    assert r["understood"] and r["approved"] and r["done"] and biz.invoice_view(inv["id"])["status"] != "DRAFT"
    assert biz.authorizations()[0]["via"] == "voice"


def test_first_payment_is_never_approved_by_voice_in_the_dialog(api):
    client, _ = api
    ws = new_ws(client, sample=False)
    biz = main.business_factory(ws)
    bill = biz.add_bill("Nuevo", "x", 20, biz.today().isoformat(), "none", "new@example.com")
    cid = client.post("/api/proposals", json={"workspace_id": ws, "kind": "pay_bill", "target": str(bill["id"])}).json()["confirmation_id"]
    fp = client.get("/api/state", headers={"x-workspace": ws}).json()["confirmations"][0]["fingerprint"]
    r = client.post("/api/confirm/voice", json={"workspace_id": ws, "conversation_id": "11111111-1111-4111-8111-111111111111",
                                                "confirmation_id": cid, "fingerprint": fp, "text": "yes", "lang": "en"}).json()
    assert r.get("button_only") and biz.bill_view(bill["id"])["status"] == "pending"
