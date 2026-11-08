"""FastAPI server for Destiny Pay."""

from __future__ import annotations

import logging
import os
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import i18n
from .agent import DestinyPayAgent, ai_configured
from .business import Business, ValidationError
from .fake import FakePayPal
from .i18n import LANGS
from .paypal import UNPAID_STATUSES, PayPalClient, PayPalError
from .seed import NAMES, seed_demo
from .store import Store
from .voice import Voice, VoiceError

load_dotenv()
log = logging.getLogger("destiny_pay")
STATIC = Path(__file__).resolve().parent.parent / "static"

DEMO_MODE = not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID", "")
store = Store()
paypal = FakePayPal(store) if DEMO_MODE else PayPalClient.from_env()
voice = Voice()

WS_ID = dict(min_length=32, max_length=32, pattern=r"^[a-f0-9]{32}$")
CONV_ID = dict(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-]+$")


def business_factory(ws: str) -> Business:
    return Business(store, paypal, ws)


agent = DestinyPayAgent(business_factory)


# --- background work: payment status, due reminders, bills due tomorrow ----------------------------
def scheduler_tick() -> None:
    now = time.time()
    for r in store.q("SELECT DISTINCT ws FROM reminders WHERE status='pending' AND due_at<=?", (now,)):
        try:
            business_factory(r["ws"]).fire_due_reminders()
        except Exception:
            log.exception("reminders failed for a workspace")
    for ws in store.active_workspaces(600):
        try:
            biz = business_factory(ws)
            stale = store.q("SELECT id FROM invoices WHERE ws=? AND refreshed_at<? AND status IN (%s) LIMIT 10"
                            % ",".join("?" * len(UNPAID_STATUSES)), (ws, now - 20, *UNPAID_STATUSES))
            for row in stale:
                try:
                    biz.refresh_invoice(row["id"])
                except PayPalError:
                    store.x("UPDATE invoices SET refreshed_at=? WHERE id=?", (now, row["id"]))
            tomorrow = (biz.today().toordinal() + 1)
            from datetime import date
            for b in store.q("SELECT * FROM bills WHERE ws=? AND status='pending' AND alerted=0 AND due_date=?",
                             (ws, date.fromordinal(tomorrow).isoformat())):
                store.alert(ws, "a_bill_due", payee=b["payee"], amount=[b["amount"], b["currency"]])
                store.x("UPDATE bills SET alerted=1 WHERE id=?", (b["id"],))
        except Exception:
            log.exception("refresh failed for a workspace")
    for r in store.q("SELECT DISTINCT ws FROM payouts WHERE (status NOT IN ('done','failed','unclaimed') AND checked_at<?) "
                     "OR (status='unclaimed' AND checked_at<?)", (now - 30, now - 1800)):
        try:
            business_factory(r["ws"]).check_payouts()
        except Exception:
            log.exception("payout check failed for a workspace")


def _scheduler_loop(stop: threading.Event) -> None:
    while not stop.wait(15):
        try:
            scheduler_tick()
        except Exception:
            log.exception("scheduler tick failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    if os.environ.get("DP_SCHEDULER", "1") == "1":
        threading.Thread(target=_scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()
    yield
    stop.set()


app = FastAPI(title="Destiny Pay", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


# --- abuse limits -----------------------------------------------------------------------------
class RateLimiter:
    def __init__(self, per_minute: int) -> None:
        self.per_minute = per_minute
        self.hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            if len(self.hits) > 5000:  # forget idle keys so spoofed ones can't grow memory forever
                for k in [k for k, q in self.hits.items() if not q or now - q[-1] > 60]:
                    del self.hits[k]
            q = self.hits[key]
            while q and now - q[0] > 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


limits = {
    "chat": RateLimiter(_env_int("DP_CHAT_PER_MINUTE", 15)),
    "chat_all": RateLimiter(_env_int("DP_GLOBAL_PER_MINUTE", 120)),
    "state": RateLimiter(_env_int("DP_STATE_PER_MINUTE", 120)),
    "workspace": RateLimiter(_env_int("DP_WORKSPACES_PER_MINUTE", 6)),
    "workspace_all": RateLimiter(_env_int("DP_WORKSPACES_GLOBAL_PER_MINUTE", 40)),
    "speech": RateLimiter(_env_int("DP_SPEECH_PER_MINUTE", 40)),
    "transcribe": RateLimiter(_env_int("DP_TRANSCRIBE_PER_MINUTE", 15)),
    "transcribe_all": RateLimiter(_env_int("DP_TRANSCRIBE_GLOBAL_PER_MINUTE", 60)),
    "chat_ws": RateLimiter(_env_int("DP_CHAT_PER_WORKSPACE_PER_MINUTE", 15)),
}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


def limit(request: Request, *names: str, ws: str = "") -> None:
    # The forwarded IP can be spoofed, so every per-IP limit is backed by a per-business or server-wide one.
    ip = client_ip(request)
    for name in names:
        key = "all" if name.endswith("_all") else ws if name.endswith("_ws") else ip
        if not limits[name].allow(key):
            raise HTTPException(429, "Too many requests. Wait a minute and try again.")


def open_business(ws: str) -> Business:
    try:
        biz = business_factory(ws)
    except ValidationError:
        raise HTTPException(404, "Unknown workspace")
    store.touch(ws)
    return biz


def lang_of(lang: str) -> str:
    return lang if lang in LANGS else "en"


# --- pages and status -------------------------------------------------------------------------
@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/status")
def status():
    return {
        "demo_mode": DEMO_MODE,
        "paypal_env": "demo" if DEMO_MODE else ("live" if "sandbox" not in paypal.base_url else "sandbox"),
        "currency": paypal.currency,
        "ai_ready": ai_configured(),
        "voice": {"tts": voice.tts_enabled, "stt": voice.stt_enabled},
        "webhooks": bool(WEBHOOK_ID) and not DEMO_MODE,
        "langs": list(LANGS),
    }


# --- workspaces (a business with its own memory) ----------------------------------------------
class WorkspaceIn(BaseModel):
    lang: str = "es"
    tz: str = Field(default="America/Lima", max_length=64)
    sample: bool = True


@app.post("/api/workspace")
def create_workspace(body: WorkspaceIn, request: Request):
    limit(request, "workspace", "workspace_all")
    lang = lang_of(body.lang)
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(body.tz)
        tz = body.tz
    except Exception:
        tz = "America/Lima"
    ws = secrets.token_hex(16)
    store.create_workspace(ws, NAMES.get(lang, NAMES["en"]), lang, tz, paypal.currency, body.sample)
    if body.sample:
        seed_demo(business_factory(ws), lang)
    return {"workspace_id": ws}


@app.get("/api/state")
def state(request: Request, ws: str = Query(**WS_ID), lang: str = "es", since: int = 0):
    limit(request, "state")
    biz = open_business(ws)
    lang = lang_of(lang)
    alerts = []
    for a in store.alerts_since(ws, max(since, 0)):
        params = money_params(a["params"], lang)
        try:
            text = i18n.t(a["key"], lang, **params)
        except (KeyError, IndexError):
            continue
        alerts.append({"id": a["id"], "text": text, "key": a["key"], "speech_id": voice.register(text, lang)})
    return {
        "business": {"name": biz.info["name"], "currency": biz.currency, "tz": biz.tz.key, "sample": bool(biz.info["demo"])},
        "overview": biz.overview("month"),
        "cashflow": biz.cashflow(30),
        "invoices": biz.list_invoices("all")[:50],
        "bills": [b for b in biz.list_bills("all") if b["status"] == "pending"] + [b for b in biz.list_bills("paid")][-10:],
        "reminders": biz.list_reminders(),
        "contacts": biz.find_contacts("", limit=100),
        "activity": [render_activity(a, lang) for a in store.q("SELECT id, at, kind, text, data FROM activity WHERE ws=? ORDER BY id DESC LIMIT 40", (ws,))],
        "confirmations": biz.pending_confirmations(),
        "facts": biz.facts(20),
        "alerts": alerts,
        "last_alert_id": store.last_alert_id(ws),
    }


def money_params(params: dict, lang: str) -> dict:
    return {k: (i18n.fmt_money(v[0], v[1], lang) if isinstance(v, list) and len(v) == 2 else v) for k, v in params.items()}


def render_activity(row: dict, lang: str) -> dict:
    import json as _json
    try:
        data = _json.loads(row.get("data") or "{}")
    except ValueError:
        data = {}
    text = i18n.log_text(data.get("_k", ""), lang, money_params(data.get("_p", {}), lang), row["text"]) if data.get("_k") else row["text"]
    return {"id": row["id"], "at": row["at"], "kind": row["kind"], "text": text}


@app.get("/api/briefing")
def briefing(request: Request, ws: str = Query(**WS_ID), lang: str = "es"):
    limit(request, "state")
    biz = open_business(ws)
    text = biz.briefing(lang_of(lang))
    return {"text": text, "speech_id": voice.register(text, lang_of(lang))}


# --- conversation -----------------------------------------------------------------------------
class ChatIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    conversation_id: str = Field(**CONV_ID)
    message: str = Field(min_length=1, max_length=2000)
    lang: str = "es"
    alternatives: list[str] = Field(default_factory=list, max_length=3)


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    limit(request, "chat", "chat_all", "chat_ws", ws=body.workspace_id)
    open_business(body.workspace_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    alternatives = [a[:300] for a in body.alternatives]
    res = agent.chat(body.workspace_id, body.conversation_id, text, lang_of(body.lang), alternatives)
    conv = agent.conversations.peek(body.conversation_id)
    res["speech_id"] = voice.register(res.get("reply", ""), conv.lang if conv else lang_of(body.lang))
    return res


class ConfirmIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    conversation_id: str = Field(**CONV_ID)
    confirmation_id: str = Field(min_length=6, max_length=32, pattern=r"^[a-f0-9]+$")
    approve: bool
    lang: str = "es"


@app.post("/api/confirm")
def confirm(body: ConfirmIn, request: Request):
    limit(request, "chat", "chat_ws", ws=body.workspace_id)
    open_business(body.workspace_id)
    res = agent.confirm(body.workspace_id, body.conversation_id, body.confirmation_id, body.approve, lang_of(body.lang))
    res["speech_id"] = voice.register(res["reply"], lang_of(body.lang))
    return res


# --- voice --------------------------------------------------------------------------------------
@app.get("/api/speech/{speech_id}")
def speech(speech_id: str, request: Request):
    limit(request, "speech")
    if len(speech_id) > 40:
        raise HTTPException(404, "Not found")
    try:
        audio = voice.stream(speech_id)
    except VoiceError as e:
        raise HTTPException(404 if "unknown" in str(e) else 503, str(e))
    return StreamingResponse(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/transcribe")
async def transcribe(request: Request, workspace_id: str = Form(**WS_ID), lang: str = Form("es"), audio: UploadFile = File(...)):
    limit(request, "transcribe", "transcribe_all")
    open_business(workspace_id)
    data = await audio.read(8 * 1024 * 1024 + 1)
    try:
        text = voice.transcribe(data, audio.filename or "speech.webm", audio.content_type or "audio/webm", lang_of(lang))
    except VoiceError as e:
        raise HTTPException(503, str(e))
    return {"text": text}


# --- PayPal webhooks ------------------------------------------------------------------------------
@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request):
    try:
        event = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")
    if DEMO_MODE or not WEBHOOK_ID:
        raise HTTPException(404, "Webhooks are not configured")
    try:
        genuine = paypal.verify_webhook(dict(request.headers), event, WEBHOOK_ID)
    except PayPalError:
        genuine = False
    if not genuine:
        raise HTTPException(400, "Signature verification failed")
    kind = str(event.get("event_type", ""))
    resource = event.get("resource") or {}
    if kind.startswith("INVOICING.INVOICE."):
        invoice_id = (resource.get("invoice") or {}).get("id") or resource.get("id")
        row = store.one("SELECT ws FROM invoices WHERE id=?", (str(invoice_id),)) if invoice_id else None
        if row:
            try:
                business_factory(row["ws"]).refresh_invoice(invoice_id)
            except PayPalError:
                log.warning("webhook refresh failed for %s", invoice_id)
    elif kind.startswith("PAYMENT.PAYOUTS"):
        header = resource.get("batch_header") or {}
        batch_id = header.get("payout_batch_id") or resource.get("payout_batch_id")
        status_value = header.get("batch_status") or resource.get("transaction_status") or kind.rsplit(".", 1)[-1]
        row = store.one("SELECT ws FROM payouts WHERE batch_id=?", (str(batch_id),)) if batch_id else None
        if row:  # PayPal's own status decides: settle the bill, or put it back to pending and tell the owner
            try:
                business_factory(row["ws"]).check_payouts()
            except PayPalError:
                log.warning("payout check after webhook failed for %s (%s)", batch_id, status_value)
    return {"ok": True}


# --- demo helper ----------------------------------------------------------------------------------
@app.post("/api/demo/pay/{invoice_id}")
def demo_pay(invoice_id: str, request: Request, ws: str = Query(**WS_ID)):
    """Demo mode only: simulate the customer paying online, so the 'payment received' flow can be tried."""
    limit(request, "chat")
    if not DEMO_MODE:
        raise HTTPException(404, "Not available")
    biz = open_business(ws)
    try:
        biz.demo_mark_paid(invoice_id)
    except PayPalError as e:
        raise HTTPException(404 if "does not belong" in str(e) else 409, str(e))
    return {"ok": True}
