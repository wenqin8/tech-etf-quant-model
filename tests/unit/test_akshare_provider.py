"""Unit tests for the network-isolated AKShare cross-validation adapter."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from etf_quant_lab.contracts.data import DailyBarsQuery, TradeCalendarQuery
from etf_quant_lab.contracts.enums import DataSource, Exchange, PriceAdjustment
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.data.providers.akshare import AkshareProvider, _adjust_sina_etf_qfq
from etf_quant_lab.ids import FixedIdGenerator

FETCHED_AT = datetime(2026, 7, 14, 16, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NA4"


def _daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "日期": "2026-07-10",
                "开盘": 4.001,
                "收盘": 4.043,
                "最高": 4.056,
                "最低": 3.998,
                "成交量": 3840291.0,
                "成交额": "1547220030.00",
                "换手率": 0.72,
            }
        ]
    )


def _sina_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": date(2026, 7, 10),
                "open": 4.001,
                "high": 4.056,
                "low": 3.998,
                "close": 4.043,
                "volume": 384029100,
                "amount": 1547220030.0,
            }
        ]
    )


def _provider(
    daily_fetcher: Any,
    *,
    calendar_fetcher: Any | None = None,
) -> AkshareProvider:
    return AkshareProvider(
        daily_bars_fetcher=daily_fetcher,
        trade_calendar_fetcher=calendar_fetcher,
        id_generator=FixedIdGenerator([BATCH_ID]),
        clock=lambda: FETCHED_AT,
    )


def test_daily_bars_maps_internal_code_and_preserves_raw_units() -> None:
    calls: list[dict[str, object]] = []

    def fake_fetcher(**kwargs: object) -> pd.DataFrame:
        calls.append(dict(kwargs))
        return _daily_frame()

    batch = _provider(fake_fetcher).fetch_daily_bars(
        DailyBarsQuery(
            symbols=("510300.SH",),
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 10),
            adjustment=PriceAdjustment.QFQ,
        )
    )

    assert calls == [
        {
            "symbol": "510300",
            "period": "daily",
            "start_date": "20260701",
            "end_date": "20260710",
            "adjust": "qfq",
        }
    ]
    assert batch.source is DataSource.AKSHARE
    assert batch.dataset == "fund_daily"
    assert batch.fetched_at == FETCHED_AT
    assert batch.request_metadata["provider_role"] == "cross_validation"
    assert batch.request_metadata["publication_eligible"] is False

    record = batch.records[0]
    assert record["query_symbol"] == "510300.SH"
    assert record["provider_symbol"] == "510300"
    assert record["日期"] == "2026-07-10"
    assert record["成交量"] == 3840291.0
    assert record["成交额"] == "1547220030.00"


def test_daily_bars_keeps_empty_response_as_auditable_raw_batch() -> None:
    empty = _daily_frame().iloc[0:0]

    batch = _provider(lambda **_: empty).fetch_daily_bars(
        DailyBarsQuery(
            symbols=("159915.SZ",),
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 10),
        )
    )

    assert batch.records == ()
    assert batch.request_metadata["empty_symbols"] == ("159915.SZ",)
    assert batch.request_metadata["publication_eligible"] is False


def test_daily_bars_rejects_provider_column_change() -> None:
    changed_frame = _daily_frame().rename(columns={"成交额": "成交金额"})

    with pytest.raises(DomainError) as exc_info:
        _provider(lambda **_: changed_frame).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 10),
            )
        )

    assert exc_info.value.code == "DATA_SOURCE_SCHEMA_CHANGED"
    assert exc_info.value.retryable is False
    assert exc_info.value.details["missing_columns"] == ("成交额",)


def test_daily_bars_wraps_provider_exception_without_leaking_message() -> None:
    upstream_error = RuntimeError("request URL contains a private proxy credential")

    def failing_fetcher(**_: object) -> pd.DataFrame:
        raise upstream_error

    with pytest.raises(DomainError) as exc_info:
        _provider(failing_fetcher).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.SH",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 10),
            )
        )

    error = exc_info.value
    assert error.code == "DATA_SOURCE_UNAVAILABLE"
    assert error.retryable is True
    assert error.__cause__ is upstream_error
    assert "credential" not in error.message
    assert error.details == {
        "provider": "AKSHARE",
        "dataset": "fund_daily",
        "operation": "fund_etf_hist_em",
        "symbol": "510300.SH",
    }


def test_daily_bars_rejects_symbol_that_cannot_map_to_akshare() -> None:
    with pytest.raises(DomainError) as exc_info:
        _provider(lambda **_: _daily_frame()).fetch_daily_bars(
            DailyBarsQuery(
                symbols=("510300.HK",),
                start_date=date(2026, 7, 1),
                end_date=date(2026, 7, 10),
            )
        )

    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR.value


def test_qfq_query_falls_back_to_adjusted_sina_frame(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = AkshareProvider(
        id_generator=FixedIdGenerator([BATCH_ID]),
        clock=lambda: FETCHED_AT,
    )

    def unavailable_eastmoney(**_: object) -> pd.DataFrame:
        raise ConnectionError("Remote end closed connection")

    monkeypatch.setattr(provider, "_load_daily_bars_fetcher", lambda: unavailable_eastmoney)
    monkeypatch.setattr(
        provider,
        "_fetch_sina_daily",
        lambda query, symbol: _sina_daily_frame(),
    )

    batch = provider.fetch_daily_bars(
        DailyBarsQuery(
            symbols=("510300.SH",),
            start_date=date(2026, 7, 1),
            end_date=date(2026, 7, 10),
            adjustment=PriceAdjustment.QFQ,
        )
    )

    assert len(batch.records) == 1
    assert batch.records[0]["query_symbol"] == "510300.SH"
    assert batch.records[0]["close"] == 4.043
    assert batch.request_metadata["adjustment"] == "QFQ"
    assert batch.request_metadata["operations_by_symbol"] == {
        "510300.SH": "fund_etf_hist_sina_qfq"
    }


def test_sina_qfq_adjusts_cash_distributions_and_share_splits() -> None:
    raw = pd.DataFrame(
        [
            {
                "date": "2025-06-17",
                "open": 4.50,
                "high": 4.52,
                "low": 4.48,
                "close": 4.50,
            },
            {
                "date": "2025-06-18",
                "open": 4.41,
                "high": 4.43,
                "low": 4.40,
                "close": 4.412,
            },
            {
                "date": "2026-07-06",
                "open": 3.00,
                "high": 3.02,
                "low": 2.98,
                "close": 3.009,
            },
            {
                "date": "2026-07-07",
                "open": 1.49,
                "high": 1.51,
                "low": 1.48,
                "close": 1.501,
            },
        ]
    )
    factors = pd.DataFrame(
        [
            {"d": "1900-01-01", "f": "1", "s": "2", "u": "0.088"},
            {"d": "2025-06-18", "f": "1", "s": "2", "u": "0"},
            {"d": "2026-07-07", "f": "1", "s": "1", "u": "0"},
        ]
    )

    adjusted = _adjust_sina_etf_qfq(raw, factors, symbol="159995.SZ")

    assert adjusted["close"].tolist() == pytest.approx(
        [2.162, 2.206, 1.5045, 1.501]
    )
    assert all(
        len(str(value).partition(".")[2]) <= 6 for value in adjusted["close"]
    )
    assert adjusted["qfq_scale"].tolist() == [2, 2, 2, 1]
    assert adjusted["qfq_cash"].tolist() == pytest.approx([0.088, 0, 0, 0])


def test_sina_qfq_rejects_unknown_face_value_transformation() -> None:
    factors = pd.DataFrame(
        [{"d": "1900-01-01", "f": "2", "s": "1", "u": "0"}]
    )

    with pytest.raises(DomainError, match="face-value"):
        _adjust_sina_etf_qfq(_sina_daily_frame(), factors, symbol="510300.SH")


def test_trade_calendar_filters_range_but_preserves_raw_date_values() -> None:
    calendar_frame = pd.DataFrame(
        {
            "trade_date": [
                date(2026, 7, 9),
                date(2026, 7, 10),
                date(2026, 7, 13),
                date(2026, 7, 14),
            ]
        }
    )
    provider = _provider(lambda **_: _daily_frame(), calendar_fetcher=lambda: calendar_frame)

    batch = provider.fetch_trade_calendar(
        TradeCalendarQuery(
            exchange=Exchange.SSE,
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 13),
        )
    )

    assert [record["trade_date"] for record in batch.records] == [
        date(2026, 7, 10),
        date(2026, 7, 13),
    ]
    assert batch.request_metadata["provider_role"] == "cross_validation"
    assert batch.request_metadata["publication_eligible"] is False
