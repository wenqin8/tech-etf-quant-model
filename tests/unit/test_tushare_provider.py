"""Unit tests for the network-free Tushare provider adapter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from etf_quant_lab.contracts import (
    DailyBarsQuery,
    DomainError,
    TradeCalendarQuery,
)
from etf_quant_lab.contracts.enums import DataSource, Exchange, PriceAdjustment
from etf_quant_lab.data.providers.tushare import TushareProvider
from etf_quant_lab.ids import FixedIdGenerator

BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5N12"
FETCHED_AT = datetime(2026, 7, 14, 9, 30, tzinfo=UTC)


class FakeTushareClient:
    def __init__(
        self,
        *,
        fund_daily_responses: list[pd.DataFrame | Exception] | None = None,
        trade_cal_responses: list[pd.DataFrame | Exception] | None = None,
    ) -> None:
        self.fund_daily_responses = list(fund_daily_responses or [])
        self.trade_cal_responses = list(trade_cal_responses or [])
        self.fund_daily_calls: list[dict[str, str]] = []
        self.trade_cal_calls: list[dict[str, str]] = []

    def fund_daily(
        self,
        *,
        ts_code: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame:
        self.fund_daily_calls.append(
            {
                "ts_code": ts_code,
                "start_date": start_date,
                "end_date": end_date,
                "fields": fields,
            }
        )
        response = self.fund_daily_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def trade_cal(
        self,
        *,
        exchange: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame:
        self.trade_cal_calls.append(
            {
                "exchange": exchange,
                "start_date": start_date,
                "end_date": end_date,
                "fields": fields,
            }
        )
        response = self.trade_cal_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _provider(
    client: FakeTushareClient,
    *,
    max_retries: int = 0,
    sleeper: Callable[[float], None] = lambda _delay: None,
) -> TushareProvider:
    return TushareProvider(
        client,
        FixedIdGenerator([BATCH_ID]),
        lambda: FETCHED_AT,
        max_retries=max_retries,
        retry_backoff_seconds=0.25,
        sleeper=sleeper,
    )


def _fund_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ts_code": "510300.SH",
                "trade_date": "20260713",
                "open": 4.123,
                "high": 4.201,
                "low": 4.101,
                "close": 4.188,
                "pre_close": 4.120,
                "vol": 123456.0,
                "amount": 512345.6,
            }
        ]
    )


def test_fetch_daily_bars_maps_fields_and_request_metadata() -> None:
    client = FakeTushareClient(fund_daily_responses=[_fund_daily_frame()])
    query = DailyBarsQuery(
        symbols=("510300.SH",),
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 13),
    )

    batch = _provider(client).fetch_daily_bars(query)

    assert batch.batch_id == BATCH_ID
    assert batch.source is DataSource.TUSHARE
    assert batch.dataset == "fund_daily"
    assert batch.fetched_at == FETCHED_AT
    assert batch.records == (
        {
            "symbol": "510300.SH",
            "trade_date": "20260713",
            "open": 4.123,
            "high": 4.201,
            "low": 4.101,
            "close": 4.188,
            "pre_close": 4.120,
            "volume": 123456.0,
            "amount": 512345.6,
        },
    )
    assert batch.request_metadata == {
        "symbols": ("510300.SH",),
        "start_date": "2026-07-01",
        "end_date": "2026-07-13",
        "adjustment": "RAW",
        "request_count": 1,
    }
    assert client.fund_daily_calls == [
        {
            "ts_code": "510300.SH",
            "start_date": "20260701",
            "end_date": "20260713",
            "fields": "ts_code,trade_date,open,high,low,close,pre_close,vol,amount",
        }
    ]


def test_fetch_trade_calendar_maps_provider_fields() -> None:
    frame = pd.DataFrame(
        [
            {
                "exchange": "SSE",
                "cal_date": "20260713",
                "is_open": 1,
                "pretrade_date": "20260710",
            },
            {
                "exchange": "SSE",
                "cal_date": "20260714",
                "is_open": 0,
                "pretrade_date": "20260713",
            },
        ]
    )
    client = FakeTushareClient(trade_cal_responses=[frame])

    batch = _provider(client).fetch_trade_calendar(
        TradeCalendarQuery(
            exchange=Exchange.SSE,
            start_date=date(2026, 7, 13),
            end_date=date(2026, 7, 14),
        )
    )

    assert batch.dataset == "trade_cal"
    assert batch.records == (
        {
            "exchange": "SSE",
            "cal_date": "20260713",
            "is_open": 1,
            "previous_open_date": "20260710",
        },
        {
            "exchange": "SSE",
            "cal_date": "20260714",
            "is_open": 0,
            "previous_open_date": "20260713",
        },
    )
    assert client.trade_cal_calls[0]["exchange"] == "SSE"
    assert batch.request_metadata["request_count"] == 1


def test_rate_limit_is_retried_with_exponential_backoff() -> None:
    delays: list[float] = []
    client = FakeTushareClient(
        fund_daily_responses=[Exception("每分钟最多访问一次"), _fund_daily_frame()]
    )

    batch = _provider(client, max_retries=1, sleeper=delays.append).fetch_daily_bars(
        DailyBarsQuery(
            symbols=("510300.SH",),
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 13),
        )
    )

    assert len(batch.records) == 1
    assert len(client.fund_daily_calls) == 2
    assert delays == [0.25]


def test_exhausted_rate_limit_returns_stable_retryable_error() -> None:
    client = FakeTushareClient(
        fund_daily_responses=[Exception("访问频率超限"), Exception("访问频率超限")]
    )

    with pytest.raises(DomainError) as exc_info:
        _provider(client, max_retries=1).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 13),
            )
        )

    assert exc_info.value.code == "DATA_SOURCE_RATE_LIMITED"
    assert exc_info.value.retryable is True
    assert len(client.fund_daily_calls) == 2


def test_timeout_is_retried_then_returns_unavailable_error() -> None:
    client = FakeTushareClient(
        trade_cal_responses=[TimeoutError("socket timeout"), TimeoutError("socket timeout")]
    )

    with pytest.raises(DomainError) as exc_info:
        _provider(client, max_retries=1).fetch_trade_calendar(
            TradeCalendarQuery(
                exchange=Exchange.SSE,
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 31),
            )
        )

    assert exc_info.value.code == "DATA_SOURCE_UNAVAILABLE"
    assert exc_info.value.retryable is True
    assert exc_info.value.details["reason"] == "timeout"
    assert len(client.trade_cal_calls) == 2


def test_permission_error_does_not_retry_or_expose_token() -> None:
    secret = "token-super-secret-123"
    client = FakeTushareClient(
        fund_daily_responses=[Exception(f"TOKEN {secret} 无效或没有权限")]
    )

    with pytest.raises(DomainError) as exc_info:
        _provider(client, max_retries=3).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 13),
            )
        )

    error = exc_info.value
    assert error.code == "DATA_AUTH_FAILED"
    assert error.retryable is False
    assert len(client.fund_daily_calls) == 1
    assert secret not in str(error)
    assert secret not in repr(error.as_dict())
    assert error.__context__ is None


def test_missing_required_field_returns_schema_changed_error() -> None:
    frame = _fund_daily_frame().drop(columns=["amount"])
    client = FakeTushareClient(fund_daily_responses=[frame])

    with pytest.raises(DomainError) as exc_info:
        _provider(client).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 13),
            )
        )

    assert exc_info.value.code == "DATA_SOURCE_SCHEMA_CHANGED"
    assert exc_info.value.retryable is False
    assert exc_info.value.details["missing_fields"] == ("amount",)


def test_adjusted_query_is_rejected_before_client_call() -> None:
    client = FakeTushareClient()

    with pytest.raises(DomainError) as exc_info:
        _provider(client).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 13),
                adjustment=PriceAdjustment.QFQ,
            )
        )

    assert exc_info.value.code == "VALIDATION_ERROR"
    assert client.fund_daily_calls == []
