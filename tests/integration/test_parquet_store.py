"""Integration tests for immutable raw and canonical Parquet persistence."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from etf_quant_lab.contracts.data import DailyBar, RawProviderBatch
from etf_quant_lab.contracts.enums import DataLayer, DataSource, Exchange
from etf_quant_lab.storage.parquet import (
    ChecksumMismatchError,
    ParquetStorageError,
    ParquetStore,
)

RAW_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NB1"
CANONICAL_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NB2"
FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def _raw_batch(batch_id: str = RAW_BATCH_ID) -> RawProviderBatch:
    return RawProviderBatch(
        batch_id=batch_id,
        source=DataSource.TUSHARE,
        dataset="fund_daily",
        records=(
            {"symbol": "510300.SH", "trade_date": "20260710", "close": "4.043"},
            {"symbol": "510300.SH", "trade_date": "20260713", "close": "4.061"},
        ),
        fetched_at=FETCHED_AT,
    )


def _daily_bar(
    *,
    symbol: str,
    trade_date: date,
    exchange: Exchange,
    batch_id: str = CANONICAL_BATCH_ID,
    close: str = "4.043",
) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=exchange,
        open=Decimal("4.001"),
        high=Decimal("4.070"),
        low=Decimal("3.998"),
        close=Decimal(close),
        volume=Decimal("3840291"),
        amount=Decimal("1547220030.0000"),
        source=DataSource.TUSHARE,
        batch_id=batch_id,
        ingested_at=FETCHED_AT,
    )


def test_write_raw_appends_immutable_snapshot(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    artifact = store.write_raw(_raw_batch())

    assert artifact.layer is DataLayer.RAW
    assert artifact.row_count == 2
    assert artifact.relative_path.startswith("raw/provider=tushare/dataset=fund_daily/")
    assert len(artifact.checksum) == 64
    store.verify(artifact)


def test_write_raw_never_overwrites_existing_batch(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    store.write_raw(_raw_batch())

    with pytest.raises(FileExistsError):
        store.write_raw(_raw_batch())


def test_write_canonical_partitions_by_exchange_and_month(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bars = (
        _daily_bar(symbol="510300.SH", trade_date=date(2026, 7, 10), exchange=Exchange.SSE),
        _daily_bar(symbol="510300.SH", trade_date=date(2026, 8, 3), exchange=Exchange.SSE),
        _daily_bar(symbol="159915.SZ", trade_date=date(2026, 7, 10), exchange=Exchange.SZSE),
    )

    artifacts = store.write_canonical_daily_bars(bars)

    partitions = sorted(artifact.relative_path for artifact in artifacts)
    assert partitions == [
        "canonical/dataset=daily_bars/exchange=sse/year=2026/month=07/"
        f"part-{CANONICAL_BATCH_ID}.parquet",
        "canonical/dataset=daily_bars/exchange=sse/year=2026/month=08/"
        f"part-{CANONICAL_BATCH_ID}.parquet",
        "canonical/dataset=daily_bars/exchange=szse/year=2026/month=07/"
        f"part-{CANONICAL_BATCH_ID}.parquet",
    ]
    for artifact in artifacts:
        store.verify(artifact)
    july_sse = next(a for a in artifacts if "exchange=sse/year=2026/month=07" in a.relative_path)
    assert july_sse.min_trade_date == date(2026, 7, 10)
    assert july_sse.max_trade_date == date(2026, 7, 10)


def test_write_canonical_rejects_multiple_batch_ids(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bars = (
        _daily_bar(
            symbol="510300.SH",
            trade_date=date(2026, 7, 10),
            exchange=Exchange.SSE,
            batch_id=CANONICAL_BATCH_ID,
        ),
        _daily_bar(
            symbol="510300.SH",
            trade_date=date(2026, 7, 13),
            exchange=Exchange.SSE,
            batch_id=RAW_BATCH_ID,
        ),
    )

    with pytest.raises(ParquetStorageError, match="exactly one batch_id"):
        store.write_canonical_daily_bars(bars)


def test_write_canonical_is_idempotent_for_identical_bytes(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    bars = (
        _daily_bar(symbol="510300.SH", trade_date=date(2026, 7, 10), exchange=Exchange.SSE),
    )

    first = store.write_canonical_daily_bars(bars)
    second = store.write_canonical_daily_bars(bars)

    assert first[0].checksum == second[0].checksum


def test_verify_detects_tampered_file(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    artifact = store.write_raw(_raw_batch())
    target = store.resolve(artifact.relative_path)
    target.write_bytes(target.read_bytes() + b"corruption")

    with pytest.raises(ChecksumMismatchError):
        store.verify(artifact)


def test_resolve_rejects_path_escape(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)

    with pytest.raises(ParquetStorageError, match="escapes"):
        store.resolve("../escape.parquet")


def test_recover_temporary_files_removes_orphans(tmp_path: Path) -> None:
    store = ParquetStore(tmp_path)
    orphan = tmp_path / "canonical" / "dataset=daily_bars"
    orphan.mkdir(parents=True)
    stray = orphan / ".tmp-deadbeef-part.parquet"
    stray.write_bytes(b"partial")

    recovered = store.recover_temporary_files()

    assert stray in recovered
    assert not stray.exists()
