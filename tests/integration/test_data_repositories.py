"""Integration tests for the node-5 DuckDB data-batch and daily-bar repositories."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import DataBatchStatus, DataSource, Exchange
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    InvalidBatchTransitionError,
)

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
FIRST_BATCH = "01K0D7F7P6XQ4M2Z8H9B3C5NC1"
SECOND_BATCH = "01K0D7F7P6XQ4M2Z8H9B3C5NC2"


def _database(tmp_path: Path) -> tuple[DataBatchRepository, ParquetStore]:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    return DataBatchRepository(database, store), store


def _daily_batch(batch_id: str) -> DataBatch:
    return DataBatch(
        batch_id=batch_id,
        provider=DataSource.TUSHARE,
        dataset="daily_bars",
        status=DataBatchStatus.FETCHING,
        fetched_at=FETCHED_AT,
        schema_version="daily_bar_v1",
    )


def _bar(*, symbol: str, trade_date: date, batch_id: str, close: str) -> DailyBar:
    close_price = Decimal(close)
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        open=close_price,
        high=close_price + Decimal("0.050"),
        low=close_price - Decimal("0.050"),
        close=close_price,
        volume=Decimal("3840291"),
        amount=Decimal("1547220030.0000"),
        source=DataSource.TUSHARE,
        batch_id=batch_id,
        ingested_at=FETCHED_AT,
    )


def _publish_active_batch(
    repository: DataBatchRepository,
    store: ParquetStore,
    *,
    batch_id: str,
    bars: tuple[DailyBar, ...],
    supersede: tuple[str, ...] = (),
) -> None:
    repository.create(
        _daily_batch(batch_id),
        requested_start=min(bar.trade_date for bar in bars),
        requested_end=max(bar.trade_date for bar in bars),
    )
    artifacts = store.write_canonical_daily_bars(bars)
    repository.stage_files(batch_id, artifacts)
    repository.activate(batch_id, supersede_batch_ids=supersede)


def test_create_requires_fetching_status(tmp_path: Path) -> None:
    repository, _ = _database(tmp_path)
    active_batch = DataBatch(
        batch_id=FIRST_BATCH,
        provider=DataSource.TUSHARE,
        dataset="daily_bars",
        status=DataBatchStatus.ACTIVE,
        fetched_at=FETCHED_AT,
        schema_version="daily_bar_v1",
    )

    with pytest.raises(InvalidBatchTransitionError, match="FETCHING"):
        repository.create(active_batch)


def test_activate_publishes_bars_into_view(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 13), batch_id=FIRST_BATCH, close="4.061"),
    )

    _publish_active_batch(repository, store, batch_id=FIRST_BATCH, bars=bars)

    stored = repository.get(FIRST_BATCH)
    assert stored is not None
    assert stored.status is DataBatchStatus.ACTIVE
    assert stored.row_count == 2
    queried = repository.query_daily_bars()
    assert [bar.trade_date for bar in queried] == [date(2026, 7, 10), date(2026, 7, 13)]
    assert queried[0].symbol == "510300.SH"


def test_query_daily_bars_prunes_by_symbol_and_date(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 13), batch_id=FIRST_BATCH, close="4.061"),
        _bar(symbol="159915.SZ", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="2.500"),
    )
    _publish_active_batch(repository, store, batch_id=FIRST_BATCH, bars=bars)

    only_sse = repository.query_daily_bars(symbols=("510300.SH",))
    assert {bar.symbol for bar in only_sse} == {"510300.SH"}

    windowed = repository.query_daily_bars(
        start_date=date(2026, 7, 11),
        end_date=date(2026, 7, 13),
    )
    assert [bar.trade_date for bar in windowed] == [date(2026, 7, 13)]


def test_activate_supersedes_previous_active_batch(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    first_bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    _publish_active_batch(repository, store, batch_id=FIRST_BATCH, bars=first_bars)

    revised_bars = (
        _bar(
            symbol="510300.SH",
            trade_date=date(2026, 7, 10),
            batch_id=SECOND_BATCH,
            close="9.999",
        ),
    )
    _publish_active_batch(
        repository,
        store,
        batch_id=SECOND_BATCH,
        bars=revised_bars,
        supersede=(FIRST_BATCH,),
    )

    superseded = repository.get(FIRST_BATCH)
    assert superseded is not None
    assert superseded.status is DataBatchStatus.SUPERSEDED
    active_close = repository.query_daily_bars()[0].close
    assert active_close == Decimal("9.999")


def test_activate_requires_canonical_files_for_daily_bars(tmp_path: Path) -> None:
    repository, _ = _database(tmp_path)
    repository.create(_daily_batch(FIRST_BATCH))

    with pytest.raises(InvalidBatchTransitionError):
        repository.activate(FIRST_BATCH)


def test_stage_files_verifies_and_moves_to_validating(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    repository.create(_daily_batch(FIRST_BATCH))
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    artifacts = store.write_canonical_daily_bars(bars)

    staged = repository.stage_files(FIRST_BATCH, artifacts)

    assert staged.status is DataBatchStatus.VALIDATING
    assert staged.file_count == 1
    assert staged.checksum is not None


def test_reject_batch_keeps_files_and_records_reason(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    repository.create(_daily_batch(FIRST_BATCH))
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    repository.stage_files(FIRST_BATCH, store.write_canonical_daily_bars(bars))

    rejected = repository.reject(FIRST_BATCH, "cross-source difference exceeded")

    assert rejected.status is DataBatchStatus.REJECTED
    assert rejected.error_summary == "cross-source difference exceeded"
    assert repository.query_daily_bars() == ()


def test_active_manifest_checksum_is_stable_and_reflects_supersede(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    _publish_active_batch(repository, store, batch_id=FIRST_BATCH, bars=bars)

    first_checksum = repository.active_daily_bar_manifest_checksum()
    assert first_checksum == repository.active_daily_bar_manifest_checksum()

    revised = (
        _bar(
            symbol="510300.SH",
            trade_date=date(2026, 7, 10),
            batch_id=SECOND_BATCH,
            close="5.000",
        ),
    )
    _publish_active_batch(
        repository,
        store,
        batch_id=SECOND_BATCH,
        bars=revised,
        supersede=(FIRST_BATCH,),
    )

    assert repository.active_daily_bar_manifest_checksum() != first_checksum


def test_activate_rejects_self_supersede(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    repository.create(_daily_batch(FIRST_BATCH))
    repository.stage_files(FIRST_BATCH, store.write_canonical_daily_bars(bars))

    with pytest.raises(ValueError, match="cannot supersede itself"):
        repository.activate(FIRST_BATCH, supersede_batch_ids=(FIRST_BATCH,))


def test_stage_files_requires_matching_batch_id(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    repository.create(_daily_batch(FIRST_BATCH))
    foreign_bars = (
        _bar(
            symbol="510300.SH",
            trade_date=date(2026, 7, 10),
            batch_id=SECOND_BATCH,
            close="4.043",
        ),
    )
    artifacts = store.write_canonical_daily_bars(foreign_bars)

    with pytest.raises(ValueError, match="belong to the staged batch"):
        repository.stage_files(FIRST_BATCH, artifacts)


def test_reject_requires_reason_and_open_batch(tmp_path: Path) -> None:
    repository, store = _database(tmp_path)
    bars = (
        _bar(symbol="510300.SH", trade_date=date(2026, 7, 10), batch_id=FIRST_BATCH, close="4.043"),
    )
    _publish_active_batch(repository, store, batch_id=FIRST_BATCH, bars=bars)

    with pytest.raises(InvalidBatchTransitionError):
        repository.reject(FIRST_BATCH, "already active")


def test_get_missing_batch_returns_none(tmp_path: Path) -> None:
    repository, _ = _database(tmp_path)

    assert repository.get(FIRST_BATCH) is None
