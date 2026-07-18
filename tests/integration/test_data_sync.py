"""Integration tests for the full data-sync orchestration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from etf_quant_lab.config import AppSettings
from etf_quant_lab.contracts.data import DailyBarsQuery, RawProviderBatch, TradeCalendarQuery
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    QualityGateStatus,
)
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.data_sync import DataSyncService, build_tushare_client
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
END = date(2026, 7, 13)
CAL_BATCH = "01K0D7F7P6XQ4M2Z8H9B3C5NX9"


class _FakeTushareProvider:
    """Returns deterministic raw records shaped like Tushare fund_daily rows."""

    def __init__(self, closes: list[str]) -> None:
        self._closes = closes
        self.source = DataSource.TUSHARE

    def fetch_daily_bars(self, query: DailyBarsQuery) -> RawProviderBatch:
        records = []
        for symbol in query.symbols:
            for offset, close in enumerate(self._closes):
                day = END - timedelta(days=len(self._closes) - 1 - offset)
                records.append(
                    {
                        "symbol": symbol,
                        "trade_date": day.strftime("%Y%m%d"),
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "pre_close": close,
                        "volume": "1000",
                        "amount": "4000",
                    }
                )
        return RawProviderBatch(
            batch_id=UlidGenerator().new(),
            source=DataSource.TUSHARE,
            dataset="fund_daily",
            records=tuple(records),
            fetched_at=FETCHED_AT,
        )

    def fetch_trade_calendar(self, query: TradeCalendarQuery) -> RawProviderBatch:
        raise NotImplementedError


def _sync_service(tmp_path: Path, closes: list[str]) -> DataSyncService:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    batches = DataBatchRepository(database, store)
    calendar = DuckDBTradingCalendarRepository(database)
    calendar.upsert_many(
        tuple(
            TradingCalendarDay(
                exchange=Exchange.SSE,
                cal_date=END - timedelta(days=offset),
                is_open=True,
                previous_open_date=None,
                next_open_date=None,
                source=DataSource.TUSHARE,
                batch_id=CAL_BATCH,
                updated_at=FETCHED_AT,
            )
            for offset in range(len(closes))
        )
    )
    quality = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=QualityReportRepository(database, UlidGenerator()),
        id_generator=UlidGenerator(),
    )
    return DataSyncService(
        provider=_FakeTushareProvider(closes),
        parquet_store=store,
        batch_repository=batches,
        quality_service=quality,
        id_generator=UlidGenerator(),
        clock=lambda: FETCHED_AT,
    )


def test_clean_sync_activates_batch_and_passes_gate(tmp_path: Path) -> None:
    service = _sync_service(tmp_path, ["4.00", "4.02", "4.05"])

    batch, report = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )

    assert batch.status is DataBatchStatus.ACTIVE
    assert report.gate_status is QualityGateStatus.PASSED


def test_tampered_data_is_rejected_not_activated(tmp_path: Path) -> None:
    # A >30% spike triggers the extreme-return ERROR and fails the gate.
    service = _sync_service(tmp_path, ["4.00", "4.02", "9.99"])

    batch, report = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )

    assert batch.status is DataBatchStatus.REJECTED
    assert report.gate_status is QualityGateStatus.FAILED


def test_resyncing_same_symbol_supersedes_only_its_own_prior_batch(tmp_path: Path) -> None:
    service = _sync_service(tmp_path, ["4.00", "4.02", "4.05"])
    first, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )

    second, report = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )

    assert report.gate_status is QualityGateStatus.PASSED
    assert second.status is DataBatchStatus.ACTIVE
    refreshed = service._batches.get(first.batch_id)
    assert refreshed is not None
    assert refreshed.status is DataBatchStatus.SUPERSEDED


def test_per_symbol_syncs_coexist_and_all_stay_active(tmp_path: Path) -> None:
    # Regression: syncing one symbol must not supersede a different symbol's
    # active batch, so a per-symbol universe sync keeps every symbol queryable.
    service = _sync_service(tmp_path, ["4.00", "4.02", "4.05"])

    first, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )
    second, _ = service.sync_daily_bars(
        symbols=("159915.SZ",),
        start_date=END - timedelta(days=2),
        end_date=END,
    )

    assert first.status is DataBatchStatus.ACTIVE
    assert second.status is DataBatchStatus.ACTIVE
    still_active = service._batches.get(first.batch_id)
    assert still_active is not None
    assert still_active.status is DataBatchStatus.ACTIVE
    # Both symbols are visible in the active daily-bars view together.
    symbols = {bar.symbol for bar in service._batches.query_daily_bars()}
    assert symbols == {"510300.SH", "159915.SZ"}


def test_missing_token_fails_loudly_without_leaking(tmp_path: Path) -> None:
    settings = AppSettings(
        env="test",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "eql.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
    )

    with pytest.raises(DomainError) as excinfo:
        build_tushare_client(settings)

    assert excinfo.value.code == "DATA_AUTH_MISSING"
    assert "token" not in str(excinfo.value.details).lower() or "EQL_TUSHARE_TOKEN" in str(
        excinfo.value.details
    )
