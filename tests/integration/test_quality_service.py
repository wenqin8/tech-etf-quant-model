"""Integration tests for QualityService over persisted batches."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    QualityGateStatus,
    QualitySeverity,
)
from etf_quant_lab.contracts.quality import QualityThresholds, RunQualityChecksRequest
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.ids import FixedIdGenerator, UlidGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 7, 13, 8, 30, tzinfo=UTC)
BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NG1"
CAL_BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NG9"
OPEN_DATES = (date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13))


def _bar(*, trade_date: date, close: str, batch_id: str = BATCH_ID) -> DailyBar:
    close_price = Decimal(close)
    return DailyBar(
        symbol="510300.SH",
        trade_date=trade_date,
        exchange=Exchange.SSE,
        open=close_price,
        high=close_price + Decimal("0.05"),
        low=close_price - Decimal("0.05"),
        close=close_price,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id=batch_id,
        ingested_at=FETCHED_AT,
    )


def _service(
    tmp_path: Path,
    *,
    report_ids: list[str] | None = None,
) -> tuple[QualityService, DataBatchRepository, ParquetStore]:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    batches = DataBatchRepository(database, store)
    calendar = DuckDBTradingCalendarRepository(database)
    calendar.upsert_many(
        tuple(
            TradingCalendarDay(
                exchange=Exchange.SSE,
                cal_date=day,
                is_open=True,
                previous_open_date=None,
                next_open_date=None,
                source=DataSource.TUSHARE,
                batch_id=CAL_BATCH_ID,
                updated_at=FETCHED_AT,
            )
            for day in OPEN_DATES
        )
    )
    reports = QualityReportRepository(database, UlidGenerator())
    id_generator = FixedIdGenerator(report_ids) if report_ids else UlidGenerator()
    service = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=reports,
        id_generator=id_generator,
        thresholds=QualityThresholds(),
        clock=lambda: GENERATED_AT,
    )
    return service, batches, store


def _stage_batch(
    batches: DataBatchRepository,
    store: ParquetStore,
    bars: tuple[DailyBar, ...],
) -> None:
    batches.create(
        DataBatch(
            batch_id=BATCH_ID,
            provider=DataSource.TUSHARE,
            dataset="daily_bars",
            status=DataBatchStatus.FETCHING,
            fetched_at=FETCHED_AT,
            schema_version="daily_bar_v1",
        ),
        requested_start=min(bar.trade_date for bar in bars),
        requested_end=max(bar.trade_date for bar in bars),
    )
    batches.stage_files(BATCH_ID, store.write_canonical_daily_bars(bars))


def test_run_checks_passes_clean_batch_before_activation(tmp_path: Path) -> None:
    service, batches, store = _service(
        tmp_path,
        report_ids=["01K0D7F7P6XQ4M2Z8H9B3C5NGR"],
    )
    bars = (
        _bar(trade_date=date(2026, 7, 9), close="4.000"),
        _bar(trade_date=date(2026, 7, 10), close="4.030"),
        _bar(trade_date=date(2026, 7, 13), close="4.050"),
    )
    _stage_batch(batches, store, bars)

    report = service.run_checks(
        RunQualityChecksRequest(
            batch_id=BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=date(2026, 7, 13),
        )
    )

    assert report.gate_status is QualityGateStatus.PASSED
    assert report.checked_rows == 3
    assert report.findings == ()
    # The report is persisted and readable for the quality-issues page.
    stored = service.get_report(BATCH_ID)
    assert stored is not None
    assert stored.report_id == "01K0D7F7P6XQ4M2Z8H9B3C5NGR"


def test_run_checks_blocks_tampered_history(tmp_path: Path) -> None:
    service, batches, store = _service(tmp_path)
    # A deliberately corrupted spike on 7-13: 4.03 -> 9.90 is a >30% jump.
    bars = (
        _bar(trade_date=date(2026, 7, 9), close="4.000"),
        _bar(trade_date=date(2026, 7, 10), close="4.030"),
        _bar(trade_date=date(2026, 7, 13), close="9.900"),
    )
    _stage_batch(batches, store, bars)

    report = service.run_checks(
        RunQualityChecksRequest(
            batch_id=BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=date(2026, 7, 13),
        )
    )

    assert report.gate_status is QualityGateStatus.FAILED
    assert report.is_blocking
    assert report.count(QualitySeverity.ERROR) >= 1
    codes = {finding.rule_code for finding in report.findings}
    assert "daily_bar.extreme_return" in codes


def test_run_checks_is_idempotent_per_batch(tmp_path: Path) -> None:
    service, batches, store = _service(tmp_path)
    bars = (
        _bar(trade_date=date(2026, 7, 10), close="4.030"),
        _bar(trade_date=date(2026, 7, 13), close="4.050"),
    )
    _stage_batch(batches, store, bars)
    request = RunQualityChecksRequest(
        batch_id=BATCH_ID,
        exchange=Exchange.SSE,
        as_of_date=date(2026, 7, 13),
    )

    first = service.run_checks(request)
    second = service.run_checks(request)

    assert first.batch_id == second.batch_id
    stored = service.get_report(BATCH_ID)
    assert stored is not None
    # Re-running replaces rather than accumulates: exactly one report remains.
    assert stored.report_id == second.report_id


def test_run_checks_detects_future_date(tmp_path: Path) -> None:
    service, batches, store = _service(tmp_path)
    bars = (
        _bar(trade_date=date(2026, 7, 10), close="4.030"),
        _bar(trade_date=date(2026, 7, 13), close="4.050"),
    )
    _stage_batch(batches, store, bars)

    report = service.run_checks(
        RunQualityChecksRequest(
            batch_id=BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=date(2026, 7, 11),
        )
    )

    assert report.gate_status is QualityGateStatus.FAILED
    codes = {finding.rule_code for finding in report.findings}
    assert "daily_bar.future_date" in codes


def test_compare_sources_grades_price_divergence(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    primary = (
        _bar(trade_date=date(2026, 7, 10), close="4.030"),
        _bar(trade_date=date(2026, 7, 13), close="4.050"),
    )

    agreeing = service.compare_sources(
        primary,
        {
            ("510300.SH", date(2026, 7, 10)): "4.031",
            ("510300.SH", date(2026, 7, 13)): "4.050",
        },
    )
    assert agreeing.gate_status is QualityGateStatus.PASSED
    assert agreeing.matched_rows == 2
    assert agreeing.mismatch_count == 0

    diverging = service.compare_sources(
        primary,
        {
            ("510300.SH", date(2026, 7, 10)): "4.500",
            ("510300.SH", date(2026, 7, 13)): "4.050",
        },
    )
    assert diverging.gate_status is QualityGateStatus.FAILED
    assert diverging.mismatch_count == 1
    assert diverging.max_price_relative_difference > Decimal("0.1")


def test_compare_sources_fails_without_overlap(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    primary = (_bar(trade_date=date(2026, 7, 10), close="4.030"),)

    report = service.compare_sources(
        primary,
        {("510300.SH", date(2026, 7, 13)): "4.050"},
    )

    assert report.gate_status is QualityGateStatus.FAILED
    assert report.matched_rows == 0
    assert report.missing_in_secondary == 1
    assert report.missing_in_primary == 1
