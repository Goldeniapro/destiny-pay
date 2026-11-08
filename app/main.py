"""FastAPI server for Destiny Pay."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .agent import DestinyPayAgent
from .fake import FakePayPal
from .paypal import PayPalClient, PayPalError, summarize

load_dotenv()
STATIC = Path(__file__).resolve().parent.parent / "static"

DEMO_MODE = not (os.environ.get("PAYPAL_CLIENT_ID") and os.environ.get("PAYPAL_CLIENT_SECRET"))
backend = FakePayPal() if DEMO_MODE else PayPalClient.from_env()
agent = DestinyPayAgent(backend)

app = FastAPI(title="Destiny Pay")


class ChatIn(BaseModel):
    session_id: str
    message: str


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/status")
def status():
    return {"demo_mode": DEMO_MODE, "paypal_env": "demo" if DEMO_MODE else os.environ.get("PAYPAL_ENV", "sandbox")}


@app.post("/api/chat")
def chat(body: ChatIn):
    if not body.message.strip():
        raise HTTPException(400, "Empty message")
    return agent.chat(body.session_id, body.message.strip()[:2000])


@app.get("/api/invoices")
def invoices():
    try:
        return [summarize(i) for i in backend.list_invoices()]
    except PayPalError as e:
        raise HTTPException(502, str(e))
