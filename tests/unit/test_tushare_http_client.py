"""Unit tests for the official-endpoint Tushare HTTP client."""

from __future__ import annotations

from typing import Any

import pytest

from etf_quant_lab.data.providers.tushare_http import HttpTushareClient


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


def test_success_builds_dataframe(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        return _FakeResponse(
            {
                "code": 0,
                "msg": "",
                "data": {
                    "fields": ["cal_date", "is_open"],
                    "items": [["20260714", 1], ["20260715", 1]],
                },
            }
        )

    monkeypatch.setattr("etf_quant_lab.data.providers.tushare_http.requests.post", fake_post)
    client = HttpTushareClient("token-value")

    frame = client.trade_cal(
        exchange="SSE",
        start_date="20260714",
        end_date="20260715",
        fields="cal_date,is_open",
    )

    assert captured["url"] == "https://api.tushare.pro"
    assert captured["json"]["api_name"] == "trade_cal"
    assert list(frame.columns) == ["cal_date", "is_open"]
    assert len(frame) == 2


def test_non_zero_code_raises_server_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_post(url: str, *, json: dict[str, Any], timeout: float) -> _FakeResponse:
        return _FakeResponse({"code": 40203, "msg": "您没有接口(fund_daily)访问权限"})

    monkeypatch.setattr("etf_quant_lab.data.providers.tushare_http.requests.post", fake_post)
    client = HttpTushareClient("token-value")

    with pytest.raises(RuntimeError, match="访问权限"):
        client.fund_daily(
            ts_code="510300.SH",
            start_date="20260701",
            end_date="20260715",
            fields="ts_code,trade_date,close",
        )


def test_blank_token_rejected() -> None:
    with pytest.raises(ValueError, match="blank"):
        HttpTushareClient("   ")
