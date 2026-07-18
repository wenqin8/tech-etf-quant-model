"""Unit tests for the proxy-retry network policy on outbound clients."""

from __future__ import annotations

import os
from typing import Any

import pandas as pd
import pytest
import requests

from etf_quant_lab.data.providers.akshare import (
    _call_with_proxy_fallback,
    _looks_like_network_error,
)
from etf_quant_lab.data.providers.tushare_http import HttpTushareClient


def test_network_failure_retries_once_with_proxy_disabled() -> None:
    attempts: list[str | None] = []

    def flaky() -> pd.DataFrame:
        attempts.append(os.environ.get("NO_PROXY"))
        if len(attempts) == 1:
            raise requests.exceptions.ProxyError("Unable to connect to proxy")
        return pd.DataFrame({"ok": [1]})

    frame = _call_with_proxy_fallback(flaky)

    # First attempt: ambient config; second attempt: proxy bypassed.
    assert attempts[0] != "*"
    assert attempts[1] == "*"
    assert len(frame) == 1
    # The environment is restored after the call.
    assert os.environ.get("NO_PROXY") != "*"


def test_both_attempts_failing_raises_second_error() -> None:
    def always_down() -> pd.DataFrame:
        raise requests.exceptions.ConnectionError("Remote end closed connection")

    with pytest.raises(requests.exceptions.ConnectionError):
        _call_with_proxy_fallback(always_down)


def test_non_network_error_is_not_retried() -> None:
    attempts: list[int] = []

    def broken() -> pd.DataFrame:
        attempts.append(1)
        raise KeyError("schema drift")

    with pytest.raises(KeyError):
        _call_with_proxy_fallback(broken)

    assert len(attempts) == 1


def test_network_error_detection_by_name_and_message() -> None:
    assert _looks_like_network_error(requests.exceptions.ProxyError("x"))
    assert _looks_like_network_error(TimeoutError("x"))
    assert _looks_like_network_error(RuntimeError("Connection aborted"))
    assert not _looks_like_network_error(ValueError("bad parameter"))


def test_tushare_http_retries_direct_after_proxy_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any] | None] = []

    class _Response:
        status_code = 200

        @staticmethod
        def raise_for_status() -> None:
            return None

        @staticmethod
        def json() -> dict[str, Any]:
            return {
                "code": 0,
                "data": {"fields": ["cal_date"], "items": [["20260715"]]},
            }

    def fake_post(
        url: str,
        *,
        json: dict[str, Any],
        timeout: float,
        proxies: dict[str, str] | None = None,
    ) -> _Response:
        calls.append(proxies)
        if len(calls) == 1:
            raise requests.exceptions.ProxyError("Unable to connect to proxy")
        return _Response()

    monkeypatch.setattr(
        "etf_quant_lab.data.providers.tushare_http.requests.post", fake_post
    )
    client = HttpTushareClient("token-value")

    frame = client.trade_cal(
        exchange="SSE",
        start_date="20260715",
        end_date="20260715",
        fields="cal_date",
    )

    # First call: ambient proxies (None); second call: explicit direct.
    assert calls[0] is None
    assert calls[1] == {"http": "", "https": ""}
    assert len(frame) == 1
