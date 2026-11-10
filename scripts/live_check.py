"""Check a running Destiny Pay end to end, the way a visitor uses it: app up, AI ready, and Destiny's own voice
actually speaking (a real phrase rendered by the voice service, not just a status flag).

Usage:  python scripts/live_check.py [BASE_URL] [--chat]
        BASE_URL defaults to the public demo. --chat also asks the assistant one question (costs a few AI tokens).

Standard library only, so a monitor can run it anywhere. Creates an empty business (no sample data, so no
PayPal calls). Prints one JSON line and exits 1 if anything a visitor would notice is broken.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

DEFAULT = "https://web-production-5eebcf.up.railway.app"


def call(base: str, path: str, body: dict | None = None, headers: dict | None = None, timeout: float = 40):
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "User-Agent": "destiny-pay-live-check",
                                          **(headers or {})})
    t = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
        return r.status, r.headers.get("Content-Type", ""), raw, round((time.monotonic() - t) * 1000)


def is_mp3(b: bytes) -> bool:
    return b[:3] == b"ID3" or (len(b) > 1 and b[0] == 0xFF and (b[1] & 0xE0) == 0xE0)


def check_speech(base: str, speech_id: str) -> dict:
    _, ctype, audio, ms = call(base, "/api/speech/" + speech_id, timeout=60)
    ok = ctype.startswith("audio/mpeg") and len(audio) > 2000 and is_mp3(audio)
    return {"ok": ok, "bytes": len(audio), "ms": ms}


def main(argv: list[str]) -> int:
    base = next((a for a in argv if a.startswith("http")), DEFAULT).rstrip("/")
    out: dict = {"base": base, "problems": []}
    try:
        _, _, raw, ms = call(base, "/api/status")
        st = json.loads(raw)
        out["status"] = {"build": st.get("build"), "ai_ready": st.get("ai_ready"), "voice": st.get("voice"), "ms": ms}
        if not st.get("ai_ready"):
            out["problems"].append("AI not configured")
        if not (st.get("voice") or {}).get("tts"):
            out["problems"].append("Destiny's voice is not configured: visitors would hear the browser's system voice")
        elif (st.get("voice") or {}).get("ok") is False:
            out["problems"].append("the app's own voice watchdog reports the voice failing")

        _, _, raw, _ = call(base, "/api/workspace", {"lang": "es", "sample": False})
        ws = json.loads(raw)["workspace_id"]
        _, _, raw, _ = call(base, "/api/briefing?lang=es", headers={"x-workspace": ws})
        sid = json.loads(raw).get("speech_id")
        out["speech"] = check_speech(base, sid) if sid else {"ok": False, "bytes": 0}
        if not out["speech"]["ok"]:
            out["problems"].append("a spoken phrase did not come back as audio")

        if "--chat" in argv:
            _, _, raw, ms = call(base, "/api/chat", {"workspace_id": ws, "conversation_id": "live-" + uuid.uuid4().hex[:12],
                                                    "message": "¿Quién me debe?", "lang": "es"}, timeout=90)
            res = json.loads(raw)
            reply = (res.get("reply") or "").strip()
            out["chat"] = {"ms": ms, "reply": reply[:120]}
            if not reply or "couldn't reach" in reply.lower() or "no pude conectarme" in reply.lower():
                out["problems"].append("the assistant did not answer")
            elif res.get("speech_id"):
                out["chat"]["speech"] = check_speech(base, res["speech_id"])
                if not out["chat"]["speech"]["ok"]:
                    out["problems"].append("the assistant's reply did not come back as audio")
    except urllib.error.HTTPError as e:
        out["problems"].append(f"HTTP {e.code} on {e.url}")
    except Exception as e:  # network down, timeout, bad JSON
        out["problems"].append(f"{type(e).__name__}: {e}")
    out["ok"] = not out["problems"]
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
