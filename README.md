# Destiny Pay 🎙️

**A multilingual voice cashier for small businesses. Say who to bill, and Destiny Pay creates, sends and tracks a real PayPal invoice.**

> 🔗 **Live demo:** _coming soon_ · 🎬 **Video:** _coming soon_ · Built for the PayPal AI Hackathon 2026 by Innov4AI SAC (Lima, Peru)

Millions of small sellers in Latin America run their business from a phone, WhatsApp and a notebook.
Getting paid is manual: they type amounts, forget who owes them and rarely send reminders.
Destiny Pay lets the owner just **talk**, in Spanish, English or Portuguese:

> 🗣️ "Cóbrale a Juan Pérez 3 cajas de té a 12 dólares, su correo es juan arroba correo punto com"
>
> 🤖 "Juan Pérez, 36 dollars, to j-u-a-n at correo.com. Shall I send it?"
>
> 🗣️ "Sí, envíala" → a PayPal invoice with a payment link lands in Juan's inbox, and a QR is ready on screen.
>
> 🔔 Later: "Juan Pérez just paid you 36.00 USD with PayPal!"

## Try it in 60 seconds
1. Open the live demo in **Chrome, Edge or Safari** and pick your language (top right).
2. Tap 🎙️ and say, or tap the first example: *"Bill John Smith for 3 boxes of tea at 12 dollars, email john at example dot com"*.
3. Destiny Pay reads the draft back. Say **"Yes, send it"**. Nothing is sent before you confirm.
4. Say **"Show me John's QR code"**. A customer can scan it to pay.
5. Pay the invoice with a PayPal sandbox personal account (or press **Simulate payment** in demo mode). Within seconds Destiny Pay announces the payment out loud.
6. Ask **"Who owes me?"** or say **"John paid me 10 dollars in cash"** to record an offline payment.

No microphone? Type in the box: it is the same agent.

## What it does
| You say | Destiny Pay does | PayPal API |
|---|---|---|
| "Bill Ana for 2 cakes at 15" | Creates a **draft** invoice and reads it back | `POST /v2/invoicing/invoices` |
| "Yes, send it" | Sends it with a PayPal payment link | `POST /v2/invoicing/invoices/{id}/send` |
| "Show the QR" | Shows the invoice's payment QR on screen | `POST /v2/invoicing/invoices/{id}/generate-qr-code` |
| "Who owes me?" / "Did Ana pay?" | Lists unpaid invoices / checks one | `GET /v2/invoicing/invoices/{id}` |
| "Ana paid me in cash" (or Yape, Plin, transfer) | Records the offline payment | `POST /v2/invoicing/invoices/{id}/payments` |
| "Remind Ana" / "Cancel Ana's invoice" | Sends a reminder / cancels | `/remind`, `/cancel` |
| _(customer pays online)_ | Announces the payment out loud | status polling |

## Designed so the agent can be trusted with money
- **Two-step charges.** The agent can only create drafts on its own. Sending requires the owner's explicit confirmation in a later turn.
- **Validated inputs.** Emails dictated as words ("arroba", "at", "ponto") are normalized and checked; quantities, prices and totals have hard limits; the agent never invents an email.
- **No double charges.** An identical draft requested twice within two minutes is reused, not duplicated.
- **Isolation.** Every browser session only sees and acts on the invoices it created, even though all visitors share one sandbox merchant.
- **Tool results are data, not instructions.** Every tool call gets a result, errors are explained in plain words, and a failed turn is rolled back so the conversation never breaks.
- **Bounded cost.** Per-IP and global rate limits, a per-session turn cap and a daily token budget protect the public demo.
- **Sandbox only.** Live PayPal is refused unless explicitly enabled with `ALLOW_LIVE=1`.

## Voice that holds up in a real shop
- Browser speech recognition (no app, no install), with live transcription while you speak.
- Clear states: listening, thinking, speaking. Tap to interrupt the assistant at any time.
- It never listens while it talks, so it does not hear itself.
- Friendly fallbacks for a blocked microphone, silence, no microphone or no network, and typing always works.
- Native voices per language (es-PE, en-US, pt-BR), and a mute button for quiet places.

## Architecture
```
Browser: Web Speech API (STT + TTS), es / en / pt
   │  POST /api/chat {session_id, message, lang}
   ▼
FastAPI ── rate limits, sessions, token budget
   │
   ├── Claude (claude-opus-5-5) tool-use loop: strict tool schemas, low effort for fast spoken replies,
   │   prompt caching, server-side model fallback
   │
   └── ScopedBackend (per-session isolation) ──► PayPal Invoicing v2 (sandbox)
                                               └► in-memory PayPal in demo mode
```
- `app/agent.py`: system prompt, tools, validation, the agent loop and its safety rails.
- `app/paypal.py`: PayPal REST client (OAuth with token cache, retries on 429/5xx, token refresh on 401).
- `app/scope.py`: per-session isolation and invoice caching.
- `app/fake.py`: in-memory PayPal used by tests and by demo mode.
- `static/index.html`: single-page voice UI, no build step.

PayPal Invoicing does not support Peruvian soles, Colombian or Chilean pesos yet, so invoices are issued in USD (MXN, EUR and GBP also work through `DP_CURRENCY`).

## Run it locally
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env        # add your Anthropic key and PayPal sandbox app credentials
uvicorn app.main:app --reload
# open http://localhost:8000
```
Without PayPal credentials the app runs in **demo mode** with in-memory invoices.

Checks:
```bash
pytest -q                                              # 35 offline tests, no keys needed
python scripts/agent_smoke.py                          # real Claude, 3 languages, in-memory PayPal
python scripts/sandbox_smoke.py you-buyer@personal.example.com   # real PayPal sandbox, end to end
```

## Deploy
`render.yaml` is a ready Render Blueprint: create a Blueprint from this repo and type `ANTHROPIC_API_KEY`, `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET` in the dashboard. Run a single instance: sessions live in memory.

## Roadmap
- PayPal webhooks (`INVOICING.INVOICE.PAID`) instead of polling.
- WhatsApp channel, so the owner can talk to Destiny Pay where they already work.
- Local currencies as PayPal adds them, and daily sales summaries by voice.

## Privacy
This is a sandbox demo: no real money moves. Conversations are kept in memory for 30 minutes and never stored. Please do not enter real customer data.

## License
MIT © 2026 Innov4AI SAC
