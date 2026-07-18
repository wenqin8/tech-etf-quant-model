"""End-to-end: raw Tushare batch -> normalize -> persist -> quality gate."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from etf_quant_lab.contracts.data import DataBatch, RawProviderBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    QualityGateStatus,
)
from etf_quant_lab.contracts.quality import RunQualityChecksRequest
from etf_quant_lab.data.normalize import normalize_tushare_daily_bars
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)

RAW_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NH1"
CANONICAL_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NH2"
FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
INGESTED_AT = datetime(2026, 7, 13, 8, 5, tzinfo=UTC)
GENERATED_AT = datetime(2026, 7, 13, 8, 30, tzinfo=UTC)


def _raw_record(trade_date: str, close: str) -> dict[str, object]:
    return {
        "symbol": "510300.SH",
        "trade_date": trade_date,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "pre_close": close,
        "volume": "1000",
        "amount": "4000",
    }


def test_raw_tushare_batch_flows_through_gate(tmp_path: Path) -> None:
    raw = RawProviderBatch(
        batch_id=RAW_BATCH_ID,
        source=DataSource.TUSHARE,
        dataset="fund_daily",
        records=(
            _raw_record("20260710", "4.030"),
            _raw_record("20260713", "4.050"),
        ),
        fetched_at=FETCHED_AT,
    )

    normalized = normalize_tushare_daily_bars(
        raw,
        batch_id=CANONICAL_BATCH_ID,
        ingested_at=INGESTED_AT,
    )
    assert normalized.findings == ()
    assert len(normalized.bars) == 2

    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    batches = DataBatchRepository(database, store)
    calendar = DuckDBTradingCalendarRepository(database)
    reports = QualityReportRepository(database, UlidGenerator())

    batches.create(
        DataBatch(
            batch_id=CANONICAL_BATCH_ID,
            provider=DataSource.TUSHARE,
            dataset="daily_bars",
            status=DataBatchStatus.FETCHING,
            fetched_at=FETCHED_AT,
            schema_version="daily_bar_v1",
        )
    )
    batches.stage_files(
        CANONICAL_BATCH_ID,
        store.write_canonical_daily_bars(normalized.bars),
    )

    service = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=reports,
        id_generator=UlidGenerator(),
        clock=lambda: GENERATED_AT,
    )
    report = service.run_checks(
        RunQualityChecksRequest(
            batch_id=CANONICAL_BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=normalized.bars[-1].trade_date,
        )
    )

    # No calendar rows loaded: calendar-dependent rules skip, but the pipeline
    # still runs end-to-end and the clean batch passes.
    assert report.gate_status is QualityGateStatus.PASSED
    assert report.checked_rows == 2

    # The batch can now be activated and surfaced through v_daily_bars.
    batches.activate(CANONICAL_BATCH_ID)
    active = batches.query_daily_bars()
    assert [bar.close for bar in active] == [normalized.bars[0].close, normalized.bars[1].close]
