"""Integration tests for idempotent, quality-gated daily signal generation."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    RiskState,
    SignalAction,
    StrategyId,
)
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.signal import GenerateDailySignalRequest, SignalStatus
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.domain.strategies.trend_baseline import TrendBaselineStrategy
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.services.signal import SignalService
from etf_quant_lab.services.strategy import StrategyService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)
from etf_quant_lab.storage.signal import SignalRepository

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
GENERATED_AT = datetime(2026, 7, 13, 16, 40, tzinfo=UTC)
BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NP1"
CAL_BATCH = "01K0D7F7P6XQ4M2Z8H9B3C5NP9"
TRADE_DATE = date(2026, 7, 13)
UNIVERSE = ("510300.SH",)
PARAMS: dict[str, object] = {"fast_window": 3, "slow_window": 5}


def _bar(trade_date: date, close: str) -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        symbol="510300.SH",
        trade_date=trade_date,
        exchange=Exchange.SSE,
        open=price,
        high=price + Decimal("0.05"),
        low=price - Decimal("0.05"),
        close=price,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id=BATCH_ID,
        ingested_at=FETCHED_AT,
    )


def _service(tmp_path: Path, *, latest_bar_date: date = TRADE_DATE) -> SignalService:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    batches = DataBatchRepository(database, store)
    calendar = DuckDBTradingCalendarRepository(database)

    # Calendar: 8 consecutive open days ending at TRADE_DATE.
    days = [latest_bar_date - timedelta(days=offset) for offset in range(7, -1, -1)]
    days.append(TRADE_DATE)
    calendar.upsert_many(
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
            for day in sorted(set(days))
        )
    )

    # Rising closes so the trend strategy holds the symbol.
    closes = ["4.00", "4.05", "4.10", "4.15", "4.20", "4.25"]
    bar_dates = [latest_bar_date - timedelta(days=offset) for offset in range(5, -1, -1)]
    bars = tuple(_bar(day, close) for day, close in zip(bar_dates, closes, strict=True))
    batches.create(
        DataBatch(
            batch_id=BATCH_ID,
            provider=DataSource.TUSHARE,
            dataset="daily_bars",
            status=DataBatchStatus.FETCHING,
            fetched_at=FETCHED_AT,
            schema_version="daily_bar_v1",
        )
    )
    batches.stage_files(BATCH_ID, store.write_canonical_daily_bars(bars))
    batches.activate(BATCH_ID)

    registry = StrategyRegistry()
    registry.register(TrendBaselineStrategy())
    strategy_service = StrategyService(registry)
    quality_service = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=QualityReportRepository(database, UlidGenerator()),
        id_generator=UlidGenerator(),
    )
    return SignalService(
        strategy_service=strategy_service,
        quality_service=quality_service,
        batch_repository=batches,
        calendar_repository=calendar,
        signal_repository=SignalRepository(database, UlidGenerator()),
        id_generator=UlidGenerator(),
        universe_symbols=UNIVERSE,
        clock=lambda: GENERATED_AT,
    )


def _request(**overrides: object) -> GenerateDailySignalRequest:
    payload: dict[str, object] = {
        "trade_date": TRADE_DATE,
        "strategy_id": StrategyId.TREND_BASELINE,
        "strategy_version": "1.0.0",
        "parameters": PARAMS,
    }
    payload.update(overrides)
    return GenerateDailySignalRequest(**payload)  # type: ignore[arg-type]


def test_valid_signal_contains_targets_and_reference_close(tmp_path: Path) -> None:
    service = _service(tmp_path)

    batch = service.generate_daily(_request())

    assert batch.status == SignalStatus.VALID
    assert batch.risk_state is RiskState.NORMAL
    assert batch.data_as_of == TRADE_DATE
    assert len(batch.items) == 1
    item = batch.items[0]
    assert item.symbol == "510300.SH"
    assert item.action is SignalAction.BUY
    assert item.reference_close == Decimal("4.25")
    assert item.reason_codes
    invested = sum((entry.target_weight for entry in batch.items), Decimal(0))
    assert invested + batch.target_cash_weight == Decimal(1)


def test_same_context_returns_same_signal(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.generate_daily(_request())
    second = service.generate_daily(_request())

    assert first.signal_id == second.signal_id
    assert first.idempotency_key == second.idempotency_key


def test_different_parameters_produce_new_signal(tmp_path: Path) -> None:
    service = _service(tmp_path)

    first = service.generate_daily(_request())
    second = service.generate_daily(
        _request(parameters={"fast_window": 2, "slow_window": 5})
    )

    assert first.signal_id != second.signal_id


def test_non_trading_day_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(DomainError) as excinfo:
        service.generate_daily(_request(trade_date=date(2026, 7, 19)))

    assert excinfo.value.code == "SIG_NOT_TRADING_DAY"


def test_stale_data_produces_blocked_signal(tmp_path: Path) -> None:
    # Latest bar is 3 days before the requested trade date.
    service = _service(tmp_path, latest_bar_date=TRADE_DATE - timedelta(days=3))

    batch = service.generate_daily(_request())

    assert batch.status == SignalStatus.BLOCKED
    assert batch.risk_state is RiskState.BLOCKED
    assert batch.items == ()
    assert batch.target_cash_weight == Decimal(1)
    assert batch.blocked_reason is not None
    # Blocked outcomes are idempotent too.
    again = service.generate_daily(_request())
    assert again.signal_id == batch.signal_id


def test_exit_item_generated_for_position_not_in_target(tmp_path: Path) -> None:
    service = _service(tmp_path)

    batch = service.generate_daily(
        _request(
            parameters={"fast_window": 3, "slow_window": 5},
            current_weights={"159915.SZ": Decimal("0.2")},
        )
    )

    exits = [item for item in batch.items if item.action is SignalAction.SELL]
    assert len(exits) == 1
    assert exits[0].symbol == "159915.SZ"
    assert exits[0].weight_delta == Decimal("-0.2")
    assert exits[0].reason_codes == ("EXIT_NOT_IN_TARGET",)


def test_get_returns_stored_batch_and_missing_id_fails(tmp_path: Path) -> None:
    service = _service(tmp_path)
    created = service.generate_daily(_request())

    fetched = service.get(created.signal_id)
    assert fetched.signal_id == created.signal_id
    # DuckDB DECIMAL columns quantize scale (0.25 -> 0.250000), so compare
    # numerically rather than by dataclass equality.
    assert len(fetched.items) == len(created.items)
    for stored, original in zip(fetched.items, created.items, strict=True):
        assert stored.symbol == original.symbol
        assert stored.action is original.action
        assert stored.target_weight == original.target_weight
        assert stored.weight_delta == original.weight_delta
        assert stored.reference_close == original.reference_close
        assert stored.reason_codes == original.reason_codes

    with pytest.raises(DomainError, match="信号不存在"):
        service.get("01K0D7F7P6XQ4M2Z8H9B3C5NZZ")
