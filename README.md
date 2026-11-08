# Destiny Pay 🎙️💸

**A Spanish-speaking voice cashier for Latin American small businesses, built on PayPal + Claude.**

Millions of small sellers in Latin America run their business on WhatsApp and a notebook. Chasing payments is
manual: they type amounts, forget who owes them, and never send reminders. Destiny Pay lets the owner just *talk*:

> "Cóbrale a Juan Pérez 3 cajas de té a 12 dólares, su correo es juan@correo.com"

The AI agent understands the request, confirms it out loud, creates and sends a real **PayPal invoice**, and
keeps track of who has paid.

## What it does
- 🗣️ **Voice in, voice out (Spanish):** browser speech recognition + speech synthesis, no app to install.
- 🧾 **Create & send invoices** with PayPal Invoicing v2 (sandbox) — the customer gets a PayPal payment link by email.
- 📋 **"¿Quién me debe?"** — lists pending invoices and summarizes them in one sentence.
- 🔔 **Reminders** and ❌ **cancellations** by voice.
- ✅ **"¿Ya pagó Juan?"** — checks the live invoice status.
- 🛡️ **Safe agent design:** the agent must confirm customer, total and email before sending; it never invents emails;
  tool errors are reported in plain Spanish.

## How it works
```
Browser (Web Speech API, es-PE)  ->  FastAPI  ->  Claude (claude-opus-5-5, tool use)  ->  PayPal Invoicing v2 (sandbox)
```
- `app/agent.py` — agent loop with strict tool schemas (`create_and_send_invoice`, `list_invoices`, `get_invoice`,
  `remind_invoice`, `cancel_invoice`), one append-only conversation per session.
- `app/paypal.py` — minimal PayPal REST client (OAuth client credentials, token caching, Invoicing v2).
- `app/fake.py` — in-memory PayPal used by the tests and by demo mode when no credentials are set.
- `static/index.html` — single-page voice UI with live invoice table.

## Run locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # add ANTHROPIC_API_KEY and your PayPal sandbox app credentials
uvicorn app.main:app --reload
# open http://localhost:8000 (Chrome recommended for voice)
pytest -q
```
Without PayPal credentials the app runs in **demo mode** (in-memory invoices) so you can try the agent anyway.

## Testing the payment flow (sandbox)
1. Create a sandbox app at developer.paypal.com and put its client ID/secret in `.env`.
2. Ask Destiny Pay to bill one of your **sandbox personal** accounts' emails.
3. Open the invoice "pagar" link, log in with the sandbox personal account and pay.
4. Ask "¿Ya pagó …?" — the status changes to `PAID`.

## Notes
- PayPal does not support Peruvian soles (PEN), so invoices are issued in USD.
- Built by **Innov4AI SAC** (Lima, Peru) for the PayPal AI Hackathon 2026.

## License
MIT
