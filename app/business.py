"""The business domain: everything Destiny Pay can know and do for one workspace (one business).

Tools call these methods; money-moving actions (send, cancel, pay) run only through
`resolve_confirmation`, which is triggered by the owner (button or spoken "yes"), never by the model.
"""

from __future__ import annotations

import difflib
import json
import re
import time
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import i18n
from .paypal import FINAL_STATUSES, PAID_STATUSES, PAYMENT_METHODS, UNPAID_STATUSES, PayPalError, summarize
from .store import Store

MAX_INVOICES = 60
MAX_BILLS = 200
MAX_FACTS = 100
CONFIRMATION_TTL = 15 * 60
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_SPOKEN = [
    (r"\s*\b(arroba|at|arobase)\b\s*", "@"), (r"\s*(艾特|圈a)\s*", "@"),
    (r"\s*\b(punto|dot|ponto|point)\b\s*", "."), (r"\s*点\s*", "."),
    (r"\s*\b(guion bajo|guión bajo|underscore|tiret bas)\b\s*", "_"), (r"\s*下划线\s*", "_"),
    (r"\s*\b(guion|guión|dash|hífen|hifen|tiret)\b\s*", "-"),
]
METHODS = {"CASH", "BANK_TRANSFER", "YAPE", "PLIN", "CARD", "PAYPAL", "OTHER"}


class ValidationError(ValueError):
    pass


def normalize_email(raw: str) -> str:
    e = raw.strip().lower()
    if "@" not in e:
        for pat, rep in _SPOKEN:
            e = re.sub(pat, rep, e)
    return re.sub(r"\s+", "", e)


def _clean(text: str, limit: int) -> str:
    return " ".join(str(text or "").split())[:limit]


class Business:
    def __init__(self, store: Store, paypal, ws: str) -> None:
        self.store, self.paypal, self.ws = store, paypal, ws
        row = store.workspace(ws)
        if row is None:
            raise ValidationError("Unknown workspace")
        self.info = row
        self.currency = getattr(paypal, "currency", None) or row["currency"]
        try:
            self.tz = ZoneInfo(row["tz"])
        except Exception:
            self.tz = ZoneInfo("America/Lima")

    # --- time ------------------------------------------------------------------------------
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def today(self) -> date:
        return self.now().date()

    def _parse_date(self, value: str) -> date:
        try:
            d = date.fromisoformat(str(value)[:10])
        except ValueError:
            raise ValidationError(f"'{value}' is not a date. Use YYYY-MM-DD.")
        if not (self.today() - timedelta(days=366)) <= d <= (self.today() + timedelta(days=731)):
            raise ValidationError("That date is too far away.")
        return d

    def _parse_datetime(self, value: str) -> datetime:
        try:
            dt = datetime.fromisoformat(str(value).strip())
        except ValueError:
            raise ValidationError(f"'{value}' is not a date and time. Use YYYY-MM-DDTHH:MM in the business's local time.")
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=self.tz)
        return dt

    # --- activity -------------------------------------------------------------------------
    def log(self, kind: str, text: str, **data) -> None:
        self.store.log(self.ws, kind, text, data)

    # --- contacts (memory of people) --------------------------------------------------------
    def find_contacts(self, query: str, limit: int = 5) -> list[dict]:
        q = _clean(query, 80).lower()
        rows = self.store.q("SELECT * FROM contacts WHERE ws=? ORDER BY name", (self.ws,))
        if not q:
            return [self._contact_view(r) for r in rows[:limit]]
        scored = []
        for r in rows:
            name, email = r["name"].lower(), r["email"].lower()
            ratio = max(difflib.SequenceMatcher(None, q, name).ratio(),
                        max((difflib.SequenceMatcher(None, q, part).ratio() for part in name.split()), default=0))
            if q in name or q in email:
                ratio = max(ratio, 0.95)
            if ratio >= 0.6:
                scored.append((ratio, r))
        scored.sort(key=lambda x: -x[0])
        return [self._contact_view(r) for _, r in scored[:limit]]

    def _contact_view(self, r: dict) -> dict:
        inv = self.store.q("SELECT id,status,total,due,currency,created_at FROM invoices WHERE ws=? AND contact=? COLLATE NOCASE ORDER BY created_at DESC LIMIT 3",
                           (self.ws, r["name"]))
        return {"name": r["name"], "email": r["email"], "kind": r["kind"], "notes": r["notes"],
                "recent_invoices": [{"id": i["id"], "status": i["status"], "total": i["total"], "due": i["due"]} for i in inv]}

    def save_contact(self, name: str, email: str = "", kind: str = "customer", notes: str = "") -> dict:
        name = _clean(name, 140)
        if not name:
            raise ValidationError("A contact needs a name.")
        email = normalize_email(email) if email else ""
        if email and not EMAIL_RE.match(email):
            raise ValidationError(f"'{email}' is not a valid email address.")
        kind = kind if kind in ("customer", "supplier") else "customer"
        existing = self.store.one("SELECT * FROM contacts WHERE ws=? AND name=? COLLATE NOCASE", (self.ws, name))
        if existing:
            self.store.x("UPDATE contacts SET email=?, kind=?, notes=? WHERE id=?",
                         (email or existing["email"], kind or existing["kind"], _clean(notes, 300) or existing["notes"], existing["id"]))
        else:
            if self.store.one("SELECT COUNT(*) n FROM contacts WHERE ws=?", (self.ws,))["n"] >= 500:
                raise ValidationError("Contact limit reached.")
            self.store.x("INSERT INTO contacts(ws,name,email,kind,notes,created_at) VALUES(?,?,?,?,?,?)",
                         (self.ws, name, email, kind, _clean(notes, 300), time.time()))
        return self._contact_view(self.store.one("SELECT * FROM contacts WHERE ws=? AND name=? COLLATE NOCASE", (self.ws, name)))

    def _resolve_email(self, name: str, email: str) -> tuple[str, str]:
        """Use the dictated email, or the one remembered for this customer."""
        if email.strip():
            e = normalize_email(email)
            if not EMAIL_RE.match(e):
                raise ValidationError(f"'{e}' is not a valid email address. Ask the owner to repeat it.")
            return name, e
        matches = [c for c in self.find_contacts(name) if c["email"]]
        exact = [c for c in matches if c["name"].lower() == name.lower()]
        pick = exact[0] if len(exact) == 1 else (matches[0] if len(matches) == 1 else None)
        if pick is None:
            if len(matches) > 1:
                raise ValidationError("Several saved contacts match: " + ", ".join(f"{c['name']} <{c['email']}>" for c in matches)
                                      + ". Ask the owner which one.")
            raise ValidationError(f"I have no email saved for {name}. Ask the owner for it.")
        return pick["name"], pick["email"]

    # --- invoices (receivables) ---------------------------------------------------------
    def validate_items(self, items: list[dict]) -> tuple[list[dict], float]:
        if not 1 <= len(items or []) <= 10:
            raise ValidationError("An invoice needs between 1 and 10 items.")
        clean, total = [], 0.0
        for it in items:
            item_name = _clean(it.get("name", ""), 200)
            qty, price = float(it.get("quantity", 0)), float(it.get("unit_price", 0))
            if not item_name:
                raise ValidationError("Every item needs a name.")
            if not 0 < qty <= 1000:
                raise ValidationError(f"Quantity {qty:g} for '{item_name}' is not valid (must be between 0 and 1000).")
            if not 0 < price <= 10000:
                raise ValidationError(f"Unit price {price:g} for '{item_name}' is not valid (must be between 0 and 10,000).")
            clean.append({"name": item_name, "quantity": qty, "unit_price": round(price, 2)})
            total += qty * price
        if total > 10000:
            raise ValidationError("Demo invoices are limited to 10,000 per invoice.")
        return clean, round(total, 2)

    def draft_invoice(self, customer_name: str, customer_email: str, items: list[dict], note: str = "", due_in_days: int = 7) -> dict:
        name = _clean(customer_name, 140)
        if not name:
            raise ValidationError("Customer name is missing.")
        name, email = self._resolve_email(name, customer_email or "")
        clean, total = self.validate_items(items)
        n = self.store.one("SELECT COUNT(*) n FROM invoices WHERE ws=?", (self.ws,))["n"]
        if n >= MAX_INVOICES:
            raise PayPalError(f"Demo limit reached: this business already created {MAX_INVOICES} invoices.")
        recent = self.store.one(
            "SELECT id, summary FROM invoices WHERE ws=? AND contact=? COLLATE NOCASE AND status='DRAFT' AND total=? AND created_at>?",
            (self.ws, name, total, time.time() - 120))
        if recent and json.loads(recent["summary"]).get("items_key") == json.dumps(clean, sort_keys=True):
            return {**self.invoice_view(recent["id"]), "duplicate_of": recent["id"],
                    "message": "An identical draft was just created; reuse it instead of creating another."}
        inv = self.paypal.create_invoice(name, email, clean, _clean(note, 500), reference=f"dp-{self.ws[:12]}")
        due_date = (self.today() + timedelta(days=max(0, min(int(due_in_days or 7), 90)))).isoformat()
        now = time.time()
        self.store.x("INSERT INTO invoices(id,ws,contact,status,total,due,currency,due_date,summary,created_at,updated_at,refreshed_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                     (inv["id"], self.ws, name, inv.get("status", "DRAFT"), total, total, self.currency, due_date,
                      json.dumps({"items": clean, "items_key": json.dumps(clean, sort_keys=True), "email": email}, ensure_ascii=False), now, now, 0))
        self.save_contact(name, email, "customer")
        self.log("invoice_draft", f"Draft invoice for {name}: {i18n.fmt_money(total, self.currency)}", invoice_id=inv["id"])
        return self.refresh_invoice(inv["id"])

    def _owned(self, invoice_id: str) -> dict:
        row = self.store.one("SELECT * FROM invoices WHERE id=? AND ws=?", (str(invoice_id), self.ws))
        if row is None:
            raise PayPalError("That invoice does not belong to this business. Use list_invoices to find the right id.")
        return row

    def refresh_invoice(self, invoice_id: str) -> dict:
        """Read the invoice from PayPal, store its state, and react to a new payment."""
        row = self._owned(invoice_id)
        inv = self.paypal.get_invoice(invoice_id)
        s = summarize(inv)
        total = float((inv.get("amount") or {}).get("value") or row["total"])
        due_raw = (inv.get("due_amount") or {}).get("value")
        due = float(due_raw) if due_raw not in (None, "") else (0.0 if s["status"] in PAID_STATUSES else row["due"])
        meta = json.loads(row["summary"])
        meta.update({"payer_link": s["payer_link"], "number": s["number"]})
        self.store.x("UPDATE invoices SET status=?, total=?, due=?, summary=?, updated_at=?, refreshed_at=? WHERE id=?",
                     (s["status"], total, due, json.dumps(meta, ensure_ascii=False), time.time(), time.time(), invoice_id))
        if s["status"] == "PAID" and row["status"] != "PAID":
            paid_now = row["due"] if row["due"] > 0 else total
            self._movement("income", f"PayPal invoice {s['number'] or invoice_id}", paid_now, "PAYPAL", "sales", row["contact"], f"inv:{invoice_id}:online")
            self.store.alert(self.ws, "a_invoice_paid", name=row["contact"], total=i18n.fmt_money(total, self.currency))
            self.log("invoice_paid", f"{row['contact']} paid {i18n.fmt_money(total, self.currency)} online", invoice_id=invoice_id)
        return self.invoice_view(invoice_id)

    def invoice_view(self, invoice_id: str) -> dict:
        r = self._owned(invoice_id)
        meta = json.loads(r["summary"])
        overdue = r["status"] in UNPAID_STATUSES and bool(r["due_date"]) and r["due_date"] < self.today().isoformat()
        return {"id": r["id"], "number": meta.get("number"), "customer": r["contact"], "email": meta.get("email"),
                "status": r["status"], "total": r["total"], "due": r["due"], "currency": r["currency"],
                "due_date": r["due_date"], "overdue": overdue, "items": meta.get("items", []),
                "payer_link": meta.get("payer_link") if r["status"] in UNPAID_STATUSES else None,
                "created_at": r["created_at"]}

    def list_invoices(self, status: str = "all", customer: str = "") -> list[dict]:
        rows = self.store.q("SELECT id FROM invoices WHERE ws=? ORDER BY created_at DESC LIMIT 100", (self.ws,))
        out = [self.invoice_view(r["id"]) for r in rows]
        if customer.strip():
            names = {c["name"].lower() for c in self.find_contacts(customer)}
            out = [i for i in out if i["customer"].lower() in names or customer.lower() in i["customer"].lower()]
        if status == "unpaid":
            out = [i for i in out if i["status"] in UNPAID_STATUSES]
        elif status == "overdue":
            out = [i for i in out if i["overdue"]]
        elif status == "paid":
            out = [i for i in out if i["status"] in PAID_STATUSES]
        elif status == "draft":
            out = [i for i in out if i["status"] == "DRAFT"]
        return out

    def send_invoice(self, invoice_id: str) -> dict:
        self._owned(invoice_id)
        self.paypal.send_invoice(invoice_id)
        view = self.refresh_invoice(invoice_id)
        self.log("invoice_sent", f"Invoice sent to {view['customer']} ({view['email']})", invoice_id=invoice_id)
        return view

    def cancel_invoice(self, invoice_id: str, note: str = "") -> dict:
        self._owned(invoice_id)
        self.paypal.cancel_invoice(invoice_id, _clean(note, 500))
        view = self.refresh_invoice(invoice_id)
        self.log("invoice_cancelled", f"Invoice for {view['customer']} cancelled", invoice_id=invoice_id)
        return view

    def remind_invoice(self, invoice_id: str, note: str = "") -> dict:
        view = self.invoice_view(invoice_id)
        self.paypal.remind_invoice(invoice_id, _clean(note, 500))
        self.log("invoice_reminded", f"Payment reminder sent to {view['customer']}", invoice_id=invoice_id)
        return {"ok": True, "invoice_id": invoice_id, "customer": view["customer"]}

    def record_payment(self, invoice_id: str, amount: float, method: str = "CASH", note: str = "") -> dict:
        row = self._owned(invoice_id)
        amount = round(float(amount), 2)
        if not 0 < amount <= 10000:
            raise ValidationError("The payment amount must be greater than 0.")
        method = method if method in METHODS else "OTHER"
        paypal_method = method if method in PAYMENT_METHODS else "OTHER"
        label = {"YAPE": "Yape", "PLIN": "Plin", "CARD": "card"}.get(method, "")
        self.paypal.record_payment(invoice_id, amount, paypal_method, _clean(" ".join(filter(None, [label, note])), 200))
        n = self.store.one("SELECT COUNT(*) n FROM movements WHERE ws=? AND ref LIKE ?", (self.ws, f"inv:{invoice_id}:pay:%"))["n"]
        self._movement("income", f"Payment on invoice for {row['contact']}", amount, method, "sales", row["contact"], f"inv:{invoice_id}:pay:{n + 1}")
        view = self.refresh_invoice(invoice_id)
        self.log("payment_recorded", f"{row['contact']} paid {i18n.fmt_money(amount, self.currency)} ({method})", invoice_id=invoice_id)
        return view

    def qr_code(self, invoice_id: str) -> str:
        self._owned(invoice_id)
        return self.paypal.qr_code(invoice_id)

    def demo_mark_paid(self, invoice_id: str) -> None:
        self._owned(invoice_id)
        self.paypal.mark_paid(invoice_id)
        self.refresh_invoice(invoice_id)

    # --- bills (payables) -------------------------------------------------------------------
    def add_bill(self, payee: str, description: str, amount: float, due_date: str, recurrence: str = "none", payee_email: str = "") -> dict:
        payee = _clean(payee, 140)
        if not payee:
            raise ValidationError("Who is the payment for?")
        amount = round(float(amount), 2)
        if not 0 < amount <= 100000:
            raise ValidationError("The amount must be greater than 0.")
        d = self._parse_date(due_date)
        recurrence = recurrence if recurrence in ("none", "weekly", "monthly") else "none"
        email = normalize_email(payee_email) if payee_email else ""
        if email and not EMAIL_RE.match(email):
            raise ValidationError(f"'{email}' is not a valid email address.")
        if not email:
            known = [c for c in self.find_contacts(payee) if c["email"] and c["name"].lower() == payee.lower()]
            email = known[0]["email"] if known else ""
        if self.store.one("SELECT COUNT(*) n FROM bills WHERE ws=? AND status='pending'", (self.ws,))["n"] >= MAX_BILLS:
            raise ValidationError("Too many pending bills.")
        cur = self.store.x("INSERT INTO bills(ws,payee,payee_email,description,amount,currency,due_date,recurrence,status,created_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (self.ws, payee, email, _clean(description, 200), amount, self.currency, d.isoformat(), recurrence, "pending", time.time()))
        if email:
            self.save_contact(payee, email, "supplier")
        self.log("bill_added", f"Bill to pay: {payee} {i18n.fmt_money(amount, self.currency)} on {d.isoformat()}", bill_id=cur.lastrowid)
        return self.bill_view(cur.lastrowid)

    def _bill(self, bill_id) -> dict:
        try:
            bid = int(bill_id)
        except (TypeError, ValueError):
            raise ValidationError("Unknown bill id.")
        row = self.store.one("SELECT * FROM bills WHERE id=? AND ws=?", (bid, self.ws))
        if row is None:
            raise ValidationError("That bill does not exist in this business. Use list_bills to find it.")
        return row

    def bill_view(self, bill_id) -> dict:
        b = self._bill(bill_id)
        overdue = b["status"] == "pending" and b["due_date"] < self.today().isoformat()
        return {"id": b["id"], "payee": b["payee"], "payee_email": b["payee_email"], "description": b["description"],
                "amount": b["amount"], "currency": b["currency"], "due_date": b["due_date"], "recurrence": b["recurrence"],
                "status": b["status"], "method": b["method"], "overdue": overdue}

    def list_bills(self, status: str = "pending", within_days: int = 0) -> list[dict]:
        rows = self.store.q("SELECT id FROM bills WHERE ws=? ORDER BY due_date, id LIMIT 200", (self.ws,))
        out = [self.bill_view(r["id"]) for r in rows]
        if status in ("pending", "paid"):
            out = [b for b in out if b["status"] == status]
        if within_days and int(within_days) > 0:
            limit = (self.today() + timedelta(days=int(within_days))).isoformat()
            out = [b for b in out if b["due_date"] <= limit]
        return out

    def _settle_bill(self, b: dict, method: str, note: str, batch_id: str = "") -> dict:
        self.store.x("UPDATE bills SET status='paid', method=?, paid_at=?, payout_batch_id=? WHERE id=?", (method, time.time(), batch_id, b["id"]))
        self._movement("expense", _clean(f"{b['payee']}: {b['description'] or note}", 200), b["amount"], method, "bills", b["payee"], f"bill:{b['id']}:paid")
        if b["recurrence"] != "none":
            d = date.fromisoformat(b["due_date"])
            nxt = d + (timedelta(days=7) if b["recurrence"] == "weekly" else timedelta(days=31))
            if b["recurrence"] == "monthly":
                nxt = date(d.year + (d.month // 12), d.month % 12 + 1, min(d.day, 28))
            self.store.x("INSERT INTO bills(ws,payee,payee_email,description,amount,currency,due_date,recurrence,status,created_at) "
                         "VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (self.ws, b["payee"], b["payee_email"], b["description"], b["amount"], b["currency"], nxt.isoformat(), b["recurrence"], "pending", time.time()))
        return self.bill_view(b["id"])

    def mark_bill_paid(self, bill_id, method: str = "CASH", note: str = "") -> dict:
        b = self._bill(bill_id)
        if b["status"] != "pending":
            raise ValidationError("That bill is already paid.")
        view = self._settle_bill(b, method if method in METHODS else "OTHER", note)
        self.log("bill_paid", f"Paid {b['payee']} {i18n.fmt_money(b['amount'], b['currency'])} ({method})", bill_id=b["id"])
        return view

    def pay_bill_with_paypal(self, bill_id) -> dict:
        b = self._bill(bill_id)
        if b["status"] != "pending":
            raise ValidationError("That bill is already paid.")
        if not EMAIL_RE.match(b["payee_email"] or ""):
            raise ValidationError(f"I need {b['payee']}'s PayPal email to pay with PayPal.")
        sender_batch_id = f"dp{self.ws[:10]}b{b['id']}"
        try:  # the primary key makes a second, concurrent attempt fail here, before any money moves
            self.store.x("INSERT INTO payouts(sender_batch_id,ws,bill_id,status,created_at) VALUES(?,?,?,?,?)",
                         (sender_batch_id, self.ws, b["id"], "starting", time.time()))
        except Exception:
            raise ValidationError("This bill already has a PayPal payment in progress or completed.")
        try:
            out = self.paypal.create_payout(sender_batch_id, b["payee_email"], b["amount"],
                                            note=_clean(f"{b['description'] or 'Payment'} - {self.info['name']}", 200),
                                            subject=f"Payment from {self.info['name']}")
        except PayPalError as e:
            if "SENDER_BATCH_ID" in str(e).upper() or "ALREADY" in str(e).upper():
                self.store.x("UPDATE payouts SET status='duplicate' WHERE sender_batch_id=?", (sender_batch_id,))
                raise ValidationError("PayPal says this bill was already paid earlier, so I did not pay it again.")
            self.store.x("DELETE FROM payouts WHERE sender_batch_id=?", (sender_batch_id,))  # nothing was paid: allow a retry
            raise
        header = out.get("batch_header", {}) if isinstance(out, dict) else {}
        batch_id, batch_status = header.get("payout_batch_id", ""), header.get("batch_status", "PENDING")
        self.store.x("UPDATE payouts SET status=?, batch_id=? WHERE sender_batch_id=?", (batch_status, batch_id, sender_batch_id))
        view = self._settle_bill(b, "PAYPAL", "PayPal payout", batch_id)
        self.log("bill_paid", f"Paid {b['payee']} {i18n.fmt_money(b['amount'], b['currency'])} with PayPal (batch {batch_id})", bill_id=b["id"])
        return {**view, "payout_batch_id": batch_id, "payout_status": batch_status}

    # --- money in and out ---------------------------------------------------------------
    def _movement(self, kind, description, amount, method, category, contact, ref="", currency=None, at=None) -> None:
        try:
            self.store.x("INSERT INTO movements(ws,kind,description,amount,currency,method,category,contact,ref,at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (self.ws, kind, _clean(description, 200), round(float(amount), 2), currency or self.currency, method, _clean(category, 40).lower(),
                          _clean(contact, 140), ref, at or time.time()))
        except Exception as e:  # same ref twice = already recorded: never count money twice
            if "UNIQUE" not in str(e).upper():
                raise

    def record_movement(self, kind: str, description: str, amount: float, method: str = "CASH", category: str = "", contact: str = "") -> dict:
        if kind not in ("income", "expense"):
            raise ValidationError("kind must be income or expense.")
        amount = round(float(amount), 2)
        if not 0 < amount <= 100000:
            raise ValidationError("The amount must be greater than 0.")
        if not _clean(description, 200):
            raise ValidationError("Describe the sale or expense.")
        self._movement(kind, description, amount, method if method in METHODS else "OTHER", category or ("sales" if kind == "income" else "general"), contact)
        self.log("movement", f"{'Income' if kind == 'income' else 'Expense'}: {_clean(description, 80)} {i18n.fmt_money(amount, self.currency)}")
        return {"ok": True, "kind": kind, "amount": amount, "currency": self.currency, "description": _clean(description, 200)}

    # --- reminders ------------------------------------------------------------------------
    def create_reminder(self, text: str, due_at: str, kind: str = "owner_note", invoice_id: str = "") -> dict:
        text = _clean(text, 300)
        if not text:
            raise ValidationError("What should I remind you about?")
        dt = self._parse_datetime(due_at)
        now = self.now()
        if dt < now - timedelta(minutes=1):
            raise ValidationError(f"That time has already passed (it is now {now.strftime('%Y-%m-%d %H:%M')}).")
        if dt > now + timedelta(days=366):
            raise ValidationError("Reminders can be at most one year ahead.")
        kind = kind if kind in ("owner_note", "remind_customer") else "owner_note"
        if kind == "remind_customer":
            view = self.invoice_view(invoice_id)
            if view["status"] not in UNPAID_STATUSES:
                raise ValidationError("Only sent, unpaid invoices can get a customer reminder.")
        else:
            invoice_id = ""
        if self.store.one("SELECT COUNT(*) n FROM reminders WHERE ws=? AND status='pending'", (self.ws,))["n"] >= 100:
            raise ValidationError("Too many pending reminders.")
        cur = self.store.x("INSERT INTO reminders(ws,text,due_at,kind,invoice_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                           (self.ws, text, dt.timestamp(), kind, invoice_id, "pending", time.time()))
        self.log("reminder_added", f"Reminder for {dt.strftime('%Y-%m-%d %H:%M')}: {text}", reminder_id=cur.lastrowid)
        return self.reminder_view(cur.lastrowid)

    def reminder_view(self, rid) -> dict:
        r = self.store.one("SELECT * FROM reminders WHERE id=? AND ws=?", (int(rid), self.ws))
        if r is None:
            raise ValidationError("Unknown reminder.")
        local = datetime.fromtimestamp(r["due_at"], self.tz)
        return {"id": r["id"], "text": r["text"], "due_at": local.strftime("%Y-%m-%dT%H:%M"), "kind": r["kind"],
                "invoice_id": r["invoice_id"], "status": r["status"], "result": r["result"]}

    def list_reminders(self, include_done: bool = False) -> list[dict]:
        sql = "SELECT id FROM reminders WHERE ws=?" + ("" if include_done else " AND status='pending'") + " ORDER BY due_at LIMIT 100"
        return [self.reminder_view(r["id"]) for r in self.store.q(sql, (self.ws,))]

    def cancel_reminder(self, rid) -> dict:
        view = self.reminder_view(rid)
        if view["status"] != "pending":
            raise ValidationError("That reminder is not pending.")
        self.store.x("UPDATE reminders SET status='cancelled' WHERE id=?", (view["id"],))
        self.log("reminder_cancelled", f"Reminder cancelled: {view['text']}")
        return {**view, "status": "cancelled"}

    def fire_due_reminders(self) -> int:
        fired = 0
        for r in self.store.q("SELECT * FROM reminders WHERE ws=? AND status='pending' AND due_at<=?", (self.ws, time.time())):
            if self.store.x("UPDATE reminders SET status='firing' WHERE id=? AND status='pending'", (r["id"],)).rowcount != 1:
                continue  # another worker took it
            result = "ok"
            if r["kind"] == "remind_customer":
                try:
                    view = self.invoice_view(r["invoice_id"])
                    if view["status"] in UNPAID_STATUSES:
                        self.paypal.remind_invoice(r["invoice_id"], r["text"])
                        self.store.alert(self.ws, "a_customer_reminded", name=view["customer"], total=i18n.fmt_money(view["total"], view["currency"]))
                        self.log("invoice_reminded", f"Scheduled reminder sent to {view['customer']}", invoice_id=r["invoice_id"])
                    else:
                        result = "skipped: invoice no longer unpaid"
                except Exception as e:
                    result = f"failed: {e}"[:200]
                    self.store.alert(self.ws, "a_reminder_failed", name=r["invoice_id"], error=str(e)[:120])
            else:
                self.store.alert(self.ws, "a_reminder", text=r["text"])
            self.store.x("UPDATE reminders SET status='done', fired_at=?, result=? WHERE id=?", (time.time(), result, r["id"]))
            fired += 1
        return fired

    # --- remembered facts -----------------------------------------------------------------
    def remember(self, fact: str) -> dict:
        fact = _clean(fact, 300)
        if not fact:
            raise ValidationError("Nothing to remember.")
        if self.store.one("SELECT COUNT(*) n FROM facts WHERE ws=?", (self.ws,))["n"] >= MAX_FACTS:
            oldest = self.store.one("SELECT id FROM facts WHERE ws=? ORDER BY id LIMIT 1", (self.ws,))
            self.store.x("DELETE FROM facts WHERE id=?", (oldest["id"],))
        self.store.x("INSERT INTO facts(ws,fact,created_at) VALUES(?,?,?)", (self.ws, fact, time.time()))
        self.log("fact", f"Remembered: {fact}")
        return {"ok": True, "remembered": fact}

    def facts(self, limit: int = 20) -> list[str]:
        return [r["fact"] for r in self.store.q("SELECT fact FROM facts WHERE ws=? ORDER BY id DESC LIMIT ?", (self.ws, limit))]

    # --- financial overview ---------------------------------------------------------------
    def _period_start(self, period: str) -> datetime:
        now = self.now()
        if period == "today":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "week":
            start = now - timedelta(days=now.weekday())
            return start.replace(hour=0, minute=0, second=0, microsecond=0)
        return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    def overview(self, period: str = "month") -> dict:
        period = period if period in ("today", "week", "month") else "month"
        start = self._period_start(period).timestamp()
        today = self.today()
        invs = self.list_invoices("all")
        open_inv = [i for i in invs if i["status"] in UNPAID_STATUSES]
        overdue = [i for i in open_inv if i["overdue"]]
        bills = self.list_bills("pending")
        week = (today + timedelta(days=7)).isoformat()
        month = (today + timedelta(days=30)).isoformat()
        moves = self.store.q("SELECT kind, amount, category, contact FROM movements WHERE ws=? AND at>=?", (self.ws, start))
        income = round(sum(m["amount"] for m in moves if m["kind"] == "income"), 2)
        expense = round(sum(m["amount"] for m in moves if m["kind"] == "expense"), 2)
        by_cat: dict[str, float] = {}
        by_customer: dict[str, float] = {}
        for m in moves:
            if m["kind"] == "expense":
                by_cat[m["category"] or "general"] = by_cat.get(m["category"] or "general", 0) + m["amount"]
            elif m["contact"]:
                by_customer[m["contact"]] = by_customer.get(m["contact"], 0) + m["amount"]
        receivable = round(sum(i["due"] for i in open_inv), 2)
        due_7 = round(sum(b["amount"] for b in bills if b["due_date"] <= week), 2)
        due_30 = round(sum(b["amount"] for b in bills if b["due_date"] <= month), 2)
        return {
            "period": period, "currency": self.currency, "today": today.isoformat(),
            "income": income, "expense": expense, "net": round(income - expense, 2),
            "receivable": receivable, "open_invoices": len(open_inv),
            "overdue_invoices": [{"id": i["id"], "customer": i["customer"], "due": i["due"], "due_date": i["due_date"]} for i in overdue],
            "bills_overdue": [{"id": b["id"], "payee": b["payee"], "amount": b["amount"], "due_date": b["due_date"]} for b in bills if b["overdue"]],
            "bills_next_7_days": due_7, "bills_next_30_days": due_30,
            "next_bills": [{"id": b["id"], "payee": b["payee"], "amount": b["amount"], "due_date": b["due_date"]} for b in bills[:5]],
            "top_expense_categories": sorted(({"category": k, "amount": round(v, 2)} for k, v in by_cat.items()), key=lambda x: -x["amount"])[:3],
            "top_customers": sorted(({"customer": k, "amount": round(v, 2)} for k, v in by_customer.items()), key=lambda x: -x["amount"])[:3],
            "projected_30_days": round(receivable - due_30, 2),
            "pending_reminders": len(self.list_reminders()),
        }

    def cashflow(self, days: int = 30) -> list[dict]:
        """Expected money in (open invoices by due date) and out (bills by due date), day by day."""
        today = self.today()
        series = {(today + timedelta(days=i)).isoformat(): {"in": 0.0, "out": 0.0} for i in range(days)}
        first = today.isoformat()
        for i in self.list_invoices("unpaid"):
            key = max(i["due_date"] or first, first)
            if key in series:
                series[key]["in"] += i["due"]
        for b in self.list_bills("pending"):
            key = max(b["due_date"], first)
            if key in series:
                series[key]["out"] += b["amount"]
        return [{"date": k, "in": round(v["in"], 2), "out": round(v["out"], 2)} for k, v in series.items()]

    def briefing(self, lang: str) -> str:
        o = self.overview("month")
        hour = self.now().hour
        parts = [i18n.t("b_morning" if hour < 12 else "b_afternoon" if hour < 19 else "b_evening", lang) + "."]
        cur = self.currency
        if o["open_invoices"]:
            parts.append(i18n.t("b_receivable", lang, amount=i18n.fmt_money(o["receivable"], cur), n=o["open_invoices"]))
            if o["overdue_invoices"]:
                parts.append(i18n.t("b_overdue", lang, n=len(o["overdue_invoices"])))
        if o["bills_overdue"]:
            b0 = o["bills_overdue"][0]
            parts.append(i18n.t("b_bills_overdue", lang, n=len(o["bills_overdue"]), payee=b0["payee"], amount=i18n.fmt_money(b0["amount"], cur)))
        today_s, week_s = self.today().isoformat(), (self.today() + timedelta(days=7)).isoformat()
        upcoming = [b for b in self.list_bills("pending") if today_s <= b["due_date"] <= week_s]
        if upcoming:
            parts.append(i18n.t("b_bills", lang, amount=i18n.fmt_money(sum(b["amount"] for b in upcoming), cur), payee=upcoming[0]["payee"],
                                date=i18n.fmt_date(date.fromisoformat(upcoming[0]["due_date"]), lang)))
        end_of_day = self.now().replace(hour=23, minute=59).timestamp()
        today_rem = self.store.one("SELECT COUNT(*) n FROM reminders WHERE ws=? AND status='pending' AND due_at<=?", (self.ws, end_of_day))["n"]
        if today_rem:
            parts.append(i18n.t("b_reminders", lang, n=today_rem))
        if o["income"] or o["expense"]:
            parts.append(i18n.t("b_month", lang, income=i18n.fmt_money(o["income"], cur), expense=i18n.fmt_money(o["expense"], cur)))
        if len(parts) == 1:
            parts.append(i18n.t("b_clear", lang))
        return " ".join(parts)

    def memory_snapshot(self) -> str:
        """Compact business memory given to the agent at the start of each conversation."""
        o = self.overview("month")
        contacts = self.store.q("SELECT name, email, kind FROM contacts WHERE ws=? ORDER BY id DESC LIMIT 15", (self.ws,))
        lines = [f"Business: {self.info['name']}. Currency: {self.currency}. Time zone: {self.tz.key}.",
                 "Contacts: " + ("; ".join(f"{c['name']} <{c['email'] or 'no email'}> ({c['kind']})" for c in contacts) or "none yet"),
                 f"Open invoices: {o['open_invoices']} totalling {o['receivable']} {self.currency}; overdue: {len(o['overdue_invoices'])}.",
                 "Upcoming bills: " + ("; ".join(f"#{b['id']} {b['payee']} {b['amount']} on {b['due_date']}" for b in o["next_bills"]) or "none"),
                 f"This month: income {o['income']}, expenses {o['expense']}.",
                 "Remembered facts: " + (" | ".join(self.facts(10)) or "none")]
        return "\n".join(lines)

    # --- confirmations: executed by code when the owner says yes --------------------------
    def request_confirmation(self, kind: str, target: str, conversation: str = "") -> dict:
        if kind in ("send_invoice", "cancel_invoice"):
            view = self.invoice_view(target)
            if kind == "send_invoice" and view["status"] != "DRAFT":
                raise ValidationError(f"That invoice is {view['status']}, not a draft, so it cannot be sent again.")
            if kind == "cancel_invoice" and view["status"] not in UNPAID_STATUSES:
                raise ValidationError(f"Only sent, unpaid invoices can be cancelled (this one is {view['status']}).")
            details = {"customer": view["customer"], "email": view["email"], "total": view["total"], "currency": view["currency"], "items": view["items"]}
        elif kind == "pay_bill":
            b = self.bill_view(target)
            if b["status"] != "pending":
                raise ValidationError("That bill is already paid.")
            if not EMAIL_RE.match(b["payee_email"] or ""):
                raise ValidationError(f"I need {b['payee']}'s PayPal email before paying. Ask the owner, then use add_bill or save_contact.")
            details = {"payee": b["payee"], "email": b["payee_email"], "amount": b["amount"], "currency": b["currency"], "description": b["description"]}
            target = str(b["id"])
        else:
            raise ValidationError("Unknown action.")
        self.expire_confirmations()
        existing = self.store.one("SELECT id FROM confirmations WHERE ws=? AND kind=? AND target=? AND status='pending'", (self.ws, kind, str(target)))
        cid = existing["id"] if existing else uuid.uuid4().hex[:12]
        if not existing:
            self.store.x("INSERT INTO confirmations(id,ws,conversation,kind,target,details,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                         (cid, self.ws, conversation, kind, str(target), json.dumps(details, ensure_ascii=False), "pending", time.time()))
        return {"status": "awaiting_owner_confirmation", "confirmation_id": cid, "action": kind, "details": details,
                "instructions": "Read these details back to the owner and ask them to confirm. The action runs only when the owner "
                                "presses Confirm or answers yes; you cannot execute it yourself."}

    def expire_confirmations(self) -> None:
        self.store.x("UPDATE confirmations SET status='expired', resolved_at=? WHERE ws=? AND status='pending' AND created_at<?",
                     (time.time(), self.ws, time.time() - CONFIRMATION_TTL))

    def pending_confirmations(self) -> list[dict]:
        self.expire_confirmations()
        rows = self.store.q("SELECT * FROM confirmations WHERE ws=? AND status='pending' ORDER BY created_at", (self.ws,))
        return [{"id": r["id"], "kind": r["kind"], "target": r["target"], "details": json.loads(r["details"]), "created_at": r["created_at"]} for r in rows]

    def resolve_confirmation(self, cid: str, approve: bool, lang: str) -> tuple[bool, str, dict]:
        """Run (or drop) a pending action. Returns (done, spoken reply, result)."""
        self.expire_confirmations()
        row = self.store.one("SELECT * FROM confirmations WHERE id=? AND ws=?", (str(cid), self.ws))
        if row is None:
            return False, i18n.t("c_gone", lang), {}
        new_status = "executing" if approve else "denied"
        if self.store.x("UPDATE confirmations SET status=?, resolved_at=? WHERE id=? AND status='pending'", (new_status, time.time(), cid)).rowcount != 1:
            return False, i18n.t("c_gone", lang), {}  # already handled (double click, button + voice): never run twice
        details = json.loads(row["details"])
        if not approve:
            self.log("confirmation_denied", f"Owner declined: {row['kind']}", confirmation_id=cid)
            return False, i18n.t("c_denied", lang), {}
        try:
            if row["kind"] == "send_invoice":
                result = self.send_invoice(row["target"])
                reply = i18n.t("c_send_invoice", lang, name=details["customer"], total=i18n.fmt_money(details["total"], details["currency"]))
            elif row["kind"] == "cancel_invoice":
                result = self.cancel_invoice(row["target"])
                reply = i18n.t("c_cancel_invoice", lang, name=details["customer"], total=i18n.fmt_money(details["total"], details["currency"]))
            else:
                result = self.pay_bill_with_paypal(row["target"])
                reply = i18n.t("c_pay_bill", lang, payee=details["payee"], amount=i18n.fmt_money(details["amount"], details["currency"]))
        except (PayPalError, ValidationError) as e:
            self.store.x("UPDATE confirmations SET status='failed', result=? WHERE id=?", (str(e)[:300], cid))
            self.log("confirmation_failed", f"{row['kind']} failed: {e}", confirmation_id=cid)
            return False, i18n.t("c_failed", lang, error=str(e)[:200]), {}
        self.store.x("UPDATE confirmations SET status='done', result=? WHERE id=?", (json.dumps(result, ensure_ascii=False, default=str)[:2000], cid))
        return True, reply, result
