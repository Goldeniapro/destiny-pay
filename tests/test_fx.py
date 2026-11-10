import httpx

from app import fx


def _reset(monkeypatch):
    monkeypatch.setenv("DP_FX", "1")
    monkeypatch.setattr(fx, "_cache", {})
    monkeypatch.setattr(fx, "_fetching", False)


def test_reference_rate_is_fetched_and_cached(monkeypatch):
    _reset(monkeypatch)
    calls = []
    monkeypatch.setattr(fx.httpx, "get", lambda url, timeout: calls.append(url) or httpx.Response(
        200, json={"result": "success", "time_last_update_utc": "Tue, 10 Nov 2026 00:02:31 +0000", "rates": {"PEN": 3.7512}},
        request=httpx.Request("GET", url)))
    assert fx.pen_rate() == {}  # first call only starts the background fetch
    fx._fetch()
    assert fx.pen_rate()["pen"] == 3.7512 and fx.pen_rate()["source"] == "open.er-api.com"


def test_an_implausible_or_missing_rate_shows_nothing(monkeypatch):
    _reset(monkeypatch)
    monkeypatch.setattr(fx.httpx, "get", lambda url, timeout: httpx.Response(200, json={"rates": {"PEN": 375}}, request=httpx.Request("GET", url)))
    fx._fetch()
    assert fx.pen_rate() == {}
    monkeypatch.setattr(fx.httpx, "get", lambda url, timeout: (_ for _ in ()).throw(httpx.ConnectError("down")))
    fx._fetch()
    assert fx.pen_rate() == {}
