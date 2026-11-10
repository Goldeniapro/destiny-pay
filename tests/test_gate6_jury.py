"""Gate 6 jury (Ciber + Alpha) adversarial tests, kept as regressions: each one asserts the safe behaviour."""
import time

import pytest
from fastapi.testclient import TestClient

from app import i18n
from app.agent import DestinyPayAgent, run_tool
from app.business import GATED_KINDS, ValidationError
from app.paypal import PayPalError

ITEMS = [{"name": "Chompa de alpaca", "quantity": 2, "unit_price": 45}]


def sent_invoice(biz):
    inv = biz.draft_invoice("Sarah Miller", "sarah@example.com", ITEMS, "")
    biz.send_invoice(inv["id"])
    return inv


# ---------------------------------------------------------------------------------------------- F1
def test_F1_record_movement_never_books_a_paypal_payment_outside_the_gate(biz):
    """README: 'PayPal is never accepted as a manual payment method, so money can't be marked as sent outside the gate'.
    record_movement (no authorization) accepts method PAYPAL in its strict enum and in Business."""
    with pytest.raises(ValidationError):
        run_tool(biz, "conv", "record_movement", {"kind": "expense", "description": "PayPal payment to Textiles Andinos",
                                                  "amount": 2000, "method": "PAYPAL", "category": "bills", "contact": "Textiles Andinos"})


# ---------------------------------------------------------------------------------------------- F2
def test_F2_a_bill_whose_payout_went_to_a_wrong_email_can_be_corrected(biz):
    """SECURITY: 'A payout to a wrong email is lost -> ... can be cancelled so PayPal returns the money'.
    After cancelling, nothing (tool, endpoint, contact update) can change the bill's payee_email:
    the only PayPal proposal for it is to the same wrong email again."""
    bill = biz.add_bill("Proveedor", "hilo", 120, biz.today().isoformat(), "none", "wrong@unclaimed.example")
    biz.pay_bill_with_paypal(bill["id"])
    biz.check_payouts()
    req = biz.request_confirmation("cancel_payout", str(bill["id"]))
    assert biz.resolve_confirmation(req["confirmation_id"], True, "es", req["fingerprint"])[0]
    biz.save_contact("Proveedor", "right@example.com", "supplier")  # the owner gives the right email
    again = biz.request_confirmation("pay_bill", str(bill["id"]))
    assert again["details"]["email"] == "right@example.com"


# ---------------------------------------------------------------------------------------------- F3
def test_F3_two_equal_cash_payments_are_both_recorded_even_if_a_refresh_failed(biz, monkeypatch):
    """record_payment's PayPal-Request-Id is dp-rp-<invoice>-<cents>-<local due>. If the refresh after the first
    payment fails, the local due is stale, so a second, legitimate payment of the same amount gets the SAME id:
    PayPal returns the first answer, nothing new is recorded, yet the authorization log says 'done' twice."""
    inv = sent_invoice(biz)
    real_get = biz.paypal.get_invoice
    fail = {"on": False}

    def flaky(invoice_id):
        if fail["on"]:
            raise PayPalError("PayPal 503", status=503)
        return real_get(invoice_id)

    monkeypatch.setattr(biz.paypal, "get_invoice", flaky)
    real_record = biz.paypal.record_payment

    def record_then_fail_reads(*args, **kw):
        out = real_record(*args, **kw)
        fail["on"] = True  # PayPal records the payment, but the read right after it fails
        return out

    monkeypatch.setattr(biz.paypal, "record_payment", record_then_fail_reads)
    a = biz.request_confirmation("record_payment", f"{inv['id']}|4000|CASH")
    assert biz.resolve_confirmation(a["confirmation_id"], True, "es", a["fingerprint"])[0]
    monkeypatch.setattr(biz.paypal, "record_payment", real_record)
    fail["on"] = False
    b = biz.request_confirmation("record_payment", f"{inv['id']}|4000|YAPE")  # the customer pays another 40 by Yape
    # built on a stale balance: PayPal's own balance is checked first, nothing is sent, the books catch up
    assert biz.resolve_confirmation(b["confirmation_id"], True, "es", b["fingerprint"])[0] is False
    c = biz.request_confirmation("record_payment", f"{inv['id']}|4000|YAPE")  # proposed again on the real balance
    assert biz.resolve_confirmation(c["confirmation_id"], True, "es", c["fingerprint"])[0]
    assert [x["status"] for x in biz.authorizations()[:3]] == ["done", "invalidated", "done"]
    biz.refresh_invoice(inv["id"])
    assert biz.invoice_view(inv["id"])["due"] == 10  # 90 - 40 - 40; with the finding PayPal still says 50


# ---------------------------------------------------------------------------------------------- F4
def test_F4_unknown_payout_is_not_resent_after_paypal_forgets_the_ids(biz, monkeypatch):
    """sender_batch_id is unique for 30 days at PayPal and PayPal-Request-Id is kept up to 45 days. An 'unknown'
    payout is re-sent forever with no age limit: after those windows a resend could pay a second time."""
    bill = biz.add_bill("Proveedor", "x", 40, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("timeout", uncertain=True)))
    with pytest.raises(ValidationError):
        biz.pay_bill_with_paypal(bill["id"])
    biz.store.x("UPDATE payouts SET created_at=?, checked_at=0 WHERE bill_id=?", (time.time() - 50 * 86400, bill["id"]))
    calls = []
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: calls.append(a) or {"batch_header": {"payout_batch_id": "B2", "batch_status": "PENDING"}})
    biz.check_payouts()
    assert calls == []  # 50 days later, a resend is no longer a safe idempotent retry


# ---------------------------------------------------------------------------------------------- F5
def test_F5_voice_yes_charges_the_payout_allowance_only_when_a_payout_runs(monkeypatch):
    """/api/chat charges the visitor's daily PayPal allowance on a spoken yes but never gives it back when the
    payout does not run (the button path /api/confirm and /api/confirm/voice both refund it)."""
    from types import SimpleNamespace as NS
    from app import main
    for name in list(main.limits):
        monkeypatch.setitem(main.limits, name, main.RateLimiter(1000))

    def tool(name, args):
        return NS(stop_reason="tool_use", content=[NS(type="tool_use", id="tu1", name=name, input=args)], usage=None)

    def text(t):
        return NS(stop_reason="end_turn", content=[NS(type="text", text=t)], usage=None)

    client = TestClient(main.app)
    ws = client.post("/api/workspace", json={"lang": "es", "tz": "America/Lima", "sample": False}).json()["workspace_id"]
    biz = main.business_factory(ws)
    old = biz.add_bill("Earlier", "x", 10, biz.today().isoformat(), "none", "known@example.com")
    biz.store.x("INSERT INTO payouts(sender_batch_id, ws, bill_id, status, batch_id, amount, checked_at, created_at) VALUES(?,?,?,?,?,?,?,?)",
                ("trusted-" + ws, ws, old["id"], "done", "B-old", 10, 0, 0))
    biz.store.x("UPDATE bills SET created_at=0")
    bill = biz.add_bill("Proveedor", "hilo", 900, biz.today().isoformat(), "none", "known@example.com")
    biz.store.x("UPDATE bills SET created_at=0 WHERE id=?", (bill["id"],))
    scripted = NS(responses=[tool("request_pay_bill", {"bill_id": bill["id"]}), text("Listo.")])
    scripted.beta = NS(messages=NS(create=lambda **kw: scripted.responses.pop(0)))
    monkeypatch.setattr(main, "agent", DestinyPayAgent(main.business_factory, client=scripted))
    conv = "conv-gate6-f5"
    client.post("/api/chat", json={"workspace_id": ws, "conversation_id": conv, "message": "pagale al proveedor", "lang": "es"})
    assert main.agent.conversations.peek(conv, ws).awaiting  # the code read it back and armed the yes
    ip = "testclient"
    before = main.payouts_per_ip.left(ip)
    # PayPal cleanly refuses the payout (e.g. insufficient funds): nothing is paid
    monkeypatch.setattr(main.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("PayPal 422: INSUFFICIENT_FUNDS", status=422, uncertain=False)))
    r = client.post("/api/chat", json={"workspace_id": ws, "conversation_id": conv, "message": "sí", "lang": "es"}).json()
    assert r["actions"][0]["result"]["done"] is False and biz.bill_view(bill["id"])["status"] == "pending"
    assert main.payouts_per_ip.left(ip) == before  # nothing paid, nothing charged


# ------------------------------------------------------------------------------------------- CONTROLS
@pytest.mark.parametrize("lang", ["es", "en", "pt"])
def test_C1_every_gated_kind_has_a_code_readback_in_every_language(biz, lang):
    from app.agent import Conversation, DestinyPayAgent as A
    for kind in GATED_KINDS:
        d = {"customer": "Ana", "payee": "Ana", "email": "a@example.com", "total": 10, "amount": 10, "currency": "USD",
             "method": "CASH" if kind in ("record_payment", "mark_bill_paid") else ""}
        conv = Conversation(ws="w", lang=lang)
        out = A._arm_voice_yes(conv, "ok", [{"action": kind, "details": d, "confirmation_id": "c1", "new_recipient": False}], 1.0)
        assert conv.awaiting == "c1" and "10" in out, (kind, lang)


def test_C2_forged_fingerprint_cannot_void_or_run_a_record_payment(biz):
    inv = sent_invoice(biz)
    req = biz.request_confirmation("record_payment", f"{inv['id']}|1000|CASH")
    for _ in range(3):
        assert biz.resolve_confirmation(req["confirmation_id"], True, "es", "f" * 32)[0] is False
    assert biz.invoice_view(inv["id"])["due"] == 90 and [c["id"] for c in biz.pending_confirmations()] == [req["confirmation_id"]]


def test_C3_other_business_cannot_resolve_or_read_a_confirmation(make_biz):
    a, b = make_biz(), make_biz()
    bill = a.add_bill("P", "x", 10, a.today().isoformat(), "none", "p@example.com")
    req = a.request_confirmation("pay_bill", str(bill["id"]))
    assert b.resolve_confirmation(req["confirmation_id"], True, "es", req["fingerprint"])[0] is False
    assert b.pending_confirmations() == [] and a.bill_view(bill["id"])["status"] == "pending"
    with pytest.raises(ValidationError):
        b.request_confirmation("pay_bill", str(bill["id"]))


def test_C4_target_splicing_is_refused(biz):
    inv = sent_invoice(biz)
    for target in (f"{inv['id']}|9000|PAYPAL", f"{inv['id']}|99999|CASH", f"{inv['id']}|-100|CASH", f"{inv['id']}|10|CASH|x", f"{inv['id']}|1e3|CASH"):
        with pytest.raises((ValidationError, PayPalError)):
            biz.request_confirmation("record_payment", target)


def test_C5_chunked_body_over_the_cap_is_refused(monkeypatch):
    from app import main
    client = TestClient(main.app)

    def gen():
        for _ in range(80):
            yield b"a" * 1024

    r = client.post("/api/chat", content=gen(), headers={"content-type": "application/json"})
    assert r.status_code == 413
