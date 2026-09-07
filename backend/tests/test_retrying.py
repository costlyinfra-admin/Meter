"""Retry/backoff on transient ingest failures."""

from __future__ import annotations

import httpx
import pytest
from meter.retrying import http_get_with_retry

_NO_SLEEP = lambda _d: None  # noqa: E731


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_retries_5xx_then_succeeds():
    calls = {"n": 0}

    def handler(_request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500, json={"err": "transient"})
        return httpx.Response(200, json={"ok": True})

    resp = http_get_with_retry(_client(handler), "https://x/y", sleep=_NO_SLEEP)
    assert resp.status_code == 200
    assert calls["n"] == 3


def test_retries_transport_error_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 2:
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200)

    resp = http_get_with_retry(_client(handler), "https://x/y", sleep=_NO_SLEEP)
    assert resp.status_code == 200


def test_returns_final_5xx_after_exhausting_retries():
    def handler(_request):
        return httpx.Response(503)

    resp = http_get_with_retry(_client(handler), "https://x/y", attempts=2, sleep=_NO_SLEEP)
    assert resp.status_code == 503  # caller maps this to its own error


def test_raises_after_persistent_transport_error():
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(httpx.TransportError):
        http_get_with_retry(_client(handler), "https://x/y", attempts=2, sleep=_NO_SLEEP)
