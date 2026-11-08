"""Check the real Claude agent in the three languages, with in-memory PayPal (no sandbox calls).

Usage:  python scripts/agent_smoke.py
Needs ANTHROPIC_API_KEY in .env. Costs a few cents. Prints each reply and the tools the agent used.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.agent import DestinyPayAgent  # noqa: E402
from app.fake import FakePayPal  # noqa: E402

SCRIPTS = {
    "es": ["Cóbrale a Juan Pérez 3 cajas de té a 12 dólares, su correo es juan arroba correo punto com",
           "Sí, envíala", "¿Quién me debe?", "Juan me pagó 10 dólares en efectivo", "¿Cuánto me debe Juan todavía?"],
    "en": ["Bill John Smith for 2 haircuts at 15 dollars each, email john dot smith at example dot com",
           "Yes, send it", "Show me the QR code", "Who owes me?"],
    "pt": ["Cobre a Maria Souza 2 bolos a 20 dólares, e-mail maria arroba exemplo ponto com",
           "Sim, pode enviar", "Quem me deve?"],
}
# The send tool must never run before the owner confirms.
GUARD = {"es": 0, "en": 0, "pt": 0}


def main() -> None:
    load_dotenv()
    agent = DestinyPayAgent(lambda sid: FakePayPal())
    problems = []
    for lang, turns in SCRIPTS.items():
        sid = f"smoke-{lang}-{int(time.time())}"
        print(f"\n===== {lang} =====")
        for i, text in enumerate(turns):
            t0 = time.time()
            res = agent.chat(sid, text, lang)
            tools = [a["tool"] + (" (error)" if "error" in a else "") for a in res.get("actions", [])]
            print(f"> {text}\n< {res['reply']}\n  tools={tools}  {time.time() - t0:.1f}s")
            if res.get("error"):
                problems.append(f"{lang} turn {i + 1}: {res['error']}")
            if i == GUARD[lang] and "send_invoice" in tools:
                problems.append(f"{lang}: sent the invoice before the owner confirmed")
            if i == 0 and "draft_invoice" not in tools:
                problems.append(f"{lang}: no draft on the first request (did it ask something instead? read the reply)")
            if i == 1 and "send_invoice" not in tools:
                problems.append(f"{lang}: did not send after the owner confirmed")
        print("  invoices:", [(inv["status"], inv["amount"]["value"]) for inv in agent.sessions.peek(sid).backend.list_invoices()])
    print("\nALL GOOD" if not problems else "\nPROBLEMS:\n- " + "\n- ".join(problems))
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
