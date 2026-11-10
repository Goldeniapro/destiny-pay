"""Destiny Pay's own voice (Fish Audio): a native voice per language, plus server speech-to-text.

A voice trained in one language sounds foreign in another, so each language can have its own voice
(FISH_VOICE_ID_ES, FISH_VOICE_ID_EN, FISH_VOICE_ID_PT); FISH_VOICE_ID is the fallback for any of them.

Only texts the server itself produced can be spoken (replies, alerts, briefings), so the endpoint
can't be used as a free text-to-speech service. Both features are optional: without FISH_API_KEY
the browser's own voice and dictation are used.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import threading
import time
from collections import OrderedDict
from typing import Iterator

import httpx

FISH_URL = "https://api.fish.audio"
SPEECH_LANG = {"es": "es", "en": "en", "pt": "pt"}


AUDIO_CACHE_BYTES = 40 * 1024 * 1024  # repeated phrases only; keeps memory well under the instance limit


class VoiceError(RuntimeError):
    pass


class Voice:
    def __init__(self, api_key: str | None = None, voice_id: str | None = None, model: str | None = None,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.api_key = api_key if api_key is not None else os.environ.get("FISH_API_KEY", "")
        self.voice_id = voice_id if voice_id is not None else os.environ.get("FISH_VOICE_ID", "")
        self.voices = {lang: os.environ.get(f"FISH_VOICE_ID_{lang.upper()}", "") for lang in SPEECH_LANG} if voice_id is None else {}
        self.model = model or os.environ.get("FISH_MODEL", "s2.1-pro")
        self.asr_model = os.environ.get("FISH_ASR_MODEL", "transcribe-1-pro")
        self.daily_bytes = int(os.environ.get("DP_TTS_DAILY_BYTES", "2000000"))  # ~US$30/day at US$15 per million bytes
        self.daily_audio = int(os.environ.get("DP_STT_DAILY_BYTES", "300000000"))  # uploaded audio per day for transcription
        self._audio_day, self._audio_used = time.strftime("%Y-%m-%d"), 0
        self._http = httpx.Client(base_url=FISH_URL, timeout=httpx.Timeout(30, connect=6), transport=transport)
        self.failures = 0
        self.health: dict = {}  # last health check per language: ok, ms, at, error
        self._texts: OrderedDict[str, tuple[str, str, float]] = OrderedDict()  # speech id -> (text, lang, created)
        self._audio: OrderedDict[str, bytes] = OrderedDict()  # sha(text, voice) -> mp3, for repeated phrases
        self._lock = threading.Lock()
        self._day, self._used = time.strftime("%Y-%m-%d"), 0

    def voice_for(self, lang: str) -> str:
        return self.voices.get(lang) or self.voice_id

    @property
    def tts_enabled(self) -> bool:
        return bool(self.api_key and (self.voice_id or any(self.voices.values())))

    @property
    def stt_enabled(self) -> bool:
        return bool(self.api_key)

    # --- text-to-speech ---------------------------------------------------------------
    def register(self, text: str, lang: str) -> str | None:
        """Allow the server's own text to be spoken; returns the id the page uses to fetch the audio."""
        text = (text or "").strip()
        if not self.api_key or not text or not self.voice_for(lang):
            return None  # no voice for this language: the browser speaks it
        sid = secrets.token_urlsafe(12)
        with self._lock:
            self._texts[sid] = (text[:800], lang, time.time())
            while len(self._texts) > 3000:
                self._texts.popitem(last=False)
        return sid

    def _take(self, sid: str) -> tuple[str, str]:
        with self._lock:
            item = self._texts.get(sid)
            if item is None or time.time() - item[2] > 900:
                raise VoiceError("unknown or expired speech id")
            return item[0], item[1]

    def _charge(self, n: int) -> None:
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._day:
                self._day, self._used = today, 0
            if self._used + n > self.daily_bytes:
                raise VoiceError("daily voice budget reached")
            self._used += n

    def stream(self, sid: str) -> Iterator[bytes]:
        """Yield mp3 bytes as Fish Audio produces them (raises VoiceError before the first byte on failure)."""
        text, lang = self._take(sid)
        voice_id = self.voice_for(lang)
        text = spoken(text, lang)
        key = hashlib.sha256(f"{voice_id}|{self.model}|{text}".encode()).hexdigest()
        with self._lock:
            cached = self._audio.get(key)
            if cached is not None:
                self._audio.move_to_end(key)
        if cached is not None:
            return iter([cached])
        self._charge(len(text.encode("utf-8")))
        body = {"text": text, "reference_id": voice_id, "format": "mp3", "mp3_bitrate": 128,
                "latency": "balanced", "chunk_length": 200, "normalize": True, "language": SPEECH_LANG.get(lang, "en")}
        resp = self._open(body)
        return self._relay(resp, key, True)  # replaying an id never pays twice

    def _open(self, body: dict, attempts: int = 3) -> httpx.Response:
        """Start a Fish Audio stream; a hiccup (network, 429, 5xx) is retried before giving up, so the
        page doesn't lose Destiny's voice over one slow answer."""
        last = ""
        for i in range(attempts):
            req = self._http.build_request("POST", "/v1/tts", json=body,
                                           headers={"Authorization": f"Bearer {self.api_key}", "model": self.model})
            try:
                resp = self._http.send(req, stream=True)
            except httpx.HTTPError as e:
                last = f"voice service unreachable: {e.__class__.__name__}"
            else:
                if resp.status_code == 200:
                    return resp
                detail = resp.read()[:200]
                resp.close()
                last = f"voice service error {resp.status_code}: {detail!r}"
                if resp.status_code not in (408, 409, 425, 429) and resp.status_code < 500:
                    break  # a request error won't fix itself
            if i + 1 < attempts:
                time.sleep(0.4 * (i + 1))
        self.failures += 1
        raise VoiceError(last)

    def probe(self, lang: str = "es") -> float:
        """Health check: time to the first audio byte for a short phrase in Destiny's voice (milliseconds)."""
        voice_id = self.voice_for(lang)
        if not self.api_key or not voice_id:
            raise VoiceError("voice not configured")
        t0 = time.time()
        body = {"text": {"es": "Listo.", "en": "Done.", "pt": "Pronto."}.get(lang, "Done."), "reference_id": voice_id, "format": "mp3",
                "mp3_bitrate": 64, "latency": "balanced", "normalize": True, "language": SPEECH_LANG.get(lang, "en")}
        self._charge(8)
        resp = self._open(body, attempts=2)
        try:
            for chunk in resp.iter_bytes():
                if chunk:
                    break
            else:
                raise VoiceError("voice service returned no audio")
        finally:
            resp.close()
        return round((time.time() - t0) * 1000)

    def _relay(self, resp: httpx.Response, key: str, cache: bool) -> Iterator[bytes]:
        chunks: list[bytes] = []
        try:
            for chunk in resp.iter_bytes():
                if chunk:
                    if cache:
                        chunks.append(chunk)
                    yield chunk
        finally:
            resp.close()
        if cache and chunks:
            with self._lock:
                self._audio[key] = b"".join(chunks)
                while len(self._audio) > 400 or sum(map(len, self._audio.values())) > AUDIO_CACHE_BYTES:
                    self._audio.popitem(last=False)  # bounded by count and by size

    # --- speech-to-text ---------------------------------------------------------------
    def transcribe(self, audio: bytes, filename: str, mime: str, lang: str) -> str:
        if not self.stt_enabled:
            raise VoiceError("speech-to-text is not configured")
        if not audio or len(audio) > 8 * 1024 * 1024:
            raise VoiceError("audio is empty or too large")
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            if today != self._audio_day:
                self._audio_day, self._audio_used = today, 0
            if self._audio_used + len(audio) > self.daily_audio:
                raise VoiceError("daily transcription budget reached")
            self._audio_used += len(audio)
        try:
            r = self._http.post("/v1/asr", headers={"Authorization": f"Bearer {self.api_key}", "model": self.asr_model},
                                files={"audio": (filename or "speech.webm", audio, mime or "audio/webm")},
                                data={"language": SPEECH_LANG.get(lang, "en"), "ignore_timestamps": "true"},
                                timeout=httpx.Timeout(90, connect=10))
        except httpx.HTTPError as e:
            raise VoiceError(f"speech service unreachable: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise VoiceError(f"speech service error {r.status_code}: {r.text[:200]}")
        return clean_transcript(str(r.json().get("text", "")))


CURRENCY_WORDS = {"USD": {"es": "dólares", "en": "dollars", "pt": "dólares"}, "PEN": {"es": "soles", "en": "soles", "pt": "soles"},
                  "EUR": {"es": "euros", "en": "euros", "pt": "euros"}}
_AMOUNT = re.compile(r"(\d{1,3}(?:[.,\u00a0 ]\d{3})*|\d+)(?:[.,](\d{2}))?\s?(USD|PEN|EUR)\b")


def spoken(text: str, lang: str) -> str:
    """How an amount should sound, not how it reads: "1.900,00 USD" -> "1.900 dólares" (the screen keeps the exact text)."""
    def say(m: re.Match) -> str:
        whole, cents, cur = m.group(1), m.group(2), m.group(3)
        word = CURRENCY_WORDS[cur].get(lang, CURRENCY_WORDS[cur]["en"])
        sep = "," if lang in ("es", "pt") else "."
        return f"{whole}{sep}{cents} {word}" if cents and cents != "00" else f"{whole} {word}"
    return _AMOUNT.sub(say, text)


def clean_transcript(text: str) -> str:
    """Fish Audio's transcription adds markup such as <|speaker:0|> and emotion tags like [excited]; the agent must
    only see what the owner said."""
    text = re.sub(r"<\|[^|>]*\|>", " ", text)
    text = re.sub(r"\[[^\]]{1,40}\]", " ", text)
    return " ".join(text.split())
