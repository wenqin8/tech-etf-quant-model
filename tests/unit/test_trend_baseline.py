"""Unit tests for the trend baseline strategy, including the golden sequence."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import DataSource, Exchange, SignalAction, StrategyId
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategies.trend_baseline import (
    REASON_TREND_UP,
    WARNING_NO_TREND,
    TrendBaselineStrategy,
)
from etf_quant_lab.domain.strategy import StrategyContext
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.strategy import StrategyService

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
PARAMS: dict[str, object] = {
    "fast_window": 3,
    "slow_window": 5,
    "maximum_position_weight": Decimal("0.6"),
    "minimum_cash_weight": Decimal("0.1"),
}


def _bar(*, symbol: str, trade_date: date, close: Decimal) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        open=close,
        high=close + Decimal("0.10"),
        low=max(close - Decimal("0.10"), Decimal("0.01")),
        close=close,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NM1",
        ingested_at=INGESTED_AT,
    )


def _series(symbol: str, closes: list[str], *, end: date) -> tuple[DailyBar, ...]:
    """Build consecutive daily bars ending at ``end`` from a close sequence."""

    start = end - timedelta(days=len(closes) - 1)
    return tuple(
        _bar(symbol=symbol, trade_date=start + timedelta(days=offset), close=Decimal(value))
        for offset, value in enumerate(closes)
    )


def _context(bars: tuple[DailyBar, ...], as_of: date, symbols: tuple[str, ...]) -> StrategyContext:
    return StrategyContext(
        as_of_date=as_of,
        universe_symbols=symbols,
        market_data=MarketDataView(as_of_date=as_of, bars=bars),
    )


def test_uptrend_symbol_is_bought_with_reason() -> None:
    # Rising closes: fast SMA(3) ends above slow SMA(5).
    bars = _series("510300.SH", ["4.0", "4.1", "4.2", "4.3", "4.4", "4.5"], end=date(2026, 7, 10))

    portfolio = TrendBaselineStrategy().generate_targets(
        _context(bars, date(2026, 7, 10), ("510300.SH",)),
        PARAMS,
    )

    assert [a.symbol for a in portfolio.allocations] == ["510300.SH"]
    allocation = portfolio.allocations[0]
    assert allocation.action is SignalAction.BUY
    assert allocation.reason_codes == (REASON_TREND_UP,)
    assert allocation.score is not None and allocation.score > 0
    # investable = 0.9, single symbol => min(0.6, 0.9) = 0.6; cash = 0.4.
    assert allocation.target_weight == Decimal("0.6")
    assert portfolio.cash_weight == Decimal("0.4")


def test_downtrend_yields_full_cash_with_warning() -> None:
    bars = _series("510300.SH", ["4.5", "4.4", "4.3", "4.2", "4.1", "4.0"], end=date(2026, 7, 10))

    portfolio = TrendBaselineStrategy().generate_targets(
        _context(bars, date(2026, 7, 10), ("510300.SH",)),
        PARAMS,
    )

    assert portfolio.allocations == ()
    assert portfolio.cash_weight == Decimal(1)
    assert WARNING_NO_TREND in portfolio.warnings


def test_warmup_shortfall_is_skipped_not_guessed() -> None:
    # Only 4 bars but slow_window=5: the symbol must be skipped entirely.
    bars = _series("510300.SH", ["4.0", "4.1", "4.2", "4.3"], end=date(2026, 7, 10))

    portfolio = TrendBaselineStrategy().generate_targets(
        _context(bars, date(2026, 7, 10), ("510300.SH",)),
        PARAMS,
    )

    assert portfolio.allocations == ()
    assert WARNING_NO_TREND in portfolio.warnings


def test_sparse_history_symbol_does_not_break_others() -> None:
    trending = _series(
        "510300.SH", ["4.0", "4.1", "4.2", "4.3", "4.4", "4.5"], end=date(2026, 7, 10)
    )
    sparse = _series("159915.SZ", ["3.0", "3.1"], end=date(2026, 7, 10))

    portfolio = TrendBaselineStrategy().generate_targets(
        _context(trending + sparse, date(2026, 7, 10), ("159915.SZ", "510300.SH")),
        PARAMS,
    )

    assert [a.symbol for a in portfolio.allocations] == ["510300.SH"]


def test_equal_weight_is_capped_and_cash_floor_respected() -> None:
    first = _series(
        "510300.SH", ["4.0", "4.1", "4.2", "4.3", "4.4", "4.5"], end=date(2026, 7, 10)
    )
    second = _series(
        "159915.SZ", ["3.0", "3.1", "3.2", "3.3", "3.4", "3.5"], end=date(2026, 7, 10)
    )

    portfolio = TrendBaselineStrategy().generate_targets(
        _context(first + second, date(2026, 7, 10), ("159915.SZ", "510300.SH")),
        PARAMS,
    )

    # investable = 0.9 over 2 symbols => 0.45 each, below the 0.6 cap.
    weights = {a.symbol: a.target_weight for a in portfolio.allocations}
    assert weights == {"159915.SZ": Decimal("0.45"), "510300.SH": Decimal("0.45")}
    assert portfolio.cash_weight == Decimal("0.1")


def test_fast_window_must_be_smaller_than_slow_window() -> None:
    bars = _series("510300.SH", ["4.0", "4.1", "4.2", "4.3", "4.4"], end=date(2026, 7, 10))
    bad_params = dict(PARAMS, fast_window=5, slow_window=5)

    with pytest.raises(ValueError, match="fast_window"):
        TrendBaselineStrategy().generate_targets(
            _context(bars, date(2026, 7, 10), ("510300.SH",)),
            bad_params,
        )


def test_golden_signal_sequence_on_fixed_sample() -> None:
    """Fixed sample must always produce the same buy/cash sequence (验收: 黄金序列)."""

    strategy = TrendBaselineStrategy()
    # 10 consecutive days: 5 falling then 5 rising closes.
    closes = ["4.4", "4.3", "4.2", "4.1", "4.0", "4.1", "4.25", "4.4", "4.55", "4.7"]
    end = date(2026, 7, 10)
    bars = _series("510300.SH", closes, end=end)
    start = end - timedelta(days=len(closes) - 1)

    decisions: list[str] = []
    for offset in range(4, len(closes)):  # from the first date with 5 bars of history
        as_of = start + timedelta(days=offset)
        visible = tuple(bar for bar in bars if bar.trade_date <= as_of)
        portfolio = strategy.generate_targets(
            _context(visible, as_of, ("510300.SH",)),
            PARAMS,
        )
        decisions.append("BUY" if portfolio.allocations else "CASH")

    # Golden sequence: downtrend tail keeps cash; the rebound flips fast over slow
    # from the third rising close onward.
    assert decisions == ["CASH", "CASH", "CASH", "BUY", "BUY", "BUY"]


def test_registers_into_service_without_service_changes() -> None:
    """验收: 新策略注册后服务层无需改动即可运行。"""

    registry = StrategyRegistry()
    registry.register(TrendBaselineStrategy())
    service = StrategyService(registry)

    descriptors = service.list_strategies()
    assert [d.strategy_id for d in descriptors] == [StrategyId.TREND_BASELINE]

    bars = _series("510300.SH", ["4.0", "4.1", "4.2", "4.3", "4.4", "4.5"], end=date(2026, 7, 10))
    portfolio = service.generate_targets(
        strategy_id=StrategyId.TREND_BASELINE,
        version="1.0.0",
        parameters={"fast_window": 3, "slow_window": 5},
        as_of_date=date(2026, 7, 10),
        universe_symbols=("510300.SH",),
        market_data=MarketDataView(as_of_date=date(2026, 7, 10), bars=bars),
    )

    assert portfolio.allocations
    assert portfolio.allocations[0].reason_codes == (REASON_TREND_UP,)


def test_code_hash_and_description_are_stable() -> None:
    strategy = TrendBaselineStrategy()

    assert strategy.code_hash() == TrendBaselineStrategy().code_hash()
    assert len(strategy.code_hash()) == 64
    assert "趋势基准策略" in strategy.describe()
