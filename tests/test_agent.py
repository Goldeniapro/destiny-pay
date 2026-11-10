import json
from types import SimpleNamespace as NS

import anthropic
import httpx
import pytest

from app import agent as agent_mod
from app.agent import TOOLS, DestinyPayAgent, TokenBudget
from app.i18n import T
from app.business import ValidationError

DRAFT = {"customer_name": "Sarah Miller", "customer_email": "sarah arroba example punto com",
         "items": [{"name": "Chompa", "quantity": 2, "unit_price": 45}], "note": "", "due_in_days": 7}


class Scripted:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kw):
        self.requests.append({**kw, "messages": list(kw["messages"])})
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r(kw) if callable(r) else r


def text(t, stop="end_turn"):
    return NS(stop_reason=stop, content=[NS(type="text", text=t)], usage=NS(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0))


def tool(name, args, tid="tu_1"):
    return NS(stop_reason="tool_use", content=[NS(type="tool_use", id=tid, name=name, input=args)], usage=None)


def api_error():
    return anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))


@pytest.fixture
def setup(make_biz):
    biz = make_biz()
    businesses = {biz.ws: biz}

    def make(responses, **kw):
        client = Scripted(responses)
        return DestinyPayAgent(lambda ws: businesses[ws], client=client, **kw), client

    return biz, make


def trust(biz, email):
    """A supplier email this business has already paid once through PayPal (a spoken yes may pay it again)."""
    old = biz.add_bill("Earlier payee", "earlier order", 10, biz.today().isoformat(), "none", email)
    biz.store.x("INSERT INTO payouts(sender_batch_id, ws, bill_id, status, batch_id, amount, checked_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("trusted-" + email, biz.ws, old["id"], "done", "B-old", 10, 0, 0))


def last_invoice(biz):
    return biz.list_invoices()[0]


def test_tools_are_strict_and_complete():
    names = {t["name"] for t in TOOLS}
    assert {"draft_invoice", "request_send_invoice", "request_pay_bill", "add_bill", "create_reminder", "remember", "financial_overview"} <= names
    for t in TOOLS:
        schema = t["input_schema"]
        assert t["strict"] and schema["additionalProperties"] is False and set(schema["required"]) == set(schema["properties"])


def test_full_charge_flow_with_spoken_yes(setup):
    biz, make = setup
    agent, client = make([
        tool("draft_invoice", DRAFT),
        lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "tu_2"),
        text("Sarah Miller, 2 chompas a 45, total 90 dólares, al correo s-a-r-a-h arroba example punto com. ¿Confirmas?"),
    ])
    res = agent.chat(biz.ws, "conv-1", "Cóbrale a Sarah dos chompas a 45", "es")
    assert [a["tool"] for a in res["actions"]] == ["draft_invoice", "request_send_invoice"]
    assert res["ui"][0]["confirmation"]["details"]["total"] == 90
    assert last_invoice(biz)["status"] == "DRAFT"
    first = client.requests[0]["messages"][0]["content"][0]["text"]
    assert "<business_memory>" in first and first.count("[20") >= 1  # memory + local time
    res2 = agent.chat(biz.ws, "conv-1", "Sí, envíala", "es")  # handled by code: no model call
    assert len(client.requests) == 3 and res2.get("confirmed")
    assert "Sarah Miller" in res2["reply"] and last_invoice(biz)["status"] == "UNPAID"
    roles = [m["role"] for m in agent.conversations.peek("conv-1").messages]
    assert roles[-2:] == ["user", "assistant"]


def test_yes_to_another_question_does_not_execute(setup):
    biz, make = setup
    agent, client = make([
        tool("draft_invoice", DRAFT),
        lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "tu_2"),
        text("¿Confirmas?"),
        text("¿Quieres que también te recuerde el viernes?"),
        text("Perfecto."),
    ])
    agent.chat(biz.ws, "conv-2", "Cóbrale a Sarah", "es")
    agent.chat(biz.ws, "conv-2", "¿cuánto me debe Lucho?", "es")  # a different topic: the card stays pending
    agent.chat(biz.ws, "conv-2", "sí", "es")  # answers the model's new question, not the card
    assert last_invoice(biz)["status"] == "DRAFT" and len(client.requests) == 5


def test_no_cancels_and_change_goes_to_model(setup):
    biz, make = setup
    agent, client = make([
        tool("draft_invoice", DRAFT),
        lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "tu_2"),
        text("¿Confirmas?"),
        text("Claro, ¿a cuánto lo cambio?"),
    ])
    agent.chat(biz.ws, "conv-3", "Cóbrale a Sarah", "es")
    res = agent.chat(biz.ws, "conv-3", "sí, pero cambia el precio", "es")
    assert res["reply"] == "Claro, ¿a cuánto lo cambio?"  # the model handles corrections
    assert biz.pending_confirmations()  # still waiting for the owner


def test_button_confirm_records_in_conversation(setup):
    biz, make = setup
    agent, _ = make([tool("draft_invoice", DRAFT), lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "tu_2"), text("¿Confirmas?")])
    res = agent.chat(biz.ws, "conv-4", "Cóbrale a Sarah", "en")
    cid = res["ui"][0]["confirmation"]["confirmation_id"]
    out = agent.confirm(biz.ws, "conv-4", cid, True, "en")
    assert out["done"] and "Sarah Miller" in out["reply"]
    conv = agent.conversations.peek("conv-4")
    assert conv.awaiting == "" and "pressed Authorize" in conv.messages[-2]["content"][0]["text"]
    assert not agent.confirm(biz.ws, "conv-4", cid, True, "en")["done"]


def test_model_cannot_send_by_itself(setup):
    biz, make = setup
    names = {t["name"] for t in TOOLS}
    assert "send_invoice" not in names and "pay_bill" not in names and "cancel_invoice" not in names


def test_tool_errors_are_contained(setup):
    biz, make = setup
    agent, _ = make([tool("get_invoice", {"invoice_id": "NOPE"}), text("No encontré esa factura.")])
    res = agent.chat(biz.ws, "conv-5", "¿pagó NOPE?", "es")
    assert "does not belong" in res["actions"][0]["error"]
    result = agent.conversations.peek("conv-5").messages[2]["content"][0]
    assert result["is_error"] is True


def test_api_failure_rolls_back_or_keeps_completed_work(setup):
    biz, make = setup
    agent, client = make([api_error(), text("Hola")])
    res = agent.chat(biz.ws, "conv-6", "hola", "pt")
    assert res["reply"] == T["api_error"]["pt"] and agent.conversations.peek("conv-6").messages == []
    agent.chat(biz.ws, "conv-6", "hola", "pt")
    agent2, _ = make([tool("record_movement", {"kind": "income", "description": "Venta", "amount": 20, "method": "CASH", "category": "sales", "contact": ""}), api_error()])
    res = agent2.chat(biz.ws, "conv-7", "vendí 20 en efectivo", "es")
    assert res["reply"] == T["partial"]["es"] and biz.overview()["income"] == 20
    assert "record_movement" in agent2.conversations.peek("conv-7").messages[-1]["content"][0]["text"]


def test_bad_request_resets_conversation_and_empty_answer_is_safe(setup):
    biz, make = setup
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
    agent, _ = make([NS(stop_reason="end_turn", content=[], usage=None), bad, text("ok")])
    assert agent.chat(biz.ws, "conv-8", "hola", "es")["reply"] == T["done"]["es"]
    assert agent.conversations.peek("conv-8").messages[-1]["content"][-1]["text"]
    assert agent.chat(biz.ws, "conv-8", "hola", "es")["error"] == "api_error"
    assert agent.conversations.peek("conv-8").messages == []
    assert agent.chat(biz.ws, "conv-8", "hola", "es")["reply"] == "ok"


def test_alternatives_and_language_are_passed(setup):
    biz, make = setup
    agent, client = make([text("ok")])
    agent.chat(biz.ws, "conv-9", "cóbrale a juan peres", "pt", alternatives=["cóbrale a Juan Pérez", "cóbrale a juan peres"])
    req = client.requests[0]
    assert "Portuguese" in req["system"] and "Juan Pérez" in req["messages"][0]["content"][0]["text"]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default" and req["cache_control"] == {"type": "ephemeral"}


def test_busy_turn_cap_and_budget(setup, monkeypatch):
    biz, make = setup
    agent, client = make([])
    conv = agent.conversations.get("conv-10", biz.ws, "es")
    with conv.lock:
        assert agent.chat(biz.ws, "conv-10", "hola", "es")["busy"]
    monkeypatch.setattr(agent_mod, "MAX_TURNS_PER_CONVERSATION", 0)
    assert agent.chat(biz.ws, "conv-10", "hola", "es")["error"] == "conversation_full"
    monkeypatch.setattr(agent_mod, "MAX_TURNS_PER_CONVERSATION", 40)
    agent2, client2 = make([text("ok")], budget=TokenBudget(limit=15))
    agent2.chat(biz.ws, "conv-11", "uno", "en")
    assert agent2.chat(biz.ws, "conv-11", "dos", "en")["error"] == "budget" and len(client2.requests) == 1


def test_conversation_never_crosses_businesses(make_biz):
    a, b = make_biz(), make_biz()
    client = Scripted([text("a"), text("b")])
    agent = DestinyPayAgent(lambda ws: {a.ws: a, b.ws: b}[ws], client=client)
    agent.chat(a.ws, "shared-id", "hola", "es")
    agent.chat(b.ws, "shared-id", "hola", "es")
    assert len(client.requests[1]["messages"]) == 1  # b starts its own conversation


def test_every_tool_runs(setup):
    biz, make = setup
    inv = biz.draft_invoice("Sarah Miller", "sarah@example.com", DRAFT["items"], "")
    biz.send_invoice(inv["id"])
    calls = [
        ("find_contacts", {"query": "sarah"}), ("save_contact", {"name": "Lucho", "email": "lucho@example.com", "kind": "customer", "notes": ""}),
        ("list_invoices", {"status": "unpaid", "customer": ""}), ("get_invoice", {"invoice_id": inv["id"]}),
        ("remind_invoice", {"invoice_id": inv["id"]}), ("show_qr", {"invoice_id": inv["id"]}),
        ("request_record_payment", {"invoice_id": inv["id"], "amount": 10, "method": "PLIN"}),
        ("add_bill", {"payee": "Marta", "description": "Alquiler", "amount": 300, "due_date": biz.today().isoformat(), "recurrence": "monthly", "payee_email": "marta@example.com"}),
        ("list_bills", {"status": "pending", "within_days": 30}), ("request_pay_bill", {"bill_id": 1}),
        ("request_mark_bill_paid", {"bill_id": 1, "method": "CASH"}),
        ("record_movement", {"kind": "expense", "description": "Taxi", "amount": 8, "method": "CASH", "category": "transport", "contact": ""}),
        ("create_reminder", {"text": "Llamar", "due_at": "2099-01-01T10:00", "kind": "owner_note", "invoice_id": ""}),
        ("list_reminders", {"include_done": False}), ("remember", {"fact": "Lucho paga con Plin"}),
        ("financial_overview", {"period": "month"}), ("request_cancel_invoice", {"invoice_id": inv["id"]}),
    ]
    from app.agent import run_tool
    from app.business import ValidationError
    for name, args in calls:
        try:
            run_tool(biz, "conv", name, args)
        except ValidationError as e:
            assert name == "create_reminder" and "one year" in str(e), (name, e)
    from datetime import timedelta
    r = run_tool(biz, "conv", "create_reminder", {"text": "Banco", "due_at": (biz.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"),
                                                 "kind": "owner_note", "invoice_id": ""})[0]
    assert run_tool(biz, "conv", "cancel_reminder", {"reminder_id": r["id"]})[0]["status"] == "cancelled"


def test_cancel_reply_never_pays(setup):
    biz, make = setup
    bill = biz.add_bill("Marta Quispe", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    trust(biz, "marta@example.com")
    agent, client = make([tool("request_pay_bill", {"bill_id": bill["id"]}), text("¿Confirmas el pago de 300?"), text("Entendido.")])
    agent.chat(biz.ws, "conv-c1", "Págale el alquiler a Marta", "en")
    res = agent.chat(biz.ws, "conv-c1", "cancel the payment please", "en")
    assert res["reply"] == "Understood, I won't do it." and biz.bill_view(bill["id"])["status"] == "pending"
    assert biz.paypal.payouts == {}


def test_question_after_card_goes_to_model(setup):
    biz, make = setup
    bill = biz.add_bill("Marta Quispe", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    agent, client = make([tool("request_pay_bill", {"bill_id": bill["id"]}), text("¿Confirmas?"), text("Es para Marta Quispe.")])
    agent.chat(biz.ws, "conv-c2", "Págale el alquiler a Marta", "es")
    res = agent.chat(biz.ws, "conv-c2", "¿para quién es?", "es")
    assert res["reply"] == "Es para Marta Quispe." and biz.bill_view(bill["id"])["status"] == "pending"


def test_memory_snapshot_survives_reset_and_note(setup):
    biz, make = setup
    bad = anthropic.BadRequestError("bad", response=httpx.Response(400, request=httpx.Request("POST", "https://x")), body=None)
    agent, client = make([tool("draft_invoice", DRAFT), lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "t2"), bad, text("ok")])
    agent.chat(biz.ws, "conv-m", "Cóbrale a Sarah", "es")
    conv = agent.conversations.peek("conv-m")
    assert conv.messages == [] and conv.awaiting == ""  # the reset clears the pending voice confirmation
    agent.chat(biz.ws, "conv-m", "hola", "es")
    assert "<business_memory>" in client.requests[-1]["messages"][0]["content"][0]["text"]


def test_unsure_recognizer_yes_is_not_enough(setup):
    biz, make = setup
    agent, client = make([tool("draft_invoice", DRAFT), lambda kw: tool("request_send_invoice", {"invoice_id": last_invoice(biz)["id"]}, "t2"),
                          text("¿Confirmas?"), text("¿Lo envío entonces?")])
    agent.chat(biz.ws, "conv-alt", "Cóbrale a Sarah", "es")
    agent.chat(biz.ws, "conv-alt", "sí", "es", alternatives=["sí", "si no"])
    assert last_invoice(biz)["status"] == "DRAFT"


def test_two_payments_in_one_turn_cannot_be_approved_by_voice(setup):
    """Security audit M1: a "yes" must never approve a payment other than the one read aloud."""
    biz, make = setup
    rent = biz.add_bill("Marta Quispe", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    other = biz.add_bill("Proveedor X", "x", 1900, biz.today().isoformat(), "none", "attacker@example.com")
    agent, client = make([tool("request_pay_bill", {"bill_id": rent["id"]}), tool("request_pay_bill", {"bill_id": other["id"]}, "t2"),
                          text("Le pago 300 a Marta Quispe. ¿Confirmas?"), text("¿Cuál de los dos?")])
    res = agent.chat(biz.ws, "conv-two", "Págale a Marta", "es")
    assert T["multi_confirm"]["es"].format(n=2) in res["reply"]
    agent.chat(biz.ws, "conv-two", "Sí, págale", "es")
    assert biz.bill_view(rent["id"])["status"] == "pending" and biz.bill_view(other["id"])["status"] == "pending"
    assert biz.paypal.payouts == {}


def test_code_always_reads_back_what_a_yes_will_approve(setup):
    """Security audit M3: the reply always ends with the code's own readback, whatever the model said."""
    biz, make = setup
    bill = biz.add_bill("Marta Quispe", "Alquiler", 1900, biz.today().isoformat(), "monthly", "marta@example.com")
    trust(biz, "marta@example.com")
    agent, _ = make([tool("request_pay_bill", {"bill_id": bill["id"]}), text("Tienes 1.900 de alquiler. ¿Te pongo un recordatorio?")])
    res = agent.chat(biz.ws, "conv-rb", "Págale a Marta", "es")
    assert res["reply"].endswith("Para confirmar: pagar 1.900,00 USD a Marta Quispe con PayPal, al correo marta@example.com. Di «sí» o toca Autorizar.")


def test_conversation_ids_are_scoped_to_their_business(setup, make_biz):
    biz, make = setup
    agent, _ = make([text("hola")])
    agent.chat(biz.ws, "shared-id", "hola", "es")
    assert agent.conversations.peek("shared-id", biz.ws) is not None
    assert agent.conversations.peek("shared-id", "f" * 32) is None


def test_payee_written_down_in_the_same_turn_needs_the_button(setup):
    """v5 audit H1: a voice 'yes' never approves a payout to a payee the model just added."""
    biz, make = setup
    def add_then_request(kw):
        bill = biz.add_bill("Proveedor Nuevo", "x", 180, biz.today().isoformat(), "none", "nuevo@example.com")
        return tool("request_pay_bill", {"bill_id": bill["id"]}, "t2")
    agent, _ = make([add_then_request, text("Le pago 180 a Proveedor Nuevo."), text("ok")])
    res = agent.chat(biz.ws, "conv-newpayee", "Págale 180 a Proveedor Nuevo", "es")
    assert T["button_only"]["es"] in res["reply"]
    agent.chat(biz.ws, "conv-newpayee", "sí", "es")
    assert biz.paypal.payouts == {}


def test_first_payment_to_an_email_is_never_approved_by_voice(setup):
    """Security jury M1: an injected email change can't be paid on a spoken yes; only the on-screen dialog can approve it."""
    biz, make = setup
    biz.add_bill("Textiles Andinos SAC", "Hilo", 1999, biz.today().isoformat(), "none", "pagos@textiles.example.com")
    biz.save_contact("Textiles Andinos SAC", "evil@attacker.example", "supplier")  # what an obeyed injection could do
    bill = biz.add_bill("Textiles Andinos SAC", "Hilo extra", 1999, biz.today().isoformat(), "none", "evil@attacker.example")
    biz.store.x("UPDATE bills SET created_at=0 WHERE id=?", (bill["id"],))  # written down in an earlier turn
    agent, _ = make([tool("request_pay_bill", {"bill_id": bill["id"]}), text("¿Quieres que te lea el resumen del día?"), text("Listo.")])
    res = agent.chat(biz.ws, "conv-m1", "hola", "es")
    assert "primer pago a este correo" in res["reply"]
    agent.chat(biz.ws, "conv-m1", "sí", "es")
    assert biz.bill_view(bill["id"])["status"] == "pending" and biz.paypal.payouts == {}
    assert biz.pending_confirmations()[0]["new_recipient"] is True


def test_paypal_is_never_a_manual_payment_method(setup):
    """Security jury B1: PayPal money only moves through the authorization gate, never as a 'manual' record."""
    biz, _ = setup
    bill = biz.add_bill("Marta Quispe", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    with pytest.raises(ValidationError, match="request_pay_bill"):
        biz.mark_bill_paid(bill["id"], "PAYPAL")
    inv = biz.draft_invoice("Sarah Miller", "sarah@example.com", [{"name": "Chompa", "quantity": 1, "unit_price": 45}], "", 7)
    biz.send_invoice(inv["id"])
    with pytest.raises(ValidationError, match="recorded by PayPal"):
        biz.record_payment(inv["id"], 45, "PAYPAL")
    assert biz.bill_view(bill["id"])["status"] == "pending"


def test_show_view_drives_the_interface(setup):
    from app.agent import run_tool
    biz, _ = setup
    out, ui = run_tool(biz, "conv", "show_view", {"view": "invoices", "filter": "overdue"})
    assert ui == {"view": "invoices", "filter": "overdue"} and out["shown"] == "invoices"


def test_spoken_yes_records_a_cash_payment_only_after_the_code_read_it_back(setup):
    """Gate audit H1: the record_payment readback names the method, and a "yes" is armed only once it was said."""
    biz, make = setup
    inv = biz.draft_invoice("Sarah Miller", "sarah@example.com", DRAFT["items"], "")
    biz.send_invoice(inv["id"])
    agent, _ = make([tool("request_record_payment", {"invoice_id": inv["id"], "amount": 10, "method": "YAPE"}), text("Listo para registrar."), text("ok")])
    res = agent.chat(biz.ws, "conv-rp", "Sarah me pagó 10 por Yape", "es")
    assert res["reply"].endswith("Para confirmar: registrar que Sarah Miller pagó 10,00 USD en Yape. Di «sí» o toca Autorizar.")
    res = agent.chat(biz.ws, "conv-rp", "sí", "es")
    assert biz.overview()["income"] == 10


def test_spoken_yes_cancels_an_unclaimed_payout_after_the_readback(setup):
    biz, make = setup
    bill = biz.add_bill("Proveedor", "hilo", 120, biz.today().isoformat(), "none", "nobody@unclaimed.example")
    biz.pay_bill_with_paypal(bill["id"])
    biz.check_payouts()
    agent, _ = make([tool("request_cancel_payout", {"bill_id": bill["id"]}), text("Puedo cancelarlo."), text("ok")])
    res = agent.chat(biz.ws, "conv-cp", "cancela el pago al proveedor", "es")
    assert "nobody@unclaimed.example" in res["reply"] and res["reply"].endswith("Di «sí» o toca Autorizar.")
    agent.chat(biz.ws, "conv-cp", "sí", "es")
    assert biz.bill_view(bill["id"])["status"] == "pending"


def test_a_readback_that_cannot_be_built_never_arms_a_spoken_yes(setup, monkeypatch):
    biz, make = setup
    inv = biz.draft_invoice("Sarah Miller", "sarah@example.com", DRAFT["items"], "")
    biz.send_invoice(inv["id"])
    from app import i18n
    monkeypatch.setitem(i18n.T, "rb_record_payment", {"es": "{missing}", "en": "{missing}"})
    agent, _ = make([tool("request_record_payment", {"invoice_id": inv["id"], "amount": 10, "method": "CASH"}), text("Listo."), text("ok")])
    agent.chat(biz.ws, "conv-rp2", "Sarah me pagó 10", "es")
    agent.chat(biz.ws, "conv-rp2", "sí", "es")
    assert biz.overview()["income"] == 0  # nothing was read back, so nothing a "yes" could approve
