import json
import time
from datetime import timedelta

import pytest

from app.business import Business, ValidationError, normalize_email
from app.fake import FakePayPal
from app.paypal import PayPalError
from app.seed import seed_demo

ITEMS = [{"name": "Chompa de alpaca", "quantity": 2, "unit_price": 45}]


def draft(biz, name="Sarah Miller", email="sarah arroba example punto com", items=ITEMS):
    return biz.draft_invoice(name, email, items, "")


# ---------------------------------------------------------------- memory of contacts
def test_spoken_emails_in_five_languages():
    assert normalize_email("juan arroba correo punto com") == "juan@correo.com"
    assert normalize_email("mateo arroba gmail punto com") == "mateo@gmail.com"
    assert normalize_email("catalina dot rojas at mail dot com") == "catalina.rojas@mail.com"
    assert normalize_email("maria arroba exemplo ponto com ponto br") == "maria@exemplo.com.br"
    assert normalize_email("pierre arobase mail point fr") == "pierre@mail.fr"
    assert normalize_email("wang 艾特 qq 点 com") == "wang@qq.com"


def test_draft_saves_contact_and_reuses_email(biz):
    first = draft(biz)
    assert first["status"] == "DRAFT" and first["email"] == "sarah@example.com" and first["total"] == 90
    again = biz.draft_invoice("sarah", "", [{"name": "Chalina", "quantity": 1, "unit_price": 30}], "")
    assert again["email"] == "sarah@example.com" and again["customer"] == "Sarah Miller"


def test_unknown_customer_without_email_is_asked(biz):
    with pytest.raises(ValidationError, match="no email saved"):
        biz.draft_invoice("Nadie", "", ITEMS, "")


def test_ambiguous_contacts_are_reported(biz):
    biz.save_contact("Ana Ruiz", "ana.ruiz@example.com")
    biz.save_contact("Ana Rojas", "ana.rojas@example.com")
    with pytest.raises(ValidationError, match="Several saved contacts"):
        biz.draft_invoice("Ana", "", ITEMS, "")


def test_duplicate_draft_is_reused(biz):
    a = draft(biz)
    b = draft(biz)
    assert b["duplicate_of"] == a["id"]
    c = draft(biz, items=[{"name": "Chompa de alpaca", "quantity": 3, "unit_price": 45}])
    assert "duplicate_of" not in c


@pytest.mark.parametrize("items, error", [([], "between 1 and 10"), ([{"name": "x", "quantity": 0, "unit_price": 1}], "Quantity"),
                                          ([{"name": "x", "quantity": 1, "unit_price": -1}], "Unit price"),
                                          ([{"name": "x", "quantity": 2, "unit_price": 9000}], "limited")])
def test_item_validation(biz, items, error):
    with pytest.raises(ValidationError, match=error):
        draft(biz, items=items)


def test_invoices_are_isolated_between_businesses(make_biz):
    a, b = make_biz(), make_biz()
    inv = draft(a)
    assert b.list_invoices() == []
    for call in (lambda: b.refresh_invoice(inv["id"]), lambda: b.record_payment(inv["id"], 1),
                 lambda: b.request_confirmation("send_invoice", inv["id"]), lambda: b.qr_code(inv["id"])):
        with pytest.raises(PayPalError, match="does not belong"):
            call()


# ---------------------------------------------------------------- confirmations by code
def test_send_requires_owner_confirmation(biz):
    inv = draft(biz)
    req = biz.request_confirmation("send_invoice", inv["id"])
    assert req["status"] == "awaiting_owner_confirmation" and req["details"]["total"] == 90
    assert biz.refresh_invoice(inv["id"])["status"] == "DRAFT"  # nothing sent yet
    again = biz.request_confirmation("send_invoice", inv["id"])
    assert again["confirmation_id"] == req["confirmation_id"]  # one card per action
    done, reply, result = biz.resolve_confirmation(req["confirmation_id"], True, "es")
    assert done and "Sarah Miller" in reply and result["status"] == "UNPAID" and result["payer_link"]
    done2, reply2, _ = biz.resolve_confirmation(req["confirmation_id"], True, "es")
    assert not done2 and "ya no está vigente" in reply2  # a second click never sends twice


def test_denied_and_expired_confirmations(biz):
    inv = draft(biz)
    req = biz.request_confirmation("send_invoice", inv["id"])
    done, reply, _ = biz.resolve_confirmation(req["confirmation_id"], False, "en")
    assert not done and reply == "Understood, I won't do it."
    req2 = biz.request_confirmation("send_invoice", inv["id"])
    biz.store.x("UPDATE confirmations SET created_at=? WHERE id=?", (time.time() - 3600, req2["confirmation_id"]))
    assert biz.pending_confirmations() == []
    assert not biz.resolve_confirmation(req2["confirmation_id"], True, "en")[0]
    assert biz.refresh_invoice(inv["id"])["status"] == "DRAFT"


def test_confirmation_rejects_wrong_states(biz):
    inv = draft(biz)
    with pytest.raises(ValidationError, match="Only sent"):
        biz.request_confirmation("cancel_invoice", inv["id"])
    biz.send_invoice(inv["id"])
    with pytest.raises(ValidationError, match="not a draft"):
        biz.request_confirmation("send_invoice", inv["id"])
    req = biz.request_confirmation("cancel_invoice", inv["id"])
    assert biz.resolve_confirmation(req["confirmation_id"], True, "pt")[0]
    assert biz.refresh_invoice(inv["id"])["status"] == "CANCELLED"


def test_failed_execution_is_reported(biz, monkeypatch):
    inv = draft(biz)
    req = biz.request_confirmation("send_invoice", inv["id"])
    monkeypatch.setattr(biz.paypal, "send_invoice", lambda i: (_ for _ in ()).throw(PayPalError("PayPal 503: busy")))
    done, reply, _ = biz.resolve_confirmation(req["confirmation_id"], True, "es")
    assert not done and "503" in reply
    assert biz.store.one("SELECT status FROM confirmations WHERE id=?", (req["confirmation_id"],))["status"] == "failed"


# ---------------------------------------------------------------- payments in
def test_record_payment_and_online_payment_feed_income(biz):
    inv = draft(biz)
    biz.send_invoice(inv["id"])
    part = biz.record_payment(inv["id"], 40, "YAPE", "")
    assert part["status"] == "PARTIALLY_PAID" and part["due"] == 50
    biz.demo_mark_paid(inv["id"])
    biz.refresh_invoice(inv["id"])  # refreshing again must not count the payment twice
    o = biz.overview("month")
    assert o["income"] == 90 and o["receivable"] == 0
    alerts = biz.store.alerts_since(biz.ws, 0)
    assert [a["key"] for a in alerts] == ["a_invoice_paid"]


def test_overdue_invoices(biz):
    inv = biz.draft_invoice("Lucho", "lucho@example.com", ITEMS, "", due_in_days=0)
    biz.send_invoice(inv["id"])
    biz.store.x("UPDATE invoices SET due_date=? WHERE id=?", ((biz.today() - timedelta(days=2)).isoformat(), inv["id"]))
    assert [i["id"] for i in biz.list_invoices("overdue")] == [inv["id"]]
    assert biz.overview()["overdue_invoices"][0]["customer"] == "Lucho"


# ---------------------------------------------------------------- bills and payouts
def test_bills_recurrence_and_expenses(biz):
    rent = biz.add_bill("Marta Quispe", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    assert rent["status"] == "pending" and rent["payee_email"] == "marta@example.com"
    biz.mark_bill_paid(rent["id"], "BANK_TRANSFER")
    pending = biz.list_bills("pending")
    assert len(pending) == 1 and pending[0]["due_date"] > rent["due_date"]  # next month created
    assert biz.overview()["expense"] == 300
    with pytest.raises(ValidationError, match="already paid"):
        biz.mark_bill_paid(rent["id"])


def test_pay_bill_with_paypal_only_once(biz):
    bill = biz.add_bill("Textiles Andinos", "Hilo", 180, biz.today().isoformat(), "none", "proveedor@example.com")
    req = biz.request_confirmation("pay_bill", str(bill["id"]))
    assert req["details"]["amount"] == 180
    done, reply, result = biz.resolve_confirmation(req["confirmation_id"], True, "es")
    assert done and result["payout_batch_id"] and "Textiles Andinos" in reply
    assert biz.bill_view(bill["id"])["status"] == "paid"
    with pytest.raises(ValidationError):
        biz.pay_bill_with_paypal(bill["id"])
    assert len(biz.paypal.payouts) == 1


def test_payout_failure_allows_retry_and_duplicate_is_blocked(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    original = biz.paypal.create_payout
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("PayPal 500: down")))
    with pytest.raises(PayPalError):
        biz.pay_bill_with_paypal(bill["id"])
    monkeypatch.setattr(biz.paypal, "create_payout", original)
    assert biz.pay_bill_with_paypal(bill["id"])["status"] == "paid"


def test_pay_bill_needs_email(biz):
    bill = biz.add_bill("Movistar", "Internet", 45, biz.today().isoformat())
    with pytest.raises(ValidationError, match="PayPal email"):
        biz.request_confirmation("pay_bill", str(bill["id"]))


# ---------------------------------------------------------------- reminders, facts, overview
def test_reminders_fire_once(biz):
    soon = (biz.now() + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
    r = biz.create_reminder("Llamar al banco", soon)
    biz.store.x("UPDATE reminders SET due_at=? WHERE id=?", (time.time() - 1, r["id"]))
    assert biz.fire_due_reminders() == 1 and biz.fire_due_reminders() == 0
    assert biz.store.alerts_since(biz.ws, 0)[0]["params"]["text"] == "Llamar al banco"
    with pytest.raises(ValidationError, match="already passed"):
        biz.create_reminder("tarde", "2020-01-01T10:00")


def test_scheduled_customer_reminder(biz):
    inv = draft(biz)
    with pytest.raises(ValidationError, match="Only sent"):
        biz.create_reminder("Pagar", (biz.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), "remind_customer", inv["id"])
    biz.send_invoice(inv["id"])
    r = biz.create_reminder("Pagar", (biz.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), "remind_customer", inv["id"])
    biz.store.x("UPDATE reminders SET due_at=? WHERE id=?", (time.time() - 1, r["id"]))
    biz.fire_due_reminders()
    assert biz.store.alerts_since(biz.ws, 0)[-1]["key"] == "a_customer_reminded"


def test_overview_cashflow_and_briefing(biz):
    inv = draft(biz)
    biz.send_invoice(inv["id"])
    biz.add_bill("Marta", "Alquiler", 300, (biz.today() + timedelta(days=3)).isoformat())
    biz.record_movement("expense", "Taxi", 10, "CASH", "transport", "")
    o = biz.overview()
    assert o["receivable"] == 90 and o["bills_next_7_days"] == 300 and o["projected_30_days"] == -210
    assert o["top_expense_categories"][0]["category"] == "transport"
    flow = biz.cashflow(30)
    assert len(flow) == 30 and sum(d["out"] for d in flow) == 300 and sum(d["in"] for d in flow) == 90
    text = biz.briefing("es")
    assert "90.00 USD" in text and "Marta" in text
    assert "Marta" in biz.briefing("zh")


def test_memory_snapshot_and_facts(biz):
    biz.remember("Sarah siempre paga tarde")
    draft(biz)
    snap = biz.memory_snapshot()
    assert "Sarah Miller <sarah@example.com>" in snap and "Sarah siempre paga tarde" in snap


def test_seed_demo_business(make_biz):
    biz = make_biz(seed=True)
    seed_demo(biz, "en")
    o = biz.overview("month")
    assert len(biz.list_bills("pending")) == 4 and o["bills_overdue"]
    assert biz.find_contacts("sarah")[0]["email"] == "sarah.miller@example.com"
    assert len(biz.facts()) == 3 and len(biz.list_reminders()) == 1


def test_demo_paypal_survives_restart(store):
    p1 = FakePayPal(store)
    inv = p1.create_invoice("Ana", "ana@example.com", ITEMS)
    p2 = FakePayPal(store)  # a new process
    assert p2.get_invoice(inv["id"])["status"] == "DRAFT"
    assert p2.create_invoice("Ana", "ana@example.com", ITEMS)["id"] != inv["id"]
