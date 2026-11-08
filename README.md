# Destiny Pay 🎙️

**The voice-first money desk for small businesses.** Talk to it in Spanish, English or Portuguese: Destiny Pay bills your customers with PayPal invoices, pays your suppliers with PayPal Payouts, remembers every customer and every debt, reminds you before things are due, and tells you how your business is really doing. **Nothing moves without your confirmation, and that rule is enforced in code, not in a prompt.**

> 🔗 **Live demo:** _coming soon_ · 🎬 **Video:** _coming soon_ · Built for the PayPal AI Hackathon 2026 by Innov4AI SAC (Lima, Peru)

## Why
Micro and small businesses are about 99% of all companies in Latin America ([CEPAL](https://www.cepal.org/es/temas/micro-pequenas-medianas-empresas-mipyme/acerca-microempresas-pymes)). Many of them sell to tourists and clients abroad: tour guides in Cusco, artisan shops in Lima, guesthouses, freelancers. They run everything from a phone and a notebook: amounts typed by hand, debts nobody chases, bills paid late. Destiny Pay gives them a cashier and a bookkeeper they can simply talk to while they serve customers.

## Try it in 90 seconds
1. Open the live demo (Chrome, Edge or Safari). You get your own **sample business**, "Inti Crafts", with customers, suppliers, bills and a month of sales already in memory.
2. Listen to the **daily briefing**: what you're owed, what's overdue, what you must pay this week.
3. Tap 🎙️ and say *"Bill Sarah Miller for two alpaca sweaters at 45 dollars"*. Destiny Pay already knows Sarah's email, drafts the PayPal invoice and shows a **confirmation card**. Say **"yes"** or press Confirm: the invoice goes out with its payment link.
4. Say *"Show me the QR"*, then pay as the customer (sandbox buyer account in the Devpost testing notes). Destiny Pay **announces the payment out loud** within seconds.
5. Say *"Pay Textiles Andinos with PayPal"*: a card shows payee, email and amount; on your yes, a **PayPal Payout** pays the supplier. The bill is marked paid only when PayPal confirms the payout (a failed or returned payout puts it back to pending and tells you), and the next one is scheduled if it's recurring.
6. Ask *"How is my business doing this month?"* or *"What should I pay first?"* for grounded, conservative advice.
7. Close the tab and come back tomorrow: **it remembers everything.**

## What it does
| You say | Destiny Pay does | Under the hood |
|---|---|---|
| "Bill Ana for 2 cakes at 15" | Drafts an invoice, reuses Ana's saved email, reads it back, asks for confirmation | PayPal Invoicing v2: create, then send after your yes |
| "Show the QR" | Payment QR on screen | `generate-qr-code` |
| *(customer pays)* | Spoken alert "Ana just paid you 30 USD" | Verified PayPal webhook + background status refresh |
| "Remind Ana on Friday" | Schedules a PayPal reminder to the customer | Scheduler + `/remind` |
| "Ana paid 10 by Yape" / "I sold a hat for 22 cash" | Records the payment or the sale | `/payments` + local ledger |
| "Rent is 300 on the 5th, every month" | Adds a recurring bill | Payables ledger |
| "Pay the supplier with PayPal" | Confirmation card, then pays | PayPal Payouts, idempotent per bill |
| "Remind me tomorrow at 9 to call the bank" | Owner reminder, spoken when due | Scheduler + alerts |
| "Remember Sarah always pays late" | Long-term memory for future conversations | Facts store |
| "How am I doing?" | Income, expenses, overdue, 30-day cash projection, one or two concrete suggestions | Financial overview |

## Built so an AI can be trusted with money
- **Human confirmation by code.** The model has no tool that sends, cancels or pays. It can only *request* those actions, which creates a confirmation card. The server executes the action only when the owner presses Confirm, or answers a clear "yes" to that exact request in their very next message. A "yes" meant for another question, or "yes, but change the amount", never triggers a payment. The classifier only accepts a closed confirmation vocabulary in Spanish, English and Portuguese: questions, numbers, corrections, negations and "cancel" never confirm, and every speech-recognition alternative must agree. It is tested case by case.
- **Never twice.** Confirmations are claimed atomically (a button click and a spoken yes can't both run). Payouts use a unique `sender_batch_id` per bill plus a database lock before any money moves; if PayPal times out, the bill stays "in progress" (it can't be paid another way) until PayPal's own status settles it. Payouts are also capped per payment and per day. Every PayPal POST carries an idempotency key reused across retries. If a reply fails after a tool already ran, the agent remembers what was done and never repeats it.
- **Isolated.** Every business only sees and acts on its own invoices, bills, reminders and confirmations, even on one shared sandbox merchant.
- **Untrusted text stays data.** Contact names, notes, remembered facts and PayPal responses are passed as data, never as instructions.
- **Bounded cost.** Rate limits per visitor and server-wide, a cap on new businesses, a daily token budget and a daily voice budget protect the public demo.
- **Sandbox only.** Live PayPal is refused unless explicitly enabled.

## Voice that holds up at a busy counter
- **One brand voice in all three languages** (Fish Audio), streamed so speech starts in about half a second; the browser's own voice is the automatic fallback.
- **Dictation with alternatives**: the agent sees the recognizer's alternatives to pick the right name or number, and reads every item and total back before anything is sent.
- **Fallback transcription** (server speech-to-text) when browser dictation fails or isn't available, with automatic end-of-speech detection.
- Clear states (listening, thinking, speaking), tap to interrupt, no listening while speaking (no echo), queued alerts that never cut a reply, a mute button, and typing always works.
- Remembered contacts mean you rarely dictate an email twice.

## Architecture
```
Browser (Web Speech / MediaRecorder, audio player, AG Grid dashboard)
   │  /api/chat · /api/confirm · /api/state · /api/speech · /api/transcribe
   ▼
FastAPI ── rate limits · budgets · background scheduler · verified PayPal webhooks
   ├── Claude (claude-opus-5-5) tool loop: 20 strict tools, prompt caching, server-side fallback
   ├── Business layer: memory, confirmations, ledger, reminders, financial overview  ──►  SQLite
   ├── PayPal REST: Invoicing v2 · Payouts · Webhooks verification (sandbox)
   └── Fish Audio: text-to-speech (one voice, 3 languages) · speech-to-text
```
| File | What it holds |
|---|---|
| `app/agent.py` | System prompt, tools, the agent loop and its safety rails |
| `app/business.py` | The domain: contacts, invoices, bills, payouts, ledger, reminders, confirmations, overview |
| `app/store.py` | SQLite schema and access |
| `app/paypal.py` | PayPal client: OAuth, retries, idempotency, Invoicing, Payouts, webhook verification |
| `app/voice.py` | Fish Audio voice and transcription, with caching and budgets |
| `app/i18n.py` | Server texts in three languages and the yes/no classifier |
| `app/main.py` | API, scheduler, webhooks, limits |
| `static/` | The dashboard (no build step; AG Grid Community vendored) |

PayPal Invoicing doesn't support Peruvian soles or Colombian/Chilean pesos yet, so invoices are issued in USD (MXN, EUR and GBP also work through `DP_CURRENCY`). That fits the first users: sellers who charge foreign customers in dollars.

## Run it locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # Anthropic key; PayPal sandbox app; optional Fish Audio key and voice id
uvicorn app.main:app --reload
# open http://localhost:8000
```
Without PayPal credentials it runs in **demo mode** with a simulated PayPal (and a "Simulate payment" button).

Checks:
```bash
pytest -q                                              # offline test suite, no keys needed
python scripts/agent_smoke.py                          # real Claude in three languages, simulated PayPal
python scripts/sandbox_smoke.py buyer@personal.example.com   # real PayPal sandbox: invoices, QR, payments, payout
```

## Deploy
`render.yaml` is a ready Render Blueprint with a persistent disk for the memory. Add `ANTHROPIC_API_KEY`, `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET`, and optionally `FISH_API_KEY`, `FISH_VOICE_ID` and `PAYPAL_WEBHOOK_ID` (webhook URL: `https://<your-app>/api/paypal/webhook`).

## Roadmap
- WhatsApp channel: talk to Destiny Pay where small sellers already work.
- Local rails: soles through Peruvian payment providers, the electronic tax receipt (SUNAT), and bank APIs as open finance arrives, behind the same voice and the same confirmation-by-code engine.
- Pilot with 10 tourism businesses in Lima and Cusco.

## Built with
Claude (Anthropic) · PayPal Invoicing v2, Payouts and Webhooks · Fish Audio · FastAPI · SQLite · AG Grid Community · Web Speech API · Render

Built entirely during the hackathon submission period (November 2026).

## Privacy
Sandbox demo: no real money moves. Sample data is fictional. Please don't enter real customer data.

## License
MIT © 2026 Innov4AI SAC. AG Grid Community is MIT licensed (see `static/vendor`).
