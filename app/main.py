"""FastAPI server for Destiny Pay."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from .agent import LANGS, DestinyPayAgent, ai_configured
from .fake import FakePayPal
from .paypal import PayPalClient, PayPalError, summarize

load_dotenv()
STATIC = Path(__file__).resolve().parent.parent / "static"

DEMO_MODE = not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
_shared_paypal = None if DEMO_MODE else PayPalClient.from_env()

SESSION_ID = dict(min_length=8, max_length=64, pattern=r"^[A-Za-z0-9\-]+$")


def backend_factory(session_id: str):
    # Demo mode: each visitor gets a private in-memory PayPal. Sandbox mode: one shared
    # sandbox merchant, isolated per session by ScopedBackend.
    return FakePayPal() if DEMO_MODE else _shared_paypal


agent = DestinyPayAgent(backend_factory)
app = FastAPI(title="Destiny Pay")


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


chat_limiter = RateLimiter(int(os.environ.get("DP_CHAT_PER_MINUTE", "12")))
invoices_limiter = RateLimiter(int(os.environ.get("DP_INVOICES_PER_MINUTE", "60")))
# The forwarded IP can be spoofed, so server-wide caps back it up (with the daily token budget):
# total chat requests, and new sessions (so nobody can flood the session store and evict real visitors).
global_limiter = RateLimiter(int(os.environ.get("DP_GLOBAL_PER_MINUTE", "90")))
new_session_limiter = RateLimiter(int(os.environ.get("DP_NEW_SESSIONS_PER_MINUTE", "30")))


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "unknown")


class ChatIn(BaseModel):
    session_id: str = Field(**SESSION_ID)
    message: str = Field(min_length=1, max_length=2000)
    lang: str = "es"


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {
        "demo_mode": DEMO_MODE,
        "paypal_env": "demo" if DEMO_MODE else ("live" if "sandbox" not in _shared_paypal.base_url else "sandbox"),
        "currency": "USD" if DEMO_MODE else _shared_paypal.currency,
        "ai_ready": ai_configured(),
        "langs": list(LANGS),
    }


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    if not chat_limiter.allow(client_ip(request)) or not global_limiter.allow("all"):
        raise HTTPException(429, "Too many requests. Wait a minute and try again.")
    if agent.sessions.peek(body.session_id) is None and not new_session_limiter.allow("all"):
        raise HTTPException(429, "Too many new sessions. Wait a minute and try again.")
    text = body.message.strip()
    if not text:
        raise HTTPException(400, "Empty message")
    return agent.chat(body.session_id, text, body.lang if body.lang in LANGS else "es")


@app.get("/api/invoices")
def invoices(request: Request, session_id: str = Query(**SESSION_ID)):
    if not invoices_limiter.allow(client_ip(request)):
        raise HTTPException(429, "Too many requests.")
    session = agent.sessions.peek(session_id)
    if session is None:
        return []
    try:
        return [summarize(i) for i in session.backend.list_invoices()]
    except PayPalError as e:
        raise HTTPException(502, str(e))


@app.post("/api/demo/pay/{invoice_id}")
def demo_pay(invoice_id: str, session_id: str = Query(**SESSION_ID)):
    """Demo mode only: simulate the customer paying online, so the 'payment received' flow can be tried."""
    session = agent.sessions.peek(session_id)
    if not DEMO_MODE or session is None or invoice_id not in session.backend.ids:
        raise HTTPException(404, "Not available")
    try:
        session.backend.mark_paid(invoice_id)
    except PayPalError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}
