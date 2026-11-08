import httpx
import pytest

from app.i18n import LANGS, T, classify_reply
from app.voice import Voice, VoiceError


def test_every_text_exists_in_every_language():
    for key, texts in T.items():
        assert set(texts) == set(LANGS), key


@pytest.mark.parametrize("reply, verdict", [
    ("sí", "yes"), ("Sí, envíala", "yes"), ("dale", "yes"), ("bueno, sí, hazlo", "yes"), ("claro que sí", "yes"),
    ("no", "no"), ("mejor no", "no"), ("todavía no", "no"), ("no, espera", "no"),
    ("sí, pero cambia el monto", "other"), ("sí a 40 dólares", "other"), ("sí, mándala mañana", "other"), ("¿quién me debe?", "other"),
    ("yes send it", "yes"), ("go ahead", "yes"), ("wait", "no"), ("don't send it", "no"), ("yes but change the email", "other"),
    ("sim, pode enviar", "yes"), ("ainda não", "no"), ("oui", "yes"), ("d'accord", "yes"), ("non", "no"), ("pas encore", "no"),
    ("好的", "yes"), ("是的，发送", "yes"), ("不要", "no"), ("不是", "no"), ("改成五十", "other"),
])
def test_yes_no_classifier(reply, verdict):
    assert classify_reply(reply) == verdict


def test_voice_disabled_without_keys():
    v = Voice(api_key="", voice_id="")
    assert not v.tts_enabled and v.register("hola", "es") is None
    with pytest.raises(VoiceError):
        v.transcribe(b"x", "a.webm", "audio/webm", "es")


def test_voice_caches_repeated_phrases_and_budget():
    calls = []

    def fish(req):
        calls.append(req)
        return httpx.Response(200, content=b"mp3")

    v = Voice(api_key="k", voice_id="v", transport=httpx.MockTransport(fish))
    a, b = v.register("Listo.", "es"), v.register("Listo.", "es")
    assert b"".join(v.stream(a)) == b"mp3" and b"".join(v.stream(b)) == b"mp3" and len(calls) == 1
    v.daily_bytes = 5
    with pytest.raises(VoiceError, match="budget"):
        v.stream(v.register("Una frase más larga", "es"))


def test_voice_error_before_first_byte():
    v = Voice(api_key="k", voice_id="v", transport=httpx.MockTransport(lambda r: httpx.Response(402, text="no credit")))
    with pytest.raises(VoiceError, match="402"):
        v.stream(v.register("Hola", "es"))


def test_transcribe_sends_pro_model_and_language():
    def fish(req):
        assert req.url.path == "/v1/asr" and req.headers["model"] == "transcribe-1-pro"
        assert b'name="language"' in req.content and b"pt" in req.content
        return httpx.Response(200, json={"text": " Cobre a Maria ", "duration": 1.2})

    v = Voice(api_key="k", voice_id="", transport=httpx.MockTransport(fish))
    assert v.transcribe(b"webm-bytes", "speech.webm", "audio/webm", "pt") == "Cobre a Maria"
