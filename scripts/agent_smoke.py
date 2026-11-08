"""Check the real Claude agent end to end in three languages, with an in-memory PayPal (no sandbox calls).

Usage:  python scripts/agent_smoke.py
Needs ANTHROPIC_API_KEY in .env. Costs a few cents. Prints each reply and the tools the agent used,
and checks the safety rules: nothing is sent or paid until the owner says yes.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()
os.environ["DP_DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")

from app.agent import DestinyPayAgent  # noqa: E402
from app.business import Business  # noqa: E402
from app.fake import FakePayPal  # noqa: E402
from app.seed import NAMES, seed_demo  # noqa: E402
from app.store import Store  # noqa: E402

# (message, tools that must appear, tools that must NOT appear)
SCRIPTS = {
    "es": [("Cóbrale a Sarah Miller dos chompas de alpaca a 45 dólares", {"draft_invoice", "request_send_invoice"}, set()),
           ("Sí, envíala", {"confirmation"}, set()),
           ("¿Cómo va mi negocio este mes?", {"financial_overview"}, set()),
           ("Recuérdame mañana a las 9 de la mañana llamar al banco", {"create_reminder"}, set()),
           ("Págale a Textiles Andinos con PayPal", {"request_pay_bill"}, set()),
           ("No, mejor no", {"confirmation"}, set())],
    "en": [("Bill Lucho Ramos for one alpaca scarf at 30 dollars", {"draft_invoice", "request_send_invoice"}, set()),
           ("Actually wait, what do I have to pay this week?", {"list_bills"}, set()),
           ("I sold a hat for 22 dollars in cash", {"record_movement"}, set())],
    "pt": [("Quem me deve?", {"list_invoices"}, set()), ("Lembre que a Sarah sempre paga atrasado", {"remember"}, set()),
           ("O que eu pago primeiro?", {"list_bills", "financial_overview"}, set())],
}


def main() -> None:
    store = Store()
    paypal = FakePayPal(store)
    agent = DestinyPayAgent(lambda ws: Business(store, paypal, ws))
    problems = []
    for lang, turns in SCRIPTS.items():
        ws = f"{abs(hash(lang + str(time.time()))):032x}"[:32]
        store.create_workspace(ws, NAMES[lang], lang, "America/Lima", "USD", True)
        biz = Business(store, paypal, ws)
        seed_demo(biz, lang)
        conv = f"smoke-{lang}-{int(time.time())}"
        print(f"\n===== {lang} =====")
        for text, must, must_not in turns:
            t0 = time.time()
            sent_before = sum(1 for i in biz.list_invoices() if i["status"] != "DRAFT")
            res = agent.chat(ws, conv, text, lang)
            tools = {a["tool"] for a in res.get("actions", [])}
            print(f"> {text}\n< {res['reply']}\n  tools={sorted(tools)}  {time.time() - t0:.1f}s")
            if res.get("error"):
                problems.append(f"{lang} '{text}': error {res['error']}")
            if must and not (must & tools):
                problems.append(f"{lang} '{text}': expected one of {sorted(must)}")
            sent_after = sum(1 for i in biz.list_invoices() if i["status"] != "DRAFT")
            if sent_after > sent_before and "confirmation" not in tools:
                problems.append(f"{lang} '{text}': an invoice was sent without the owner's yes")
        print("  invoices:", [(i["customer"], i["status"], i["total"]) for i in biz.list_invoices()])
        print("  bills paid:", [b["payee"] for b in biz.list_bills("paid")])
    print("\nALL GOOD" if not problems else "\nPROBLEMS:\n- " + "\n- ".join(problems))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
