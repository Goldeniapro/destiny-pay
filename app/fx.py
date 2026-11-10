"""Reference USD -> PEN rate for showing a soles equivalent next to dollar amounts (never used to charge).

Fetched in the background from a public source and cached; if it can't be fetched, nothing is shown:
the app never invents a rate."""
from __future__ import annotations

import logging
import os
import threading
import time

import httpx

log = logging.getLogger("destiny.fx")
SOURCE_URL = os.environ.get("DP_FX_URL", "https://open.er-api.com/v6/latest/USD")
TTL = 6 * 3600
_lock = threading.Lock()
_cache: dict = {}
_fetching = False


def _fetch() -> None:
    global _fetching
    try:
        r = httpx.get(SOURCE_URL, timeout=6)
        r.raise_for_status()
        data = r.json()
        rate = float((data.get("rates") or {}).get("PEN") or 0)
        if not 2.0 < rate < 8.0:  # sanity bounds for USD/PEN: a broken feed shows nothing rather than a wrong number
            raise ValueError(f"implausible PEN rate {rate}")
        with _lock:
            _cache.update({"pen": round(rate, 4), "at": time.time(), "source": "open.er-api.com",
                           "updated": str(data.get("time_last_update_utc") or "")[:40]})
    except Exception as e:  # noqa: BLE001 - a missing reference rate must never break the app
        log.warning("fx fetch failed: %s", str(e)[:160])
        with _lock:
            _cache["failed_at"] = time.time()
    finally:
        with _lock:
            _fetching = False


def pen_rate() -> dict:
    """The cached reference rate, or {} while unknown. Starts a refresh in the background when stale."""
    global _fetching
    if os.environ.get("DP_FX", "1") == "0":
        return {}
    now = time.time()
    with _lock:
        fresh = _cache.get("pen") and now - _cache.get("at", 0) < TTL
        recently_failed = now - _cache.get("failed_at", 0) < 600
        if not fresh and not _fetching and not recently_failed:
            _fetching = True
            threading.Thread(target=_fetch, daemon=True, name="fx").start()
        if _cache.get("pen") and now - _cache.get("at", 0) < 2 * 86400:  # a day-old reference is still a fair reference
            return {"pen": _cache["pen"], "source": _cache["source"], "updated": _cache["updated"]}
    return {}
