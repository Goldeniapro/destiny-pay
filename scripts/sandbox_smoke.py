"""Check the real PayPal sandbox end to end with your .env credentials.

Usage:  python scripts/sandbox_smoke.py customer-sandbox-email@personal.example.com
Use the email of one of YOUR sandbox *personal* accounts (developer.paypal.com > Testing Tools > Sandbox Accounts).
It creates two small test invoices in the sandbox: no real money moves.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.paypal import SANDBOX_URL, PayPalClient, PayPalError, summarize  # noqa: E402

ok = True


def require(condition, message):
    if not condition:
        raise PayPalError(message)
    return condition


def step(name, fn):
    global ok
    try:
        out = fn()
        print(f"PASS  {name}")
        return out
    except Exception as e:  # report every step, keep going
        ok = False
        print(f"FAIL  {name}: {e}")
        return None


def main() -> None:
    load_dotenv()
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        sys.exit(__doc__)
    email = sys.argv[1]
    pp = PayPalClient.from_env()
    if pp.base_url != SANDBOX_URL:
        sys.exit("Refusing to run: this check only runs against the PayPal sandbox.")

    step("OAuth token", lambda: pp._token_header())
    items = [{"name": "Destiny Pay test item", "quantity": 2, "unit_price": 7.5}]
    inv = step("create draft invoice", lambda: pp.create_invoice("Test Customer", email, items, "Sandbox test", reference="dp-smoke"))
    if not inv:
        sys.exit(1)
    inv_id = inv["id"]
    draft = summarize(pp.get_invoice(inv_id))
    print("      ", json.dumps(draft))
    assert draft["status"] == "DRAFT", draft

    step("send invoice", lambda: pp.send_invoice(inv_id))
    raw = pp.get_invoice(inv_id)
    sent = summarize(raw)
    print("      ", json.dumps(sent))
    print("       metadata keys:", sorted(raw.get("detail", {}).get("metadata", {}).keys()))
    print("       link rels:", [l.get("rel") for l in raw.get("links", [])])
    step("payer link present after send", lambda: require(sent["payer_link"], "no payer link found"))
    step("reference kept", lambda: require(sent["reference"] == "dp-smoke", f"reference={sent['reference']}"))

    qr = step("generate QR", lambda: pp.qr_code(inv_id))
    if qr:
        png = base64.b64decode(qr)
        Path("sandbox_qr.png").write_bytes(png)
        is_png = png[:4] == b"\x89PNG"
        print(f"       QR saved to sandbox_qr.png ({len(png)} bytes, PNG header ok: {is_png})")

    step("record partial cash payment", lambda: pp.record_payment(inv_id, 5, "CASH", "test"))
    print("      ", summarize(pp.get_invoice(inv_id))["status"], "(expected PARTIALLY_PAID)")
    step("record remaining payment", lambda: pp.record_payment(inv_id, 10, "OTHER", "Yape test"))
    print("      ", summarize(pp.get_invoice(inv_id))["status"], "(expected MARKED_AS_PAID)")

    inv2 = step("create second invoice", lambda: pp.create_invoice("Test Customer", email, items, reference="dp-smoke"))
    if inv2:
        step("send second invoice", lambda: pp.send_invoice(inv2["id"]))
        step("remind second invoice", lambda: pp.remind_invoice(inv2["id"], "Friendly reminder"))
        step("cancel second invoice", lambda: pp.cancel_invoice(inv2["id"], "Test cancel"))
        print("      ", summarize(pp.get_invoice(inv2["id"]))["status"], "(expected CANCELLED)")

    supplier = os.environ.get("DP_SMOKE_PAYOUT_EMAIL", email)
    batch = step(f"payout 1.00 to {supplier} (PayPal Payouts)", lambda: pp.create_payout(f"dp-smoke-{int(time.time())}", supplier, 1.00, note="Destiny Pay test"))
    if batch:
        print("      ", batch.get("batch_header", {}))
        print("       If this failed with an authorization error, enable Payouts in your sandbox app's features.")

    print("\nALL GOOD" if ok else "\nSOME STEPS FAILED: send this output (it contains no secrets) to fix them.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
