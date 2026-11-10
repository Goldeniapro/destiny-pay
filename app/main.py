"""FastAPI server for Destiny Pay."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
import threading
import time
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import i18n
from .agent import DestinyPayAgent, ai_configured
from .business import Business, ValidationError
from .fake import FakePayPal
from .i18n import LANGS, classify_reply
from .paypal import UNPAID_STATUSES, PayPalClient, PayPalError
from .seed import NAMES, seed_demo, seed_invoices
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
    last_purge = 0.0
    while not stop.wait(15):
        try:
            scheduler_tick()
            if time.time() - last_purge > 3600:  # hourly: forget businesses idle for two days, trim logs
                last_purge = time.time()
                n = store.purge(_env_int("DP_IDLE_PURGE_HOURS", 48) * 3600)
                if n:
                    log.info("purged %d idle businesses", n)
        except Exception:
            log.exception("scheduler tick failed")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    stop = threading.Event()
    if os.environ.get("DP_SCHEDULER", "1") == "1":
        threading.Thread(target=_scheduler_loop, args=(stop,), daemon=True, name="scheduler").start()
    yield
    stop.set()


app = FastAPI(title="Destiny Pay", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self' data:; "
       "media-src 'self' data: blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()), (b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
    (b"x-frame-options", b"DENY"), (b"permissions-policy", b"microphone=(self), camera=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"), (b"strict-transport-security", b"max-age=63072000; includeSubDomains"),
]


class GuardMiddleware:
    """Security headers on every response, and request bodies capped before anything reads them."""

    def __init__(self, app, default_limit: int = 64 * 1024, upload_limit: int = 9 * 1024 * 1024) -> None:
        self.app, self.default_limit, self.upload_limit = app, default_limit, upload_limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        cap = self.upload_limit if scope["path"] == "/api/transcribe" else self.default_limit
        declared = dict(scope.get("headers") or []).get(b"content-length")
        if declared is not None and (not declared.isdigit() or int(declared) > cap):
            return await self._reject(send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > cap:
                    raise HTTPException(413, "Request too large")
            return message

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] = list(message.get("headers") or []) + SECURITY_HEADERS
            await send(message)

        await self.app(scope, limited_receive, send_with_headers)

    @staticmethod
    async def _reject(send):
        await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json"), *SECURITY_HEADERS]})
        await send({"type": "http.response.body", "body": b'{"detail":"Request too large"}'})


@app.exception_handler(RequestValidationError)
async def validation_error(_request: Request, exc: RequestValidationError):
    # Say which field is wrong, never echo what was sent (it can be long or contain personal data).
    return JSONResponse(status_code=422, content={"detail": [{"loc": list(e.get("loc", [])), "msg": e.get("msg", "")} for e in exc.errors()]})


app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(GuardMiddleware)
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


class DailyQuota:
    """Per-key daily counter (per visitor IP): no single visitor can use up what the whole demo shares."""

    def __init__(self, per_day: int) -> None:
        self.per_day, self.day, self.used = per_day, time.strftime("%Y-%m-%d"), defaultdict(int)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self.day:
                self.day, self.used = today, defaultdict(int)
            if self.used[key] >= self.per_day:
                return False
            self.used[key] += 1
            return True


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, str(default)))


class AmountQuota:
    """Per-visitor daily total (tokens, or money paid out): one visitor can't use up what the whole demo shares."""

    def __init__(self, per_day: float) -> None:
        self.per_day, self.day, self.used = per_day, time.strftime("%Y-%m-%d"), defaultdict(float)
        self._lock = threading.Lock()

    def _roll(self) -> None:
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.used = today, defaultdict(float)

    def left(self, key: str) -> float:
        with self._lock:
            self._roll()
            return self.per_day - self.used[key]

    def take(self, key: str, amount: float) -> bool:
        with self._lock:
            self._roll()
            if self.used[key] + amount > self.per_day:
                return False
            self.used[key] += amount
            return True

    def charge(self, key: str, amount: float) -> None:
        with self._lock:
            self._roll()
            self.used[key] += amount


tokens_per_ip = AmountQuota(_env_int("DP_TOKENS_PER_IP_DAY", 600000))
payouts_per_ip = AmountQuota(_env_int("DP_PAYOUT_PER_IP_DAY", 3000))

quotas = {
    "workspace_day": DailyQuota(_env_int("DP_WORKSPACES_PER_IP_DAY", 60)),
    "chat_day": DailyQuota(_env_int("DP_CHATS_PER_IP_DAY", 300)),
    "transcribe_day": DailyQuota(_env_int("DP_TRANSCRIBES_PER_IP_DAY", 150)),
    "money_day": DailyQuota(_env_int("DP_MONEY_ACTIONS_PER_IP_DAY", 40)),
}

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
    "state_ws": RateLimiter(_env_int("DP_STATE_PER_WORKSPACE_PER_MINUTE", 60)),
    "webhook_all": RateLimiter(_env_int("DP_WEBHOOKS_PER_MINUTE", 120)),
    "webhook": RateLimiter(_env_int("DP_WEBHOOKS_PER_IP_PER_MINUTE", 30)),
}


# Railway's edge overwrites X-Real-IP with the visitor's real address (a forged one is replaced, verified live);
# elsewhere set DP_CLIENT_IP_HEADER to the header your host's proxy guarantees.
CLIENT_IP_HEADER = os.environ.get("DP_CLIENT_IP_HEADER", "").lower() or (
    "x-real-ip" if any(k.startswith("RAILWAY_") for k in os.environ) else "")


def _bucket(ip: str) -> str:
    """IPv6 visitors usually own a whole /64: count it as one visitor so rotating addresses doesn't reset limits."""
    if ":" in ip:
        try:
            return str(ipaddress.ip_network(f"{ip}/64", strict=False))
        except ValueError:
            return ip
    return ip


def client_ip(request: Request) -> str:
    return _bucket(_raw_client_ip(request))


def _raw_client_ip(request: Request) -> str:
    if CLIENT_IP_HEADER:
        trusted = request.headers.get(CLIENT_IP_HEADER, "").strip()
        if trusted:
            return trusted
    # Otherwise the proxy appends the address it saw as the LAST hop; anything before it came from the client and can be forged.
    hops = [h.strip() for h in request.headers.get("x-forwarded-for", "").split(",") if h.strip()]
    return hops[-1] if hops else (request.client.host if request.client else "unknown")


def limit(request: Request, *names: str, ws: str = "") -> None:
    ip = client_ip(request)
    for name in names:
        if name in quotas:
            if not quotas[name].allow(ip):
                raise HTTPException(429, "Daily demo limit reached for this connection. Please come back tomorrow.")
            continue
        key = "all" if name.endswith("_all") else ws if name.endswith("_ws") else ip
        if not limits[name].allow(key):
            raise HTTPException(429, "Too many requests. Wait a minute and try again.")


def charge_payout(ip: str, conf: dict) -> None:
    """Count a supplier payment against the visitor's daily PayPal allowance before it can run."""
    amount = float((conf.get("details") or {}).get("amount") or 0)
    if not payouts_per_ip.take(ip, amount):
        raise HTTPException(429, "Daily PayPal payment limit reached for this connection. Please come back tomorrow.")


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
@app.api_route("/", methods=["GET", "HEAD"])
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/privacy")
def privacy():
    return FileResponse(STATIC / "privacy.html")


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
        "build": (os.environ.get("RAILWAY_GIT_COMMIT_SHA") or os.environ.get("RENDER_GIT_COMMIT") or "")[:7],
    }


# --- workspaces (a business with its own memory) ----------------------------------------------
SEED_WAIT_SECONDS = 12


class WorkspaceIn(BaseModel):
    lang: str = "es"
    tz: str = Field(default="America/Lima", max_length=64)
    sample: bool = True


@app.post("/api/workspace")
def create_workspace(body: WorkspaceIn, request: Request):
    limit(request, "workspace", "workspace_all", "workspace_day")
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
        if not DEMO_MODE:
            # real sandbox invoices take a few seconds: wait for them briefly so the first screen is complete,
            # and if PayPal is slow, let the page show that they are still being created
            SEEDING.add(ws)
            t = threading.Thread(target=_seed_invoices, args=(ws, lang), daemon=True, name="seed")
            t.start()
            t.join(timeout=SEED_WAIT_SECONDS)
    return {"workspace_id": ws}


SEEDING: set[str] = set()  # sample businesses whose real sandbox invoices are still being created (one worker process)


def _seed_invoices(ws: str, lang: str) -> None:
    try:
        seed_invoices(business_factory(ws), lang)
    except Exception:
        log.warning("sample invoices could not be created on PayPal", exc_info=True)
    finally:
        SEEDING.discard(ws)


@app.get("/api/state")
def state(request: Request, ws: str = Header(alias="x-workspace", **WS_ID), lang: str = "es", since: int = 0):
    limit(request, "state", "state_ws", ws=ws)
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
        "business": {"name": biz.info["name"], "currency": biz.currency, "tz": biz.tz.key, "sample": bool(biz.info["demo"]),
                     "seeding": ws in SEEDING},
        "overview": biz.overview("month"),
        "cashflow": biz.cashflow(30),
        "invoices": biz.list_invoices("all")[:50],
        "bills": [b for b in biz.list_bills("all") if b["status"] in ("pending", "processing")] + [b for b in biz.list_bills("paid")][-10:],
        "reminders": biz.list_reminders(),
        "contacts": biz.find_contacts("", limit=100),
        "activity": [render_activity(a, lang) for a in store.q("SELECT id, at, kind, text, data FROM activity WHERE ws=? ORDER BY id DESC LIMIT 40", (ws,))],
        "confirmations": [{**c, "readback": readback(c, lang)} for c in biz.pending_confirmations()],
        "movements": [{"id": m["id"], "kind": m["kind"], "description": m["description"], "amount": m["amount"], "currency": m["currency"],
                       "method": m["method"], "category": m["category"], "contact": m["contact"], "at": m["at"]}
                      for m in store.q("SELECT * FROM movements WHERE ws=? ORDER BY at DESC LIMIT 12", (ws,))],
        "facts": biz.facts(20),
        "alerts": alerts,
        "last_alert_id": store.last_alert_id(ws),
    }


def readback(conf: dict, lang: str) -> str:
    """What an approval will do, in the code's own words (from the stored proposal, never from the model)."""
    d = conf.get("details") or {}
    key = {"send_invoice": "rb_send_invoice", "cancel_invoice": "rb_cancel_invoice", "pay_bill": "rb_pay_bill"}.get(conf.get("kind", ""))
    amount = d.get("total", d.get("amount"))
    if not key or amount is None:
        return ""
    if conf.get("kind") == "pay_bill" and conf.get("new_recipient"):
        key = "rb_pay_bill_button"  # a first payment to an email is never approved by voice: don't invite a "yes"
    return i18n.t(key, lang, name=d.get("customer") or d.get("payee") or "", total=i18n.fmt_money(amount, d.get("currency", ""), lang), email=d.get("email") or "")


@app.get("/api/confirmation/{cid}/readback")
def confirmation_readback(cid: str, request: Request, ws: str = Header(alias="x-workspace", **WS_ID), lang: str = "es"):
    limit(request, "speech")
    biz = open_business(ws)
    conf = next((c for c in biz.pending_confirmations() if c["id"] == cid), None)
    if conf is None:
        raise HTTPException(404, "Not found")
    text = readback(conf, lang_of(lang))
    return {"text": text, "speech_id": voice.register(text, lang_of(lang))}


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
def briefing(request: Request, ws: str = Header(alias="x-workspace", **WS_ID), lang: str = "es"):
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
    limit(request, "chat", "chat_all", "chat_ws", "chat_day", ws=body.workspace_id)
    open_business(body.workspace_id)
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    ip = client_ip(request)
    if tokens_per_ip.left(ip) <= 0:
        raise HTTPException(429, "Daily demo limit reached for this connection. Please come back tomorrow.")
    pending = agent.conversations.peek(body.conversation_id, body.workspace_id)
    if pending is not None and pending.awaiting:
        limit(request, "money_day")  # this message may approve a payment by voice
        conf = next((c for c in open_business(body.workspace_id).pending_confirmations() if c["id"] == pending.awaiting), None)
        if conf and conf["kind"] == "pay_bill" and classify_reply(text, "pay_bill") == "yes":
            charge_payout(ip, conf)
    alternatives = [a[:300] for a in body.alternatives]
    res = agent.chat(body.workspace_id, body.conversation_id, text, lang_of(body.lang), alternatives)
    conv = agent.conversations.peek(body.conversation_id, body.workspace_id)
    if conv is not None:
        tokens_per_ip.charge(ip, conv.spent)
    res["speech_id"] = voice.register(res.get("reply", ""), conv.lang if conv else lang_of(body.lang))
    return res


class ConfirmIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    conversation_id: str = Field(**CONV_ID)
    confirmation_id: str = Field(min_length=6, max_length=32, pattern=r"^[a-f0-9]+$")
    approve: bool
    lang: str = "es"
    # fingerprint of the exact operation the owner reviewed; required to approve
    fingerprint: str = Field(default="", max_length=64, pattern=r"^[a-f0-9]*$")


@app.post("/api/confirm")
def confirm(body: ConfirmIn, request: Request):
    limit(request, "chat", "chat_ws", *(["money_day"] if body.approve else []), ws=body.workspace_id)
    biz = open_business(body.workspace_id)
    if body.approve and not body.fingerprint:
        raise HTTPException(400, "An approval must name the exact operation it approves")
    conf = next((c for c in biz.pending_confirmations() if c["id"] == body.confirmation_id), None) if body.approve else None
    if conf and conf["kind"] == "pay_bill":
        charge_payout(client_ip(request), conf)
    res = agent.confirm(body.workspace_id, body.conversation_id, body.confirmation_id, body.approve, lang_of(body.lang),
                        body.fingerprint if body.approve else None)
    if conf and conf["kind"] == "pay_bill" and not res.get("done"):
        payouts_per_ip.charge(client_ip(request), -float((conf.get("details") or {}).get("amount") or 0))  # nothing paid: give it back
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
        raise HTTPException(404, "Not found") if "unknown" in str(e) else HTTPException(503, "Voice unavailable")
    return StreamingResponse(audio, media_type="audio/mpeg", headers={"Cache-Control": "no-store"})


@app.post("/api/transcribe")
def transcribe(request: Request, workspace_id: str = Form(**WS_ID), lang: str = Form("es"), audio: UploadFile = File(...)):
    # Plain def: the upstream call runs in the thread pool and never blocks the server's event loop.
    limit(request, "transcribe", "transcribe_all", "transcribe_day")
    open_business(workspace_id)
    data = audio.file.read(8 * 1024 * 1024 + 1)
    try:
        text = voice.transcribe(data, audio.filename or "speech.webm", audio.content_type or "audio/webm", lang_of(lang))
    except VoiceError as e:
        log.warning("transcription failed: %s", e)
        raise HTTPException(503, "Transcription unavailable")
    return {"text": text}


# --- PayPal webhooks ------------------------------------------------------------------------------
CERT_HOSTS = ("https://api.paypal.com/", "https://api.sandbox.paypal.com/", "https://api-m.paypal.com/", "https://api-m.sandbox.paypal.com/")
_seen_events: OrderedDict[str, float] = OrderedDict()
_seen_lock = threading.Lock()


def _fresh(stamp: str, window: float = 900) -> bool:
    """PayPal signs a transmission time: a delivery more than 15 minutes off is a replay or a forgery."""
    from datetime import datetime
    try:
        sent = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return abs(time.time() - sent) <= window


def _seen(event_id: str) -> bool:
    with _seen_lock:
        return bool(event_id) and event_id in _seen_events


def _mark_seen(event_id: str) -> None:
    """Only after the event was handled: if handling fails, PayPal's retry still gets through."""
    if not event_id:
        return
    with _seen_lock:
        _seen_events[event_id] = time.time()
        while len(_seen_events) > 5000:
            _seen_events.popitem(last=False)


@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request):
    if DEMO_MODE or not WEBHOOK_ID:
        raise HTTPException(404, "Webhooks are not configured")
    limit(request, "webhook", "webhook_all")  # per sender first, so one forger can't starve PayPal's real deliveries
    if not str(request.headers.get("paypal-cert-url", "")).startswith(CERT_HOSTS) or not _fresh(request.headers.get("paypal-transmission-time", "")):
        raise HTTPException(400, "Signature verification failed")  # cheap checks before calling PayPal
    raw = (await request.body()).decode("utf-8", "replace")
    try:
        event = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Invalid JSON")
    if not isinstance(event, dict):
        raise HTTPException(400, "Invalid JSON")
    # PayPal calls are blocking: run them in the thread pool so the rest of the app keeps answering.
    return await run_in_threadpool(_handle_webhook, dict(request.headers), event, raw)


def _handle_webhook(headers: dict, event: dict, raw: str = "") -> dict:
    try:
        genuine = paypal.verify_webhook(headers, raw or event, WEBHOOK_ID)
    except PayPalError:
        genuine = False
    if not genuine:
        raise HTTPException(400, "Signature verification failed")
    event_id = str(event.get("id", ""))
    if _seen(event_id):
        return {"ok": True, "duplicate": True}
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
    _mark_seen(event_id)
    return {"ok": True}


# --- demo helper ----------------------------------------------------------------------------------
@app.post("/api/demo/pay/{invoice_id}")
def demo_pay(invoice_id: str, request: Request, ws: str = Header(alias="x-workspace", **WS_ID)):
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
