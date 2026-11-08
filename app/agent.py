"""Destiny Pay agent: Claude + PayPal Invoicing tools, Spanish voice-first cashier."""

from __future__ import annotations

import json
from typing import Any, Protocol

import anthropic

from .paypal import PayPalError, summarize

MODEL = "claude-opus-5-5"

SYSTEM = """Eres Destiny Pay, una cajera por voz para pequeños negocios de Latinoamérica.
El dueño del negocio te habla en español y tú gestionas sus cobros con PayPal usando las herramientas.

Reglas:
- Responde en español, en 1 a 3 frases cortas y naturales: tu respuesta se lee en voz alta.
- Los montos van en dólares (USD): PayPal no admite soles.
- Para cobrar necesitas: nombre del cliente, correo del cliente y qué se vende (producto, cantidad, precio unitario).
  Si falta algún dato, pregúntalo. Nunca inventes un correo.
- Antes de enviar un cobro nuevo, repite en una frase el cliente, el total y el correo, y envíalo solo si el dueño ya lo confirmó
  en este turno o en el anterior.
- Para "¿quién me debe?" usa list_invoices y resume los pendientes (estado SENT, UNPAID o PARTIALLY_PAID) con nombre y monto.
- Para recordar o cancelar, identifica la factura con list_invoices si no tienes su id.
- Si una herramienta falla, explica el problema en palabras simples.
"""


class InvoiceBackend(Protocol):
    def create_invoice(self, recipient_name: str, recipient_email: str, items: list[dict], note: str = "") -> dict: ...
    def send_invoice(self, invoice_id: str) -> dict: ...
    def get_invoice(self, invoice_id: str) -> dict: ...
    def list_invoices(self, page_size: int = 20) -> list[dict]: ...
    def remind_invoice(self, invoice_id: str, note: str = "") -> dict: ...
    def cancel_invoice(self, invoice_id: str, note: str = "") -> dict: ...


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "create_and_send_invoice",
        "description": "Crea una factura de PayPal para un cliente y se la envía por correo con su link de pago. "
        "Úsala solo cuando el dueño confirmó cliente, correo y productos.",
        "strict": True,
        "input_schema": _schema(
            {
                "recipient_name": {"type": "string", "description": "Nombre y apellido del cliente"},
                "recipient_email": {"type": "string", "description": "Correo del cliente, dicho por el dueño"},
                "items": {
                    "type": "array",
                    "items": _schema(
                        {
                            "name": {"type": "string"},
                            "quantity": {"type": "number"},
                            "unit_price": {"type": "number", "description": "Precio unitario en USD"},
                        },
                        ["name", "quantity", "unit_price"],
                    ),
                },
                "note": {"type": "string", "description": "Nota para el cliente; cadena vacía si no hay"},
            },
            ["recipient_name", "recipient_email", "items", "note"],
        ),
    },
    {
        "name": "list_invoices",
        "description": "Lista las facturas recientes con su estado (DRAFT, SENT, PAID, CANCELLED, etc.), cliente y monto.",
        "strict": True,
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_invoice",
        "description": "Consulta el estado actual de una factura por su id (por ejemplo, para saber si ya se pagó).",
        "strict": True,
        "input_schema": _schema({"invoice_id": {"type": "string"}}, ["invoice_id"]),
    },
    {
        "name": "remind_invoice",
        "description": "Envía al cliente un recordatorio de pago de una factura pendiente.",
        "strict": True,
        "input_schema": _schema({"invoice_id": {"type": "string"}, "note": {"type": "string"}}, ["invoice_id", "note"]),
    },
    {
        "name": "cancel_invoice",
        "description": "Cancela una factura enviada que todavía no se pagó.",
        "strict": True,
        "input_schema": _schema({"invoice_id": {"type": "string"}, "note": {"type": "string"}}, ["invoice_id", "note"]),
    },
]


def run_tool(backend: InvoiceBackend, name: str, args: dict) -> str:
    """Execute one tool call and return a JSON string for the model."""
    if name == "create_and_send_invoice":
        draft = backend.create_invoice(args["recipient_name"], args["recipient_email"], args["items"], args.get("note", ""))
        backend.send_invoice(draft["id"])
        return json.dumps(summarize(backend.get_invoice(draft["id"])), ensure_ascii=False)
    if name == "list_invoices":
        return json.dumps([summarize(i) for i in backend.list_invoices()], ensure_ascii=False)
    if name == "get_invoice":
        return json.dumps(summarize(backend.get_invoice(args["invoice_id"])), ensure_ascii=False)
    if name == "remind_invoice":
        backend.remind_invoice(args["invoice_id"], args.get("note", ""))
        return json.dumps({"ok": True, "invoice_id": args["invoice_id"], "action": "reminder_sent"})
    if name == "cancel_invoice":
        backend.cancel_invoice(args["invoice_id"], args.get("note", ""))
        return json.dumps({"ok": True, "invoice_id": args["invoice_id"], "action": "cancelled"})
    raise ValueError(f"Unknown tool {name}")


class DestinyPayAgent:
    """Keeps one append-only conversation per session and runs the tool loop."""

    def __init__(self, backend: InvoiceBackend, client: anthropic.Anthropic | None = None, max_steps: int = 8):
        self.backend = backend
        self.client = client or anthropic.Anthropic()
        self.max_steps = max_steps
        self.sessions: dict[str, list[dict]] = {}

    def chat(self, session_id: str, user_text: str) -> dict:
        messages = self.sessions.setdefault(session_id, [])
        if messages and messages[-1]["role"] == "user":
            # previous turn ended on tool results (step limit): keep roles alternating
            messages[-1]["content"].append({"type": "text", "text": user_text})
        else:
            messages.append({"role": "user", "content": user_text})
        actions: list[dict] = []

        for _ in range(self.max_steps):
            response = self.client.beta.messages.create(
                model=MODEL,
                max_tokens=16000,
                system=SYSTEM,
                tools=TOOLS,
                messages=messages,
                output_config={"effort": "medium"},
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "refusal":
                return {"reply": "Lo siento, no puedo ayudar con ese pedido.", "actions": actions}
            if response.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason != "tool_use" or not tool_uses:
                text = " ".join(b.text for b in response.content if b.type == "text").strip()
                return {"reply": text or "Listo.", "actions": actions}

            results = []
            for tu in tool_uses:
                try:
                    out = run_tool(self.backend, tu.name, dict(tu.input))
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
                    actions.append({"tool": tu.name, "input": dict(tu.input), "result": json.loads(out)})
                except (PayPalError, KeyError, ValueError) as e:
                    results.append({"type": "tool_result", "tool_use_id": tu.id, "content": f"Error: {e}", "is_error": True})
                    actions.append({"tool": tu.name, "input": dict(tu.input), "error": str(e)})
            messages.append({"role": "user", "content": results})

        return {"reply": "Me tomó demasiados pasos; intenta pedirlo de nuevo en partes.", "actions": actions}
