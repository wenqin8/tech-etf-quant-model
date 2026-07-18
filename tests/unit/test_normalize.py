"""Unit tests for raw provider batch normalization into canonical bars."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.data import RawProviderBatch
from etf_quant_lab.contracts.enums import DataSource, Exchange, QualitySeverity
from etf_quant_lab.data.normalize import (
    akshare_close_map,
    normalize_akshare_daily_bars,
    normalize_tushare_daily_bars,
)

RAW_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NF1"
CANONICAL_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NF2"
FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
INGESTED_AT = datetime(2026, 7, 13, 8, 5, tzinfo=UTC)


def _tushare_batch(records: tuple[dict[str, object], ...]) -> RawProviderBatch:
    return RawProviderBatch(
        batch_id=RAW_BATCH_ID,
        source=DataSource.TUSHARE,
        dataset="fund_daily",
        records=records,
        fetched_at=FETCHED_AT,
    )


def _valid_record(trade_date: str = "20260710", close: str = "4.043") -> dict[str, object]:
    return {
        "symbol": "510300.SH",
        "trade_date": trade_date,
        "open": "4.001",
        "high": "4.070",
        "low": "3.998",
        "close": close,
        "pre_close": "4.010",
        "volume": "3840291",
        "amount": "1547220030.0",
    }


def test_normalize_maps_valid_record_to_canonical_bar() -> None:
    batch = _tushare_batch((_valid_record(),))

    result = normalize_tushare_daily_bars(
        batch,
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert result.findings == ()
    assert len(result.bars) == 1
    bar = result.bars[0]
    assert bar.symbol == "510300.SH"
    assert bar.exchange is Exchange.SSE
    assert bar.trade_date == date(2026, 7, 10)
    assert bar.close == Decimal("4.043")
    assert bar.pre_close == Decimal("4.010")
    assert bar.batch_id == CANONICAL_BATCH_ID
    assert bar.ingested_at == INGESTED_AT


def test_normalize_flags_missing_field_without_crashing() -> None:
    broken = _valid_record()
    del broken["close"]
    batch = _tushare_batch((broken, _valid_record(trade_date="20260713")))

    result = normalize_tushare_daily_bars(
        batch,
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert len(result.bars) == 1
    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.rule_code == "daily_bar.missing_field"
    assert finding.severity is QualitySeverity.BLOCKING
    assert "close" in tuple(finding.observed_value["missing_fields"])


def test_normalize_flags_illegal_ohlc_as_finding() -> None:
    illegal = _valid_record()
    illegal["high"] = "3.900"  # high below open/close violates the DailyBar contract
    batch = _tushare_batch((illegal,))

    result = normalize_tushare_daily_bars(
        batch,
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert result.bars == ()
    assert len(result.findings) == 1
    assert result.findings[0].rule_code == "daily_bar.invalid_record"
    assert result.findings[0].severity is QualitySeverity.BLOCKING


def test_normalize_flags_unparseable_number() -> None:
    bad = _valid_record()
    bad["close"] = "not-a-number"
    batch = _tushare_batch((bad,))

    result = normalize_tushare_daily_bars(
        batch,
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert result.bars == ()
    assert result.findings[0].rule_code == "daily_bar.invalid_record"


def test_akshare_close_map_extracts_overlap_keys() -> None:
    batch = RawProviderBatch(
        batch_id=RAW_BATCH_ID,
        source=DataSource.AKSHARE,
        dataset="fund_daily",
        records=(
            {"query_symbol": "510300.SH", "日期": "2026-07-10", "收盘": "4.050"},
            {"query_symbol": "510300.SH", "日期": "bad-date", "收盘": "4.060"},
            {"query_symbol": "510300.SH", "日期": "2026-07-13", "收盘": None},
        ),
        fetched_at=FETCHED_AT,
    )

    closes = akshare_close_map(batch)

    assert closes == {("510300.SH", date(2026, 7, 10)): Decimal("4.050")}


def _akshare_record(**overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "query_symbol": "510300.SH",
        "provider_symbol": "510300",
        "日期": "2026-07-14",
        "开盘": 4.744,
        "收盘": 4.837,
        "最高": 4.838,
        "最低": 4.696,
        "成交量": 18084400,  # lots (手)
        "成交额": 8630538000.0,
        "换手率": 9.78,
    }
    record.update(overrides)
    return record


def _akshare_batch(records: tuple[dict[str, object], ...]) -> RawProviderBatch:
    return RawProviderBatch(
        batch_id=RAW_BATCH_ID,
        source=DataSource.AKSHARE,
        dataset="fund_daily",
        records=records,
        fetched_at=FETCHED_AT,
    )


def test_akshare_normalizer_converts_lots_to_shares() -> None:
    result = normalize_akshare_daily_bars(
        _akshare_batch((_akshare_record(),)),
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert result.findings == ()
    bar = result.bars[0]
    assert bar.symbol == "510300.SH"
    assert bar.exchange is Exchange.SSE
    assert bar.trade_date == date(2026, 7, 14)
    assert bar.close == Decimal("4.837")
    # 18,084,400 lots x 100 shares per lot.
    assert bar.volume == Decimal("1808440000")
    assert bar.source is DataSource.AKSHARE
    assert bar.batch_id == CANONICAL_BATCH_ID


def test_akshare_normalizer_flags_missing_field() -> None:
    broken = _akshare_record()
    del broken["收盘"]

    result = normalize_akshare_daily_bars(
        _akshare_batch((broken, _akshare_record(日期="2026-07-15"))),
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert len(result.bars) == 1
    assert len(result.findings) == 1
    assert result.findings[0].rule_code == "daily_bar.missing_field"
    assert "收盘" in tuple(result.findings[0].observed_value["missing_fields"])


def test_akshare_normalizer_flags_illegal_ohlc() -> None:
    illegal = _akshare_record(最高=4.0)  # high below open/close

    result = normalize_akshare_daily_bars(
        _akshare_batch((illegal,)),
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )

    assert result.bars == ()
    assert result.findings[0].rule_code == "daily_bar.invalid_record"
    assert result.findings[0].severity is QualitySeverity.BLOCKING
