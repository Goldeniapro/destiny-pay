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
def test_spoken_emails_in_three_languages():
    assert normalize_email("juan arroba correo punto com") == "juan@correo.com"
    assert normalize_email("mateo arroba gmail punto com") == "mateo@gmail.com"
    assert normalize_email("catalina dot rojas at mail dot com") == "catalina.rojas@mail.com"
    assert normalize_email("maria arroba exemplo ponto com ponto br") == "maria@exemplo.com.br"


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


def test_payout_rejected_allows_retry(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    original = biz.paypal.create_payout
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("PayPal 422: RECEIVER_INVALID", status=422, uncertain=False)))
    with pytest.raises(PayPalError):
        biz.pay_bill_with_paypal(bill["id"])
    assert biz.bill_view(bill["id"])["status"] == "pending"  # PayPal said no: nothing was paid
    monkeypatch.setattr(biz.paypal, "create_payout", original)
    assert biz.pay_bill_with_paypal(bill["id"])["status"] == "paid"


def test_payout_timeout_blocks_paying_twice(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    paid = []

    def flaky(sender_batch_id, email, amount, **k):
        paid.append(sender_batch_id)  # PayPal processed it...
        raise PayPalError("Could not reach PayPal: ReadTimeout")  # ...but we never heard back

    monkeypatch.setattr(biz.paypal, "create_payout", flaky)
    with pytest.raises(ValidationError, match="may or may not"):
        biz.pay_bill_with_paypal(bill["id"])
    assert biz.bill_view(bill["id"])["status"] == "processing"
    with pytest.raises(ValidationError, match="in progress"):
        biz.mark_bill_paid(bill["id"], "CASH")  # no cash payment on top of a possible PayPal one
    assert biz.store.alerts_since(biz.ws, 0)[-1]["key"] == "a_payout_unknown"

    def already(sender_batch_id, email, amount, **k):
        raise PayPalError('PayPal 400: {"name":"USER_BUSINESS_ERROR","details":[{"issue":"SENDER_BATCH_ID_ALREADY_USED"}],'
                          '"links":[{"href":"https://api-m.sandbox.paypal.com/v1/payments/payouts/BATCH77","rel":"self"}]}')

    monkeypatch.setattr(biz.paypal, "create_payout", already)
    monkeypatch.setattr(biz.paypal, "get_payout", lambda bid: {"batch_header": {"payout_batch_id": bid, "batch_status": "SUCCESS"},
                                                               "items": [{"transaction_status": "SUCCESS"}]} if bid == "BATCH77" else {})
    biz.check_payouts()  # PayPal links the first attempt: its real status decides
    assert biz.bill_view(bill["id"])["status"] == "paid" and len(paid) == 1
    assert biz.overview()["expense"] == 50


def test_pending_payout_settles_or_rolls_back_from_paypal_status(biz, monkeypatch):
    rent = biz.add_bill("Marta", "Alquiler", 300, biz.today().isoformat(), "monthly", "marta@example.com")
    yarn = biz.add_bill("Textiles", "Hilo", 100, biz.today().isoformat(), "none", "t@example.com")
    pending = {"batch_header": {"payout_batch_id": "B", "batch_status": "PENDING"}}
    monkeypatch.setattr(biz.paypal, "create_payout", lambda sbid, *a, **k: {"batch_header": {"payout_batch_id": "B-" + sbid, "batch_status": "PENDING"}})
    req = biz.request_confirmation("pay_bill", str(rent["id"]))
    done, reply, result = biz.resolve_confirmation(req["confirmation_id"], True, "es")
    assert done and "Te aviso cuando PayPal lo confirme" in reply and result["status"] == "processing"
    assert biz.overview()["expense"] == 0  # not counted until PayPal confirms
    biz.pay_bill_with_paypal(yarn["id"])
    from app.business import _tag
    statuses = {f"B-dp{_tag(biz.ws)}b{rent['id']}a1": "SUCCESS", f"B-dp{_tag(biz.ws)}b{yarn['id']}a1": "RETURNED"}
    monkeypatch.setattr(biz.paypal, "get_payout", lambda bid: {"batch_header": {"payout_batch_id": bid, "batch_status": "SUCCESS"},
                                                               "items": [{"transaction_status": statuses[bid]}]})
    biz.check_payouts()
    assert biz.bill_view(rent["id"])["status"] == "paid" and biz.bill_view(yarn["id"])["status"] == "pending"
    assert biz.overview()["expense"] == 300 and len(biz.list_bills("pending")) == 2  # next month's rent + the returned one
    keys = [a["key"] for a in biz.store.alerts_since(biz.ws, 0)]
    assert "a_payout_done" in keys and "a_payout_failed" in keys
    biz.check_payouts()
    assert biz.overview()["expense"] == 300  # checking again never counts twice


def test_payout_limits(biz):
    big = biz.add_bill("Proveedor", "x", 5000, biz.today().isoformat(), "none", "p@example.com")
    with pytest.raises(ValidationError, match="limited"):
        biz.pay_bill_with_paypal(big["id"])
    for i in range(3):
        b = biz.add_bill(f"P{i}", "x", 1000, biz.today().isoformat(), "none", f"p{i}@example.com")
        if i < 3:
            biz.pay_bill_with_paypal(b["id"])
    extra = biz.add_bill("P9", "x", 10, biz.today().isoformat(), "none", "p9@example.com")
    with pytest.raises(ValidationError, match="today's limit"):
        biz.pay_bill_with_paypal(extra["id"])


def test_confirmation_never_stuck_and_refresh_failure_is_not_a_failure(biz, monkeypatch):
    inv = draft(biz)
    req = biz.request_confirmation("send_invoice", inv["id"])
    real_get = biz.paypal.get_invoice
    calls = {"n": 0}

    def flaky_get(i):
        calls["n"] += 1
        raise PayPalError("PayPal 503")

    monkeypatch.setattr(biz.paypal, "get_invoice", flaky_get)
    done, reply, result = biz.resolve_confirmation(req["confirmation_id"], True, "en")
    assert done and result["status"] == "SENT"  # PayPal sent it; the local copy catches up later
    monkeypatch.setattr(biz.paypal, "get_invoice", real_get)
    inv2 = draft(biz, name="Otro", email="otro@example.com")
    req2 = biz.request_confirmation("send_invoice", inv2["id"])
    monkeypatch.setattr(biz.paypal, "send_invoice", lambda i: (_ for _ in ()).throw(KeyError("bug")))
    done, reply, _ = biz.resolve_confirmation(req2["confirmation_id"], True, "en")
    assert not done and "unexpected" in reply
    assert biz.store.one("SELECT status FROM confirmations WHERE id=?", (req2["confirmation_id"],))["status"] == "failed"


def test_record_payment_uses_paypal_payment_id(biz, monkeypatch):
    inv = draft(biz)
    biz.send_invoice(inv["id"])
    ids = iter(["P1", "P2"])
    original = biz.paypal.record_payment

    def rec(i, amount, method="CASH", note=""):
        original(i, amount, method, note)
        return {"payment_id": next(ids)}

    monkeypatch.setattr(biz.paypal, "record_payment", rec)
    biz.record_payment(inv["id"], 10, "CASH")
    biz.record_payment(inv["id"], 10, "CASH")
    assert biz.overview()["income"] == 20


def test_monthly_bill_keeps_its_day(biz):
    bill = biz.add_bill("Marta", "Alquiler", 300, "2026-12-31", "monthly")
    biz.store.x("UPDATE bills SET due_date='2026-12-31' WHERE id=?", (bill["id"],))
    biz.mark_bill_paid(bill["id"])
    jan = biz.list_bills("pending")[0]
    assert jan["due_date"] == "2027-01-31"
    biz.mark_bill_paid(jan["id"])
    assert biz.list_bills("pending")[0]["due_date"] == "2027-02-28"
    feb = biz.list_bills("pending")[0]
    biz.mark_bill_paid(feb["id"])
    assert biz.list_bills("pending")[0]["due_date"] == "2027-03-31"


def test_pending_bills_stay_visible_after_many_paid(biz):
    for i in range(205):
        biz.store.x("INSERT INTO bills(ws,payee,description,amount,currency,due_date,status,created_at,paid_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (biz.ws, f"Old{i}", "", 1, "USD", "2026-01-01", "paid", 0, 0))
    b = biz.add_bill("Nuevo", "x", 10, (biz.today() + timedelta(days=3)).isoformat())
    assert [x["id"] for x in biz.list_bills("pending")] == [b["id"]]


def test_payment_announced_once_even_with_concurrent_refreshes(biz):
    inv = draft(biz)
    biz.send_invoice(inv["id"])
    biz.paypal.mark_paid(inv["id"])
    biz.refresh_invoice(inv["id"])
    biz.store.x("UPDATE invoices SET status='PAID' WHERE id=?", (inv["id"],))
    biz.refresh_invoice(inv["id"])
    assert [a["key"] for a in biz.store.alerts_since(biz.ws, 0)].count("a_invoice_paid") == 1


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
    assert "90,00 USD" in text and "Marta" in text and "90.00 USD" in biz.briefing("en")
    assert "Marta" in biz.briefing("pt")


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


def test_already_used_without_link_stays_in_progress(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("Could not reach PayPal: ReadTimeout")))
    with pytest.raises(ValidationError):
        biz.pay_bill_with_paypal(bill["id"])
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("PayPal 400: SENDER_BATCH_ID_ALREADY_USED")))
    biz.check_payouts()
    assert biz.bill_view(bill["id"])["status"] == "processing" and biz.overview()["expense"] == 0  # never assumed paid
    with pytest.raises(ValidationError, match="in progress"):
        biz.mark_bill_paid(bill["id"], "CASH")
    biz.store.x("UPDATE payouts SET created_at=? WHERE bill_id=?", (time.time() - 3600, bill["id"]))
    assert biz.mark_bill_paid(bill["id"], "PAYPAL")["status"] == "paid"  # the owner checked PayPal and saw it
    assert biz.overview()["expense"] == 50


def test_cash_cannot_settle_a_bill_with_payout_in_flight(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda sbid, *a, **k: {"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})
    stale = biz._bill(bill["id"])  # read while still pending
    biz.pay_bill_with_paypal(bill["id"])
    with pytest.raises(ValidationError, match="already paid"):
        biz._settle_bill(stale, "CASH", "")  # what a racing mark_bill_paid would do
    assert biz.bill_view(bill["id"])["status"] == "processing"


def test_unclaimed_payout_returned_later_is_undone(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 100, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda sbid, *a, **k: {"batch_header": {"payout_batch_id": "BU", "batch_status": "PENDING"}})
    state = {"s": "UNCLAIMED"}
    monkeypatch.setattr(biz.paypal, "get_payout", lambda bid: {"batch_header": {"payout_batch_id": bid, "batch_status": "SUCCESS"},
                                                               "items": [{"transaction_status": state["s"]}]})
    biz.pay_bill_with_paypal(bill["id"])
    biz.check_payouts()
    assert biz.bill_view(bill["id"])["status"] == "paid" and biz.overview()["expense"] == 100
    state["s"] = "RETURNED"
    biz.store.x("UPDATE payouts SET checked_at=0")
    biz.check_payouts()
    assert biz.bill_view(bill["id"])["status"] == "pending" and biz.overview()["expense"] == 0
    assert biz.store.alerts_since(biz.ws, 0)[-1]["key"] == "a_payout_failed"


def test_failed_payout_can_be_retried(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda sbid, *a, **k: {"batch_header": {"payout_batch_id": "BF", "batch_status": "DENIED"}})
    with pytest.raises(ValidationError, match="denied"):
        biz.pay_bill_with_paypal(bill["id"])
    assert biz.bill_view(bill["id"])["status"] == "pending"
    monkeypatch.undo()
    assert biz.pay_bill_with_paypal(bill["id"])["status"] == "paid"


def test_unknown_payout_never_reopened_by_a_failed_resend(biz, monkeypatch):
    """Security audit H1: a re-send error can't prove the first attempt didn't pay, so the bill stays blocked."""
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad json")))
    with pytest.raises(ValidationError):
        biz.pay_bill_with_paypal(bill["id"])  # unexpected error: treated as unknown, never forgotten
    for err in (PayPalError("PayPal 422: INSUFFICIENT_FUNDS", status=422, uncertain=False), PayPalError("PayPal 429", status=429)):
        monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, e=err, **k: (_ for _ in ()).throw(e))
        biz.store.x("UPDATE payouts SET checked_at=0 WHERE ws=?", (biz.ws,))
        biz.check_payouts()
        assert biz.bill_view(bill["id"])["status"] == "processing"
    with pytest.raises(ValidationError):
        biz.request_confirmation("pay_bill", str(bill["id"]))  # can't be paid again while PayPal might have it


def test_rate_limited_payout_is_unknown_not_failed(biz, monkeypatch):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda *a, **k: (_ for _ in ()).throw(PayPalError("PayPal 429", status=429)))
    with pytest.raises(ValidationError, match="may or may not"):
        biz.pay_bill_with_paypal(bill["id"])
    assert biz.bill_view(bill["id"])["status"] == "processing"


def test_parallel_payouts_respect_the_daily_cap(make_biz, monkeypatch):
    """Security audit M1: the cap check and the payout row are one atomic step."""
    import threading
    from app import business as bmod
    biz = make_biz()
    monkeypatch.setattr(bmod, "PAYOUT_DAILY_WS", 1000.0)
    bills = [biz.add_bill(f"P{i}", "x", 300, biz.today().isoformat(), "none", f"p{i}@example.com") for i in range(8)]
    slow = biz.paypal.create_payout

    def create(*a, **k):
        time.sleep(0.02)
        return slow(*a, **k)

    monkeypatch.setattr(biz.paypal, "create_payout", create)
    def run(b):
        try:
            biz.pay_bill_with_paypal(b["id"])
        except Exception:
            pass

    threads = [threading.Thread(target=run, args=(b,)) for b in bills]
    [t.start() for t in threads]
    [t.join() for t in threads]
    total = biz.store.one("SELECT COALESCE(SUM(amount),0) s FROM payouts WHERE ws=? AND status!='failed'", (biz.ws,))["s"]
    assert total <= 1000


def test_stale_starting_payout_becomes_unknown(biz):
    bill = biz.add_bill("Proveedor", "x", 50, biz.today().isoformat(), "none", "p@example.com")
    biz.store.x("UPDATE bills SET status='processing' WHERE id=?", (bill["id"],))
    biz.store.x("INSERT INTO payouts(sender_batch_id,ws,bill_id,status,amount,created_at) VALUES(?,?,?,?,?,?)",
                ("dpX", biz.ws, bill["id"], "starting", 50, time.time() - 200))
    biz.check_payouts()  # re-sends with the same id (FakePayPal accepts it once) and settles from PayPal's answer
    assert biz.store.one("SELECT status FROM payouts WHERE sender_batch_id='dpX'")["status"] in ("done", "PENDING", "SUCCESS")
    assert biz.bill_view(bill["id"])["status"] == "paid"


def test_online_income_uses_ledger_not_stale_due(biz, monkeypatch):
    inv = draft(biz, items=[{"name": "x", "quantity": 1, "unit_price": 100}])
    biz.send_invoice(inv["id"])
    real_get = biz.paypal.get_invoice
    monkeypatch.setattr(biz.paypal, "get_invoice", lambda i: (_ for _ in ()).throw(PayPalError("PayPal 503")))
    biz.record_payment(inv["id"], 40, "CASH")  # the refresh after it fails: local due stays stale
    monkeypatch.setattr(biz.paypal, "get_invoice", real_get)
    biz.paypal.mark_paid(inv["id"])
    biz.refresh_invoice(inv["id"])
    assert biz.overview()["income"] == 100


def test_demo_partial_payments_all_counted(biz):
    inv = draft(biz, items=[{"name": "x", "quantity": 1, "unit_price": 100}])
    biz.send_invoice(inv["id"])
    biz.record_payment(inv["id"], 40, "CASH")
    biz.record_payment(inv["id"], 60, "YAPE")
    assert biz.overview()["income"] == 100 and biz.invoice_view(inv["id"])["status"] == "MARKED_AS_PAID"


def test_purge_forgets_idle_businesses_but_not_pending_work(make_biz):
    import time as _t
    idle, busy = make_biz(), make_biz()
    idle.record_movement("income", "venta", 10)
    from datetime import timedelta
    busy.create_reminder("llamar al banco", (busy.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M"))
    idle.store.x("UPDATE workspaces SET last_seen=? WHERE id IN (?,?)", (_t.time() - 3 * 86400, idle.ws, busy.ws))
    idle.store.purge(48 * 3600)
    assert idle.store.workspace(idle.ws) is None and idle.store.q("SELECT * FROM movements WHERE ws=?", (idle.ws,)) == []
    assert busy.store.workspace(busy.ws) is not None  # a reminder still to fire keeps the business


def test_receivables_aging_trust_note_and_hold_risk(biz):
    from datetime import timedelta
    small = biz.draft_invoice("Ana", "ana@example.com", [{"name": "x", "quantity": 1, "unit_price": 100}], "")
    biz.send_invoice(small["id"])
    assert "Test Shop nunca te pedirá llamar" in biz.paypal.get_invoice(small["id"])["detail"]["note"]  # anti-phishing line
    late = (biz.today() - timedelta(days=40)).isoformat()
    biz.store.x("UPDATE invoices SET due_date=? WHERE id=?", (late, small["id"]))
    assert biz.overview()["receivable_aging"] == {"not_due": 0.0, "d1_30": 0.0, "d31_60": 100.0, "d61_plus": 0.0}
    big = biz.draft_invoice("Luis", "luis@example.com", [{"name": "lote", "quantity": 1, "unit_price": 900}], "")
    assert "hold funds" in big["risk_note"]  # 9x the usual invoice: warn, never promise
    normal = biz.draft_invoice("Eva", "eva@example.com", [{"name": "y", "quantity": 1, "unit_price": 120}], "")
    assert "risk_note" not in normal


def test_unclaimed_payout_gets_one_follow_up(biz, monkeypatch):
    import time as _t
    bill = biz.add_bill("Textiles", "Hilo", 100, biz.today().isoformat(), "none", "t@example.com")
    monkeypatch.setattr(biz.paypal, "create_payout", lambda sbid, *a, **k: {"batch_header": {"payout_batch_id": "B1", "batch_status": "PENDING"}})
    monkeypatch.setattr(biz.paypal, "get_payout", lambda bid: {"batch_header": {"payout_batch_id": bid, "batch_status": "SUCCESS"},
                                                               "items": [{"transaction_status": "UNCLAIMED"}]})
    req = biz.request_confirmation("pay_bill", str(bill["id"]))
    biz.resolve_confirmation(req["confirmation_id"], True, "es")
    biz.check_payouts()
    biz.store.x("UPDATE payouts SET created_at=?, checked_at=0 WHERE ws=?", (_t.time() - 4 * 86400, biz.ws))
    biz.check_payouts(); biz.check_payouts()
    keys = [a["key"] for a in biz.store.alerts_since(biz.ws, 0)]
    assert keys.count("a_payout_unclaimed_follow") == 1


def test_invoicing_never_overwrites_a_suppliers_saved_email(biz):
    biz.save_contact("Textiles Andinos", "pagos@textiles.example.com", "supplier")
    biz.draft_invoice("Textiles Andinos", "otro@example.com", [{"name": "x", "quantity": 1, "unit_price": 10}], "")
    c = biz.find_contacts("Textiles Andinos")[0]
    assert c["email"] == "pagos@textiles.example.com" and c["kind"] == "supplier"
