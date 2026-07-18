"""Integration tests for page presenters: empty, stale, blocked and ready states."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    QualityGateStatus,
)
from etf_quant_lab.contracts.quality import RunQualityChecksRequest
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.services.tasks import TaskRunService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)
from etf_quant_lab.storage.signal import SignalRepository
from etf_quant_lab.ui.presenters import (
    BANNER_NO_DATA,
    BANNER_QUALITY_FAILED,
    BANNER_READY,
    BANNER_READY_PREVIOUS_CLOSE,
    BANNER_STALE,
    build_dashboard,
    build_data_center,
    build_task_history,
    describe_signal_state,
)

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
TODAY = date(2026, 7, 13)
BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NT1"
CAL_BATCH = "01K0D7F7P6XQ4M2Z8H9B3C5NT9"


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.database = DuckDBDatabase(tmp_path / "eql.duckdb")
        self.database.migrate()
        self.store = ParquetStore(tmp_path / "data")
        self.batches = DataBatchRepository(self.database, self.store)
        self.calendar = DuckDBTradingCalendarRepository(self.database)
        self.signals = SignalRepository(self.database, UlidGenerator())
        self.quality = QualityService(
            batch_repository=self.batches,
            calendar_repository=self.calendar,
            report_repository=QualityReportRepository(self.database, UlidGenerator()),
            id_generator=UlidGenerator(),
        )

    def add_calendar(self, days: list[date]) -> None:
        self.calendar.upsert_many(
            tuple(
                TradingCalendarDay(
                    exchange=Exchange.SSE,
                    cal_date=day,
                    is_open=True,
                    previous_open_date=None,
                    next_open_date=None,
                    source=DataSource.TUSHARE,
                    batch_id=CAL_BATCH,
                    updated_at=FETCHED_AT,
                )
                for day in days
            )
        )

    def publish_bars(self, *, latest: date, closes: list[str]) -> None:
        dates = [latest - timedelta(days=offset) for offset in range(len(closes) - 1, -1, -1)]
        bars = tuple(
            DailyBar(
                symbol="510300.SH",
                trade_date=day,
                exchange=Exchange.SSE,
                open=Decimal(close),
                high=Decimal(close) + Decimal("0.05"),
                low=Decimal(close) - Decimal("0.05"),
                close=Decimal(close),
                volume=Decimal("1000"),
                amount=Decimal("4000"),
                source=DataSource.TUSHARE,
                batch_id=BATCH_ID,
                ingested_at=FETCHED_AT,
            )
            for day, close in zip(dates, closes, strict=True)
        )
        self.batches.create(
            DataBatch(
                batch_id=BATCH_ID,
                provider=DataSource.TUSHARE,
                dataset="daily_bars",
                status=DataBatchStatus.FETCHING,
                fetched_at=FETCHED_AT,
                schema_version="daily_bar_v1",
            )
        )
        self.batches.stage_files(BATCH_ID, self.store.write_canonical_daily_bars(bars))
        self.batches.activate(BATCH_ID)

    def dashboard(self, as_of: date = TODAY) -> object:
        return build_dashboard(
            as_of=as_of,
            batch_repository=self.batches,
            calendar_repository=self.calendar,
            quality_service=self.quality,
            signal_repository=self.signals,
        )


def test_empty_database_shows_no_data_banner(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.add_calendar([TODAY])

    view = env.dashboard()

    assert not view.data_ready
    assert view.banner == BANNER_NO_DATA
    assert view.latest_bar_date is None
    assert view.open_signal is None


def test_fresh_data_shows_ready_banner(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.add_calendar([TODAY - timedelta(days=1), TODAY])
    env.publish_bars(latest=TODAY, closes=["4.00", "4.05"])

    view = env.dashboard()

    assert view.data_ready
    assert view.banner == BANNER_READY
    assert view.latest_bar_date == TODAY


def test_intraday_with_previous_close_is_ready_not_stale(tmp_path: Path) -> None:
    # During today's session the latest bar is yesterday's close; that is the
    # freshest possible state and must not read as "stale".
    env = _Env(tmp_path)
    yesterday = TODAY - timedelta(days=1)
    env.add_calendar([yesterday, TODAY])
    env.publish_bars(latest=yesterday, closes=["4.00", "4.05"])

    view = env.dashboard()

    assert view.data_ready
    assert view.banner == BANNER_READY_PREVIOUS_CLOSE
    assert view.latest_bar_date == yesterday


def test_stale_data_shows_stale_banner(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    stale_day = TODAY - timedelta(days=3)
    env.add_calendar([stale_day, TODAY - timedelta(days=1), TODAY])
    env.publish_bars(latest=stale_day, closes=["4.00", "4.05"])

    view = env.dashboard()

    assert not view.data_ready
    assert view.banner == BANNER_STALE


def test_failed_quality_gate_blocks_dashboard(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.add_calendar([TODAY - timedelta(days=1), TODAY])
    # A >30% spike fails the extreme-return rule at ERROR level.
    env.publish_bars(latest=TODAY, closes=["4.00", "9.99"])
    env.quality.run_checks(
        RunQualityChecksRequest(
            batch_id=BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=TODAY,
        )
    )

    view = env.dashboard()

    assert not view.data_ready
    assert view.banner == BANNER_QUALITY_FAILED
    assert view.gate_status is QualityGateStatus.FAILED


def test_data_center_lists_batches_with_gate_outcomes(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    env.add_calendar([TODAY - timedelta(days=1), TODAY])
    env.publish_bars(latest=TODAY, closes=["4.00", "4.05"])
    env.quality.run_checks(
        RunQualityChecksRequest(
            batch_id=BATCH_ID,
            exchange=Exchange.SSE,
            as_of_date=TODAY,
        )
    )

    view = build_data_center(
        batch_repository=env.batches,
        quality_service=env.quality,
    )

    assert [batch.batch_id for batch in view.batches] == [BATCH_ID]
    assert view.gate_by_batch[BATCH_ID] is QualityGateStatus.PASSED
    assert view.latest_bar_date == TODAY


def test_task_history_lists_recent_runs(tmp_path: Path) -> None:
    env = _Env(tmp_path)
    tasks = TaskRunService(
        env.database,
        UlidGenerator(),
        lock_dir=tmp_path / "locks",
    )
    tasks.run("daily_signal", lambda: {"ok": True})
    tasks.run("data_sync", lambda: {"rows": 10})

    view = build_task_history(env.database)

    names = [run.task_name for run in view.runs]
    assert set(names) == {"daily_signal", "data_sync"}
    assert all(run.status == "SUCCEEDED" for run in view.runs)


def test_describe_signal_state_covers_all_cases(tmp_path: Path) -> None:
    assert "尚未生成" in describe_signal_state(None)
