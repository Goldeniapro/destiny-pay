"""Sample business for the public demo, so judges see memory and insights from the first second."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from .business import Business

NAMES = {"es": "Inti Artesanías", "en": "Inti Crafts", "pt": "Inti Artesanato"}

TEXT = {
    "es": {"sarah": "Turista de Chicago; prefiere inglés.", "lucho": "Cliente local; paga con Yape.", "textiles": "Proveedor de hilo de alpaca.",
           "marta": "Dueña del local.", "yarn": "Pedido de hilo de alpaca", "rent": "Alquiler del local", "power": "Internet y luz",
           "bags": "Bolsas y empaques", "call": "Llamar a Textiles Andinos para el nuevo pedido",
           "facts": ["Sarah Miller vuelve a Chicago el 20 de noviembre.", "Los turistas suelen pagar con PayPal; los clientes locales con Yape.",
                     "El alquiler se paga a Marta Quispe los días {day} de cada mes."],
           "sales": ["Chompa de alpaca", "Chalina tejida", "Retablo ayacuchano", "Aretes de plata", "Gorro chullo", "Manta andina"],
           "exp": [("Hilo y tintes", "supplies"), ("Taxi al mercado", "transport"), ("Alquiler de octubre", "rent"), ("Publicidad en redes", "marketing")]},
    "en": {"sarah": "Tourist from Chicago; prefers English.", "lucho": "Local customer; pays with Yape.", "textiles": "Alpaca yarn supplier.",
           "marta": "Shop landlord.", "yarn": "Alpaca yarn order", "rent": "Shop rent", "power": "Internet and power",
           "bags": "Bags and packaging", "call": "Call Textiles Andinos about the new order",
           "facts": ["Sarah Miller flies back to Chicago on November 20.", "Tourists usually pay with PayPal; local customers with Yape.",
                     "Rent is paid to Marta Quispe on day {day} of each month."],
           "sales": ["Alpaca sweater", "Woven scarf", "Ayacucho altarpiece", "Silver earrings", "Chullo hat", "Andean blanket"],
           "exp": [("Yarn and dyes", "supplies"), ("Taxi to the market", "transport"), ("October rent", "rent"), ("Social media ads", "marketing")]},
}
TEXT["pt"] = {"sarah": "Turista de Chicago; prefere inglês.", "lucho": "Cliente local; paga com Yape.", "textiles": "Fornecedor de fio de alpaca.",
              "marta": "Dona da loja.", "yarn": "Pedido de fio de alpaca", "rent": "Aluguel da loja", "power": "Internet e luz",
              "bags": "Sacolas e embalagens", "call": "Ligar para a Textiles Andinos sobre o novo pedido",
              "facts": ["Sarah Miller volta para Chicago em 20 de novembro.", "Turistas costumam pagar com PayPal; clientes locais com Yape.",
                        "O aluguel é pago a Marta Quispe todo dia {day}."],
              "sales": ["Suéter de alpaca", "Cachecol tecido", "Retábulo ayacuchano", "Brincos de prata", "Gorro chullo", "Manta andina"],
              "exp": [("Fio e tinturas", "supplies"), ("Táxi até o mercado", "transport"), ("Aluguel de outubro", "rent"), ("Anúncios nas redes", "marketing")]}
INVOICE_ITEMS = {"es": ("Manta andina", "Retablo ayacuchano"), "en": ("Andean blanket", "Ayacucho altarpiece"), "pt": ("Manta andina", "Retábulo ayacuchano")}


def seed_demo(biz: Business, lang: str) -> None:
    tx = TEXT.get(lang, TEXT["en"])
    today = biz.today()
    biz.save_contact("Sarah Miller", "sarah.miller@example.com", "customer", tx["sarah"])
    biz.save_contact("Lucho Ramos", "lucho.ramos@example.com", "customer", tx["lucho"])
    biz.save_contact("Textiles Andinos SAC", "pagos@textilesandinos.example.com", "supplier", tx["textiles"])
    biz.save_contact("Marta Quispe", "marta.quispe@example.com", "supplier", tx["marta"])
    biz.add_bill("Textiles Andinos SAC", tx["yarn"], 180, (today + timedelta(days=2)).isoformat(), "none", "pagos@textilesandinos.example.com")
    biz.add_bill("Marta Quispe", tx["rent"], 250, (today + timedelta(days=5)).isoformat(), "monthly", "marta.quispe@example.com")
    biz.add_bill("Movistar", tx["power"], 45, (today + timedelta(days=9)).isoformat(), "monthly", "")
    biz.add_bill("Empaques Lima", tx["bags"], 25, (today - timedelta(days=1)).isoformat(), "none", "")
    now = time.time()
    sales = [(1, 0, 95, "PAYPAL", "Sarah Miller"), (2, 1, 35, "YAPE", "Lucho Ramos"), (3, 2, 120, "PAYPAL", ""), (5, 3, 48, "CASH", ""),
             (6, 4, 22, "YAPE", ""), (8, 0, 190, "PAYPAL", ""), (10, 5, 140, "CARD", ""), (12, 1, 35, "CASH", ""),
             (15, 3, 96, "PAYPAL", ""), (18, 0, 95, "YAPE", "Lucho Ramos"), (21, 2, 120, "PAYPAL", ""), (25, 4, 44, "CASH", "")]
    for days_ago, item, amount, method, contact in sales:
        biz._movement("income", tx["sales"][item], amount, method, "sales", contact, at=now - days_ago * 86400 - 3600)
    for days_ago, (desc, cat), amount in [(4, tx["exp"][0], 85), (9, tx["exp"][1], 18), (26, tx["exp"][2], 300), (14, tx["exp"][3], 40), (20, tx["exp"][0], 60)]:
        biz._movement("expense", desc, amount, "CASH", cat, "", at=now - days_ago * 86400 - 7200)
    tomorrow_10 = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=10)
    biz.create_reminder(tx["call"], tomorrow_10.strftime("%Y-%m-%dT%H:%M"))
    for fact in tx["facts"]:
        biz.remember(fact.format(day=(today + timedelta(days=5)).day))  # matches the rent bill's due date
    if getattr(biz.paypal, "offline", False):  # offline demo: instant; on PayPal, seed_invoices runs in the background
        seed_invoices(biz, lang, overdue=True)
    biz.log("demo", "Sample business loaded", "demo", {})


def seed_invoices(biz: Business, lang: str, overdue: bool = False) -> None:
    """Two open invoices so the sample business shows money owed from the first second (real ones on the PayPal sandbox)."""
    blanket, retablo = INVOICE_ITEMS.get(lang, INVOICE_ITEMS["en"])
    a = biz.draft_invoice("Hostal Killa", "reservas@hostalkilla.example.com", [{"name": blanket, "quantity": 5, "unit_price": 85}], "", due_in_days=6)
    biz.send_invoice(a["id"])
    b = biz.draft_invoice("Lucho Ramos", "lucho.ramos@example.com", [{"name": retablo, "quantity": 1, "unit_price": 120}], "", due_in_days=3)
    biz.send_invoice(b["id"])
    if overdue:  # offline demo only: show what an overdue invoice looks like
        biz.store.x("UPDATE invoices SET due_date=? WHERE id=?", ((biz.today() - timedelta(days=12)).isoformat(), b["id"]))
