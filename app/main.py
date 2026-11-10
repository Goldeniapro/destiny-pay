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
        "authorizations": biz.authorizations(),
        "webhook_last": webhook_last(biz.ws),
        "inbox": biz.inbox(),
        "movements": [{"id": m["id"], "kind": m["kind"], "description": m["description"], "amount": m["amount"], "currency": m["currency"],
                       "method": m["method"], "category": m["category"], "contact": m["contact"], "at": m["at"]}
                      for m in store.q("SELECT * FROM movements WHERE ws=? ORDER BY at DESC LIMIT 12", (ws,))],
        "facts": biz.facts(20),
        "alerts": alerts,
        "last_alert_id": store.last_alert_id(ws),
    }


def webhook_last(ws: str) -> dict:
    """This business's last verified PayPal notification (only its own: never another business's activity)."""
    r = store.one("SELECT event, at FROM webhook_events WHERE ws=? ORDER BY at DESC LIMIT 1", (ws,))
    return {"event": r["event"], "at": int(r["at"])} if r else {}


def readback(conf: dict, lang: str) -> str:
    """What an approval will do, in the code's own words (from the stored proposal, never from the model)."""
    d = conf.get("details") or {}
    key = {"send_invoice": "rb_send_invoice", "cancel_invoice": "rb_cancel_invoice", "pay_bill": "rb_pay_bill",
           "record_payment": "rb_record_payment", "cancel_payout": "rb_cancel_payout", "mark_bill_paid": "rb_mark_bill_paid"}.get(conf.get("kind", ""))
    amount = d.get("total", d.get("amount"))
    if not key or amount is None:
        return ""
    if conf.get("kind") == "pay_bill" and conf.get("new_recipient"):
        key = "rb_pay_bill_button"  # a first payment to an email is never approved by voice: don't invite a "yes"
    return i18n.t(key, lang, name=d.get("customer") or d.get("payee") or "", total=i18n.fmt_money(amount, d.get("currency", ""), lang),
                  email=d.get("email") or "", method=i18n.method_name(d.get("method", ""), lang))


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


def _fresh(stamp: str, window: float = 900) -> bool:
    """PayPal signs a transmission time: a delivery more than 15 minutes off is a replay or a forgery."""
    from datetime import datetime
    try:
        sent = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return False
    return abs(time.time() - sent) <= window


def _seen(event_id: str) -> bool:
    return bool(event_id) and store.one("SELECT 1 FROM webhook_events WHERE id=?", (event_id[:120],)) is not None


def _mark_seen(event_id: str, ws: str = "", event: str = "") -> None:
    """Only after the event was handled (if handling fails, PayPal's retry still gets through). Kept in the database,
    so a restart doesn't make an old delivery new; each business sees only its own events (Security view)."""
    if not event_id:
        return
    store.x("INSERT OR IGNORE INTO webhook_events(id, ws, event, at) VALUES(?,?,?,?)", (event_id[:120], ws, event[:60], time.time()))
    store.x("DELETE FROM webhook_events WHERE at<?", (time.time() - 45 * 86400,))


@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request):
    if DEMO_MODE or not WEBHOOK_ID:
        raise HTTPException(404, "Webhooks are not configured")
    limit(request, "webhook", "webhook_all")  # per sender first, so one forger can't starve PayPal's real deliveries
    cert_url = str(request.headers.get("paypal-cert-url", ""))
    if not cert_url.startswith(CERT_HOSTS) or not _fresh(request.headers.get("paypal-transmission-time", "")):
        # diagnostics in the server log only (a public signal would tell anyone when other businesses get paid)
        log.warning("webhook refused before verification: %s", "cert_host" if not cert_url.startswith(CERT_HOSTS) else "transmission_time")
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
    except PayPalError as e:
        log.warning("webhook verification call failed: %s", str(e)[:200])
        genuine = False
    log.info("webhook %s verified=%s", str(event.get("event_type", ""))[:60], bool(genuine))
    if not genuine:
        raise HTTPException(400, "Signature verification failed")
    event_id = str(event.get("id", ""))
    if _seen(event_id):
        return {"ok": True, "duplicate": True}
    kind = str(event.get("event_type", ""))
    resource = event.get("resource") or {}
    owner = ""
    if kind.startswith("INVOICING.INVOICE."):
        invoice_id = (resource.get("invoice") or {}).get("id") or resource.get("id")
        row = store.one("SELECT ws FROM invoices WHERE id=?", (str(invoice_id),)) if invoice_id else None
        if row:
            owner = row["ws"]
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
            owner = row["ws"]
            store.x("UPDATE payouts SET checked_at=0 WHERE batch_id=?", (str(batch_id),))  # an unclaimed payout is rechecked now, not in 30 min
            try:
                business_factory(row["ws"]).check_payouts()
            except PayPalError:
                log.warning("payout check after webhook failed for %s (%s)", batch_id, status_value)
    _mark_seen(event_id, owner, kind)
    return {"ok": True}


# --- proposals from the tables (no AI): they still go through the authorization dialog ----------------
class ProposalIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    kind: str = Field(pattern="^(send_invoice|cancel_invoice|pay_bill|cancel_payout|mark_bill_paid)$")
    target: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9-]+(\|[A-Z_]{3,20})?$")


@app.post("/api/proposals")
def propose(body: ProposalIn, request: Request):
    limit(request, "chat", "chat_all", "money_day", ws=body.workspace_id)
    if ("|" in body.target) != (body.kind == "mark_bill_paid"):  # only "mark paid" carries a method
        raise HTTPException(422, "Invalid target")
    biz = open_business(body.workspace_id)
    try:
        out = biz.request_confirmation(body.kind, body.target, "table")
    except ValidationError as e:
        raise HTTPException(422, str(e)[:300])
    except PayPalError as e:
        raise HTTPException(404 if "does not belong" in str(e) else 502, "Not found" if "does not belong" in str(e) else "PayPal did not answer")
    return {"confirmation_id": out["confirmation_id"]}


class RemindIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    invoice_id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9-]+$")


@app.post("/api/remind")
def remind(body: RemindIn, request: Request):
    """The owner clicked "Remind": PayPal's own reminder email, no free text (PayPal allows 2 per invoice per day)."""
    limit(request, "chat", "chat_all", "money_day", ws=body.workspace_id)
    biz = open_business(body.workspace_id)
    try:
        out = biz.remind_invoice(body.invoice_id)
    except ValidationError as e:
        raise HTTPException(422, str(e)[:300])
    except PayPalError as e:
        if "does not belong" in str(e):
            raise HTTPException(404, "Not found")
        raise HTTPException(422 if getattr(e, "status", 0) in (400, 422) else 502, "PayPal refused the reminder (it allows 2 per invoice per day)")
    return {"ok": True, "customer": out["customer"]}


class VoiceConfirmIn(BaseModel):
    workspace_id: str = Field(**WS_ID)
    conversation_id: str = Field(**CONV_ID)
    confirmation_id: str = Field(min_length=1, max_length=40)
    fingerprint: str = Field(min_length=8, max_length=64)
    text: str = Field(min_length=1, max_length=300)
    alternatives: list[str] = Field(default_factory=list, max_length=3)
    lang: str = "es"


@app.post("/api/confirm/voice")
def confirm_by_voice(body: VoiceConfirmIn, request: Request):
    """A spoken answer inside the authorization dialog. The server decides what it means, with the same closed
    vocabulary as the conversation, and never for a first payment to an email (that one needs the button)."""
    limit(request, "chat", "chat_all", ws=body.workspace_id)
    biz = open_business(body.workspace_id)
    lang = lang_of(body.lang)
    conf = next((c for c in biz.pending_confirmations() if c["id"] == body.confirmation_id), None)
    if conf is None:
        raise HTTPException(404, "Not found")
    if conf.get("new_recipient"):
        return {"understood": False, "button_only": True, "reply": i18n.t("button_only_new", lang)}
    verdict = i18n.classify_reply(body.text, conf["kind"])
    if verdict == "yes" and any(i18n.classify_reply(a, conf["kind"]) != "yes" for a in body.alternatives if a.strip()):
        verdict = "other"  # the recognizer wasn't sure it heard a yes
    if verdict not in ("yes", "no"):
        return {"understood": False, "reply": i18n.t("voice_not_understood", lang)}
    paying = verdict == "yes" and conf["kind"] == "pay_bill"
    if verdict == "yes":
        limit(request, "money_day", ws=body.workspace_id)
    if paying:
        charge_payout(client_ip(request), conf)  # the same per-visitor allowance as the button
    out = agent.confirm(body.workspace_id, body.conversation_id, body.confirmation_id, verdict == "yes", lang,
                        body.fingerprint if verdict == "yes" else None, via="voice")
    if paying and not out.get("done"):
        payouts_per_ip.charge(client_ip(request), -float((conf.get("details") or {}).get("amount") or 0))
    return {"understood": True, "approved": verdict == "yes", **out}


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
