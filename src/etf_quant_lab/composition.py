"""Composition root: wire every real service into the application context.

``build_full_context`` is what the Streamlit entry point and the scheduler call:
it migrates the database, constructs the DuckDB-backed repositories and all
services from nodes 5-18, and registers them on the shared
:class:`ApplicationContext` under their documented service names.
"""

from __future__ import annotations

from etf_quant_lab.app_context import ApplicationContext, build_application_context
from etf_quant_lab.config import AppSettings
from etf_quant_lab.contracts.enums import StrategyId
from etf_quant_lab.domain.strategies.etf_rotation import EtfRotationStrategy
from etf_quant_lab.domain.strategies.trend_baseline import TrendBaselineStrategy
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.paper import PaperAccountService
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.services.signal import SignalService
from etf_quant_lab.services.strategy import StrategyService
from etf_quant_lab.services.tasks import TaskRunService
from etf_quant_lab.services.universe import UniverseService
from etf_quant_lab.storage.duckdb import DuckDBDatabase, build_database
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBInstrumentRepository,
    DuckDBTradingCalendarRepository,
)
from etf_quant_lab.storage.signal import SignalRepository

DEFAULT_STRATEGY_VERSION = "1.0.0"


def build_strategy_registry() -> StrategyRegistry:
    """Register every released strategy version exactly once."""

    registry = StrategyRegistry()
    registry.register(TrendBaselineStrategy())
    registry.register(EtfRotationStrategy())
    return registry


def build_full_context(
    *,
    settings: AppSettings | None = None,
    initialize_paths: bool = True,
    setup_logging: bool = True,
) -> ApplicationContext:
    """Build the context with all repositories and services registered."""

    context = build_application_context(
        settings=settings,
        initialize_paths=initialize_paths,
        setup_logging=setup_logging,
    )
    resolved: AppSettings = context.settings
    database: DuckDBDatabase = build_database(resolved)
    database.migrate()
    ids = UlidGenerator()
    parquet_store = ParquetStore(resolved.data_dir)

    instruments = DuckDBInstrumentRepository(database)
    calendar = DuckDBTradingCalendarRepository(database)
    batches = DataBatchRepository(database, parquet_store)
    quality_reports = QualityReportRepository(database, ids)
    signals = SignalRepository(database, ids)

    strategy_service = StrategyService(build_strategy_registry())
    quality_service = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=quality_reports,
        id_generator=ids,
    )
    universe_service = UniverseService(instruments)
    enabled_symbols = tuple(
        instrument.symbol
        for instrument in instruments.list_all()
        if instrument.enabled
    )
    signal_service = SignalService(
        strategy_service=strategy_service,
        quality_service=quality_service,
        batch_repository=batches,
        calendar_repository=calendar,
        signal_repository=signals,
        id_generator=ids,
        universe_symbols=enabled_symbols,
    )
    paper_service = PaperAccountService(database, ids)
    task_service = TaskRunService(
        database,
        ids,
        lock_dir=resolved.data_dir / "locks",
        lock_timeout_seconds=resolved.lock_timeout_seconds,
    )

    context.register("database", database)
    context.register("parquet_store", parquet_store)
    context.register("instruments", instruments)
    context.register("calendar", calendar)
    context.register("batches", batches)
    context.register("signals", signals)
    context.register("universe", universe_service)
    context.register("strategy", strategy_service)
    context.register("quality", quality_service)
    context.register("signal", signal_service)
    context.register("paper", paper_service)
    context.register("tasks", task_service)
    return context


def default_strategy_id() -> StrategyId:
    """The strategy the daily pipeline runs when none is configured."""

    return StrategyId.ETF_ROTATION
