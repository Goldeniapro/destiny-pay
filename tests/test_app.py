import json
import threading
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent as agent_mod
from app.agent import (
    MESSAGES, DestinyPayAgent, Session, TokenBudget, ValidationError, normalize_email, run_tool, validate_draft,
)
from app.fake import FakePayPal
from app.paypal import PayPalClient, PayPalError, summarize
from app.scope import ScopedBackend

ITEMS = [{"name": "Té", "quantity": 3, "unit_price": 12}]
DRAFT_ARGS = {"recipient_name": "Juan Pérez", "recipient_email": "juan arroba correo punto com", "items": ITEMS, "note": ""}


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


# ---------------------------------------------------------------- validation
def test_normalize_spoken_emails():
    assert normalize_email("juan arroba correo punto com") == "juan@correo.com"
    assert normalize_email("Maria Souza arroba exemplo ponto com ponto br") == "mariasouza@exemplo.com.br"
    assert normalize_email("john dot smith at example dot com") == "john.smith@example.com"
    assert normalize_email("ana_ruiz@correo.pe") == "ana_ruiz@correo.pe"
    # words inside names are not spoken symbols
    assert normalize_email("mateo arroba gmail punto com") == "mateo@gmail.com"
    assert normalize_email("catalina dot rojas at mail dot com") == "catalina.rojas@mail.com"
    assert normalize_email("ana guion bajo ruiz arroba correo punto pe") == "ana_ruiz@correo.pe"


@pytest.mark.parametrize("change, error", [
    ({"recipient_email": "juan correo"}, "not a valid email"),
    ({"items": []}, "between 1 and 10"),
    ({"items": [{"name": "Té", "quantity": 0, "unit_price": 5}]}, "Quantity"),
    ({"items": [{"name": "Té", "quantity": 1, "unit_price": -5}]}, "Unit price"),
    ({"items": [{"name": "Té", "quantity": 2, "unit_price": 9000}]}, "limited"),
    ({"recipient_name": "  "}, "name"),
])
def test_validate_draft_rejects_bad_input(change, error):
    with pytest.raises(ValidationError, match=error):
        validate_draft({**DRAFT_ARGS, **change})


# ---------------------------------------------------------------- tools + isolation
def new_session(backend=None, tag="dp-test"):
    return Session(lang="es", backend=ScopedBackend(backend or FakePayPal(), tag))


def test_draft_then_send_then_record_payment():
    s = new_session()
    draft, _ = run_tool(s, "draft_invoice", DRAFT_ARGS)
    draft = json.loads(draft)
    assert draft["status"] == "DRAFT" and draft["total"] == "36.00 USD" and draft["email"] == "juan@correo.com"
    assert draft["reference"] == "dp-test" and draft["payer_link"] is None

    dup = json.loads(run_tool(s, "draft_invoice", DRAFT_ARGS)[0])
    assert dup["duplicate_of"] == draft["id"]  # a repeated request does not create a second invoice

    with pytest.raises(ValidationError, match="not confirmed"):  # same turn: no confirmation yet
        run_tool(s, "send_invoice", {"invoice_id": draft["id"]})
    s.turns += 1  # the owner's next message ("yes, send it")
    sent = json.loads(run_tool(s, "send_invoice", {"invoice_id": draft["id"]})[0])
    assert sent["status"] == "UNPAID" and sent["payer_link"].startswith("https://")

    unpaid = json.loads(run_tool(s, "list_invoices", {"status": "unpaid"})[0])
    assert [r["id"] for r in unpaid] == [draft["id"]]

    part = json.loads(run_tool(s, "record_payment", {"invoice_id": draft["id"], "amount": 10, "method": "CASH", "note": ""})[0])
    assert part["status"] == "PARTIALLY_PAID" and part["due"] == "26.00 USD"
    full = json.loads(run_tool(s, "record_payment", {"invoice_id": draft["id"], "amount": 26, "method": "OTHER", "note": "Yape"})[0])
    assert full["status"] == "MARKED_AS_PAID"
    assert json.loads(run_tool(s, "list_invoices", {"status": "paid"})[0])[0]["id"] == draft["id"]


def test_show_qr_returns_ui_payload_and_requires_sent_invoice():
    s = new_session()
    inv = json.loads(run_tool(s, "draft_invoice", DRAFT_ARGS)[0])
    with pytest.raises(PayPalError, match="NOT_SENT"):
        run_tool(s, "show_qr", {"invoice_id": inv["id"]})
    s.turns += 1
    run_tool(s, "send_invoice", {"invoice_id": inv["id"]})
    out, payload = run_tool(s, "show_qr", {"invoice_id": inv["id"]})
    assert json.loads(out)["ok"] and payload["invoice_id"] == inv["id"] and payload["qr_png_base64"]


def test_a_corrected_draft_is_not_treated_as_duplicate():
    s = new_session()
    first = json.loads(run_tool(s, "draft_invoice", {**DRAFT_ARGS, "recipient_name": "Juan Peres"})[0])
    fixed = json.loads(run_tool(s, "draft_invoice", DRAFT_ARGS)[0])
    assert "duplicate_of" not in fixed and fixed["id"] != first["id"] and fixed["recipient"] == "Juan Pérez"


def test_sessions_sharing_one_merchant_cannot_touch_each_other():
    merchant = FakePayPal()
    alice, bob = new_session(merchant, "dp-alice"), new_session(merchant, "dp-bob")
    inv = json.loads(run_tool(alice, "draft_invoice", DRAFT_ARGS)[0])
    assert json.loads(run_tool(bob, "list_invoices", {"status": "all"})[0]) == []
    for name, args in [("get_invoice", {}), ("send_invoice", {}), ("cancel_invoice", {"note": ""}),
                       ("record_payment", {"amount": 1, "method": "CASH", "note": ""}), ("show_qr", {})]:
        with pytest.raises(PayPalError, match="does not belong"):
            run_tool(bob, name, {"invoice_id": inv["id"], **args})


def test_scoped_list_caches_final_invoices():
    class Counting(FakePayPal):
        gets = 0

        def get_invoice(self, invoice_id):
            Counting.gets += 1
            return super().get_invoice(invoice_id)

    backend = ScopedBackend(Counting(), "dp-x")
    inv = backend.create_invoice("Ana", "ana@correo.com", ITEMS)
    backend.send_invoice(inv["id"])
    backend.record_payment(inv["id"], 36)
    backend.list_invoices()
    before = Counting.gets
    backend._listed = None  # skip the short list cache
    backend.list_invoices()
    assert Counting.gets == before  # a paid invoice is not fetched again


def test_scoped_list_cache_is_refreshed_after_changes():
    backend = ScopedBackend(FakePayPal(), "dp-y")
    assert backend.list_invoices() == []
    inv = backend.create_invoice("Ana", "ana@correo.com", ITEMS)
    assert [i["id"] for i in backend.list_invoices()] == [inv["id"]]  # not the cached empty list
    backend.send_invoice(inv["id"])
    backend.mark_paid(inv["id"])
    assert backend.list_invoices()[0]["status"] == "PAID"


# ---------------------------------------------------------------- agent loop
class ScriptedClient:
    """Mimics client.beta.messages.create with canned responses or exceptions."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def text(t, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=t)], usage=NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0))


def tool(name, args, tid="tu_1"):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=tid, name=name, input=args)], usage=None)


def make_agent(responses, **kw):
    client = ScriptedClient(responses)
    return DestinyPayAgent(lambda sid: FakePayPal(), client=client, **kw), client


def api_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


def test_agent_drafts_and_keeps_valid_history():
    agent, client = make_agent([tool("draft_invoice", DRAFT_ARGS), text("Juan Pérez, 36 dólares. ¿Lo envío?")])
    res = agent.chat("session-1", "Cóbrale a Juan Pérez 3 tés a 12, juan arroba correo punto com", "es")
    assert "36" in res["reply"]
    assert res["actions"][0]["result"]["status"] == "DRAFT"
    msgs = agent.sessions.peek("session-1").messages
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert msgs[0]["content"] == [{"type": "text", "text": "Cóbrale a Juan Pérez 3 tés a 12, juan arroba correo punto com"}]
    assert msgs[2]["content"][0]["type"] == "tool_result"
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert req["output_config"] == {"effort": agent_mod.EFFORT} and req["cache_control"] == {"type": "ephemeral"}
    assert all(t["strict"] and t["input_schema"]["additionalProperties"] is False for t in req["tools"])
    assert "Spanish" in req["system"] and "USD" in req["system"]


def test_agent_language_is_fixed_per_session():
    agent, client = make_agent([text("Hi"), text("Hello again")])
    agent.chat("session-en", "hello", "en")
    agent.chat("session-en", "hola", "es")
    assert "English" in client.requests[0]["system"] and client.requests[0]["system"] == client.requests[1]["system"]


def test_tool_error_becomes_is_error_result():
    agent, _ = make_agent([tool("get_invoice", {"invoice_id": "NOPE"}), text("No encontré esa factura.")])
    res = agent.chat("session-2", "¿Ya pagó la factura NOPE?")
    assert "does not belong" in res["actions"][0]["error"]
    result = agent.sessions.peek("session-2").messages[2]["content"][0]
    assert result["is_error"] is True and result["tool_use_id"] == "tu_1"


def test_unexpected_tool_crash_is_contained(monkeypatch):
    def boom(*a, **k):
        raise KeyError("bug")

    monkeypatch.setattr(agent_mod, "run_tool", boom)
    agent, _ = make_agent([tool("list_invoices", {"status": "all"}), text("Hubo un problema.")])
    res = agent.chat("session-3", "¿Quién me debe?")
    assert res["actions"][0]["error"] == "Unexpected error: KeyError"
    assert res["reply"] == "Hubo un problema."


def test_api_failure_rolls_back_and_session_recovers():
    agent, client = make_agent([api_error(), text("Hola de nuevo")])
    res = agent.chat("session-4", "hola", "es")
    assert res["reply"] == MESSAGES["api_error"]["es"] and res["error"] == "api_error"
    assert agent.sessions.peek("session-4").messages == []
    assert agent.chat("session-4", "hola", "es")["reply"] == "Hola de nuevo"
    assert len(client.requests[1]["messages"]) == 1


def test_api_failure_after_a_tool_ran_keeps_it_in_history():
    agent, client = make_agent([tool("list_invoices", {"status": "all"}), api_error(), text("ok")])
    res = agent.chat("session-5", "¿Quién me debe?", "en")
    assert res["error"] == "api_error" and res["reply"] == MESSAGES["partial"]["en"]
    msgs = agent.sessions.peek("session-5").messages
    assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant"]
    assert "list_invoices" in msgs[-1]["content"][0]["text"] and "do not repeat" in msgs[-1]["content"][0]["text"]
    agent.chat("session-5", "and now?", "en")  # the next turn sends a valid, alternating history
    roles = [m["role"] for m in client.requests[-1]["messages"]]
    assert roles == ["user", "assistant", "user", "assistant", "user"]


def test_payment_is_not_recorded_twice_after_a_failure():
    agent, _ = make_agent([])
    session = agent.sessions.get("session-pay", "es")
    inv = json.loads(run_tool(session, "draft_invoice", DRAFT_ARGS)[0])
    session.turns += 1
    run_tool(session, "send_invoice", {"invoice_id": inv["id"]})
    pay = {"invoice_id": inv["id"], "amount": 10, "method": "CASH", "note": ""}
    agent.client.responses += [tool("record_payment", pay), api_error()]
    res = agent.chat("session-pay", "Juan me pagó 10", "es")
    assert res["reply"] == MESSAGES["partial"]["es"]  # does not tell the owner to repeat it
    assert session.backend.get_invoice(inv["id"])["due_amount"]["value"] == "26.00"
    assert "record_payment" in session.messages[-1]["content"][0]["text"]


def test_draft_and_send_in_one_turn_is_blocked():
    agent, _ = make_agent([
        tool("draft_invoice", DRAFT_ARGS),
        NS(stop_reason="tool_use", usage=None, content=[NS(type="tool_use", id="tu_s", name="send_invoice", input={"invoice_id": "INV2-DEMO-0001"})]),
        text("Primero confirma, por favor."),
    ])
    res = agent.chat("session-guard", "Cóbrale a Juan y envíala ya", "es")
    assert res["actions"][1]["tool"] == "send_invoice" and "not confirmed" in res["actions"][1]["error"]
    assert agent.sessions.peek("session-guard").backend.get_invoice("INV2-DEMO-0001")["status"] == "DRAFT"


def test_empty_final_answer_never_poisons_history():
    agent, client = make_agent([NS(stop_reason="end_turn", content=[], usage=None), text("Hola")])
    assert agent.chat("session-empty", "hola", "es")["reply"] == MESSAGES["done"]["es"]
    stored = agent.sessions.peek("session-empty").messages[-1]
    assert stored["role"] == "assistant" and stored["content"][-1]["text"]
    assert agent.chat("session-empty", "hola", "es")["reply"] == "Hola"


def test_rejected_request_resets_the_conversation():
    bad = anthropic.BadRequestError("bad history", response=httpx.Response(400, request=httpx.Request("POST", "https://api.anthropic.com")), body=None)
    agent, _ = make_agent([text("uno"), bad, text("dos")])
    agent.chat("session-400", "hola", "es")
    res = agent.chat("session-400", "hola otra vez", "es")
    assert res["error"] == "api_error" and agent.sessions.peek("session-400").messages == []
    assert agent.chat("session-400", "hola", "es")["reply"] == "dos"


@pytest.mark.parametrize("stop, key", [("max_tokens", "too_long"), ("refusal", "refusal")])
def test_truncated_or_refused_turns_are_rolled_back(stop, key):
    agent, _ = make_agent([text("...", stop=stop)])
    res = agent.chat("session-6", "hola", "pt")
    assert res["reply"] == MESSAGES[key]["pt"]
    assert agent.sessions.peek("session-6").messages == []


def test_step_limit_closes_the_turn():
    loop = [tool("list_invoices", {"status": "all"}, tid=f"tu_{i}") for i in range(3)]
    agent, _ = make_agent(loop, max_steps=3)
    res = agent.chat("session-7", "?", "es")
    assert res["error"] == "steps" and res["reply"] == MESSAGES["partial"]["es"]
    assert agent.sessions.peek("session-7").messages[-1]["role"] == "assistant"


def test_failed_tool_only_turn_is_rolled_back():
    loop = [tool("get_invoice", {"invoice_id": "NOPE"}, tid=f"tu_{i}") for i in range(2)]
    agent, _ = make_agent(loop, max_steps=2)
    res = agent.chat("session-7b", "?", "es")
    assert res["reply"] == MESSAGES["steps"]["es"] and agent.sessions.peek("session-7b").messages == []


def test_busy_session_does_not_run_twice():
    agent, client = make_agent([])
    session = agent.sessions.get("session-8", "es")
    with session.lock:
        res = agent.chat("session-8", "hola", "es")
    assert res["busy"] and client.requests == []


def test_turn_cap(monkeypatch):
    monkeypatch.setattr(agent_mod, "MAX_TURNS_PER_SESSION", 1)
    agent, _ = make_agent([text("ok")])
    agent.chat("session-9", "uno", "es")
    res = agent.chat("session-9", "dos", "es")
    assert res["reply"] == MESSAGES["session_full"]["es"] and res["error"] == "session_full"


def test_daily_token_budget():
    budget = TokenBudget(limit=15)
    agent, client = make_agent([text("ok")], budget=budget)
    agent.chat("session-10", "uno", "en")  # uses 15 tokens
    assert agent.chat("session-10", "dos", "en")["reply"] == MESSAGES["budget"]["en"]
    assert len(client.requests) == 1


def test_active_sessions_survive_a_flood_of_new_ones():
    agent, _ = make_agent([])
    agent.sessions.max = 3
    agent.sessions.get("real-visitor", "es")
    for i in range(5):
        agent.sessions.get(f"flood-{i}", "es")
        agent.sessions.peek("real-visitor")  # the visitor's page keeps polling
    assert agent.sessions.peek("real-visitor") is not None


def test_missing_api_key_gives_friendly_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    agent = DestinyPayAgent(lambda sid: FakePayPal())
    res = agent.chat("session-11", "hola", "es")
    assert res["error"] == "api_error"


# ---------------------------------------------------------------- HTTP API
@pytest.fixture
def api(monkeypatch):
    from app import main

    monkeypatch.setattr(main, "DEMO_MODE", True)
    monkeypatch.setattr(main, "chat_limiter", main.RateLimiter(3))
    monkeypatch.setattr(main, "global_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "new_session_limiter", main.RateLimiter(100))
    monkeypatch.setattr(main, "invoices_limiter", main.RateLimiter(100))
    fresh = DestinyPayAgent(main.backend_factory, client=ScriptedClient([]))
    monkeypatch.setattr(main, "agent", fresh)
    return TestClient(main.app), fresh, main


def test_status_and_index(api):
    client, _, _ = api
    s = client.get("/api/status").json()
    assert s["demo_mode"] is True and s["langs"] == ["es", "en", "pt"] and s["currency"] == "USD"
    assert "Destiny" in client.get("/").text


def test_chat_invoices_and_demo_payment(api):
    client, agent, _ = api
    sid = "abcdef12-3456"
    agent.client.responses += [tool("draft_invoice", DRAFT_ARGS), text("¿Lo envío?")]
    r = client.post("/api/chat", json={"session_id": sid, "message": "Cóbrale a Juan", "lang": "es"})
    assert r.status_code == 200 and r.json()["reply"] == "¿Lo envío?"
    rows = client.get("/api/invoices", params={"session_id": sid}).json()
    assert rows[0]["status"] == "DRAFT"
    inv_id = rows[0]["id"]

    assert client.post(f"/api/demo/pay/{inv_id}", params={"session_id": sid}).status_code == 409  # draft: not payable
    agent.sessions.peek(sid).backend.send_invoice(inv_id)
    agent.sessions.peek(sid).backend._listed = None
    assert client.post(f"/api/demo/pay/{inv_id}", params={"session_id": sid}).status_code == 200
    assert client.get("/api/invoices", params={"session_id": sid}).json()[0]["status"] == "PAID"
    assert client.post(f"/api/demo/pay/{inv_id}", params={"session_id": "other-session-1"}).status_code == 404
    assert client.get("/api/invoices", params={"session_id": "unknown-session"}).json() == []


def test_chat_input_validation_and_rate_limit(api):
    client, agent, _ = api
    assert client.post("/api/chat", json={"session_id": "bad id!", "message": "hi"}).status_code == 422
    assert client.post("/api/chat", json={"session_id": "abcdefgh", "message": ""}).status_code == 422
    assert client.get("/api/invoices", params={"session_id": "<script>"}).status_code == 422
    agent.client.responses += [text("ok"), text("ok"), text("ok")]
    codes = [client.post("/api/chat", json={"session_id": "abcdefgh", "message": "hola"}).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]  # 3 per minute in this test; rejected 422s never reach the limiter


def test_new_session_and_invoice_polling_limits(api, monkeypatch):
    client, agent, main = api
    monkeypatch.setattr(main, "new_session_limiter", main.RateLimiter(1))
    monkeypatch.setattr(main, "invoices_limiter", main.RateLimiter(2))
    agent.client.responses += [text("ok"), text("ok")]
    assert client.post("/api/chat", json={"session_id": "first-session", "message": "hola"}).status_code == 200
    assert client.post("/api/chat", json={"session_id": "second-session", "message": "hola"}).status_code == 429
    assert client.post("/api/chat", json={"session_id": "first-session", "message": "hola"}).status_code == 200
    codes = [client.get("/api/invoices", params={"session_id": "first-session"}).status_code for _ in range(3)]
    assert codes == [200, 200, 429]


def test_limiter_forgets_idle_keys():
    from app.main import RateLimiter

    rl = RateLimiter(5)
    for i in range(5002):
        rl.hits[f"k{i}"]  # empty deques, as left behind by spoofed addresses
    rl.allow("fresh")
    assert len(rl.hits) == 1


def test_demo_pay_is_disabled_outside_demo(api, monkeypatch):
    client, agent, main = api
    agent.sessions.get("abcdefgh", "es")
    monkeypatch.setattr(main, "DEMO_MODE", False)
    assert client.post("/api/demo/pay/INV2-DEMO-0001", params={"session_id": "abcdefgh"}).status_code == 404
