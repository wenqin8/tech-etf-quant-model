"""Unit tests for the ETF momentum rotation strategy."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import DataSource, Exchange, SignalAction, StrategyId
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategies.etf_rotation import (
    REASON_ABOVE_TREND_FILTER,
    REASON_MOMENTUM_TOP_RANK,
    WARNING_INSUFFICIENT_CANDIDATES,
    WARNING_NO_CANDIDATE,
    EtfRotationStrategy,
)
from etf_quant_lab.domain.strategy import StrategyContext
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.strategy import StrategyService

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
END = date(2026, 7, 10)
PARAMS: dict[str, object] = {
    "momentum_window_short": 3,
    "momentum_window_medium": 5,
    "momentum_window_long": 8,
    "volatility_window": 5,
    "trend_filter_days": 5,
    "holdings_count": 2,
    "maximum_position_weight": Decimal("0.35"),
    "minimum_cash_weight": Decimal("0.10"),
    "cash_when_all_filtered": True,
}


def _bar(*, symbol: str, trade_date: date, close: Decimal) -> DailyBar:
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        open=close,
        high=close + Decimal("0.20"),
        low=max(close - Decimal("0.20"), Decimal("0.01")),
        close=close,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NN1",
        ingested_at=INGESTED_AT,
    )


def _series(symbol: str, closes: list[str], *, end: date = END) -> tuple[DailyBar, ...]:
    start = end - timedelta(days=len(closes) - 1)
    return tuple(
        _bar(symbol=symbol, trade_date=start + timedelta(days=offset), close=Decimal(value))
        for offset, value in enumerate(closes)
    )


def _context(bars: tuple[DailyBar, ...], symbols: tuple[str, ...]) -> StrategyContext:
    return StrategyContext(
        as_of_date=END,
        universe_symbols=symbols,
        market_data=MarketDataView(as_of_date=END, bars=bars),
    )


# 10-close rising and flat-ish sequences used across the ranking tests.
_STRONG = ["4.0", "4.05", "4.2", "4.15", "4.4", "4.5", "4.7", "4.85", "5.1", "5.4"]
_MILD = ["3.0", "3.02", "3.01", "3.05", "3.04", "3.08", "3.07", "3.10", "3.12", "3.15"]
_DECLINE = ["5.0", "4.9", "4.8", "4.7", "4.6", "4.5", "4.4", "4.3", "4.2", "4.1"]


def test_top_n_selected_with_scores_and_reasons() -> None:
    bars = (
        _series("510300.SH", _STRONG)
        + _series("159915.SZ", _MILD)
        + _series("512100.SH", _DECLINE)
    )

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("159915.SZ", "510300.SH", "512100.SH")),
        PARAMS,
    )

    symbols = [a.symbol for a in portfolio.allocations]
    # The declining ETF fails the trend filter; the two uptrenders are held.
    assert symbols == ["159915.SZ", "510300.SH"]
    for allocation in portfolio.allocations:
        assert allocation.action is SignalAction.BUY
        assert allocation.score is not None
        assert REASON_MOMENTUM_TOP_RANK in allocation.reason_codes
        assert REASON_ABOVE_TREND_FILTER in allocation.reason_codes


def test_per_symbol_cap_and_cash_floor() -> None:
    bars = _series("510300.SH", _STRONG) + _series("159915.SZ", _MILD)

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("159915.SZ", "510300.SH")),
        PARAMS,
    )

    # investable 0.9 / 2 = 0.45, capped at 0.35 => cash 0.30.
    weights = {a.symbol: a.target_weight for a in portfolio.allocations}
    assert weights == {"159915.SZ": Decimal("0.35"), "510300.SH": Decimal("0.35")}
    assert portfolio.cash_weight == Decimal("0.30")


def test_insufficient_candidates_warns_but_still_allocates() -> None:
    bars = _series("510300.SH", _STRONG) + _series("512100.SH", _DECLINE)

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("510300.SH", "512100.SH")),
        PARAMS,
    )

    assert [a.symbol for a in portfolio.allocations] == ["510300.SH"]
    assert WARNING_INSUFFICIENT_CANDIDATES in portfolio.warnings


def test_all_filtered_defaults_to_full_cash() -> None:
    bars = _series("510300.SH", _DECLINE) + _series("512100.SH", _DECLINE)

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("510300.SH", "512100.SH")),
        PARAMS,
    )

    assert portfolio.allocations == ()
    assert portfolio.cash_weight == Decimal(1)
    assert WARNING_NO_CANDIDATE in portfolio.warnings


def test_cash_when_all_filtered_false_falls_back_to_momentum() -> None:
    bars = _series("510300.SH", _DECLINE) + _series("512100.SH", _DECLINE)
    params = dict(PARAMS, cash_when_all_filtered=False)

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("510300.SH", "512100.SH")),
        params,
    )

    # Trend gate opted out: momentum ranking still yields holdings plus a warning.
    assert portfolio.allocations
    assert "TREND_FILTER_BYPASSED" in portfolio.warnings


def test_tie_breaks_by_symbol_deterministically() -> None:
    # Two identical series produce identical scores; ranking must break ties
    # by symbol so the selection is stable regardless of input order.
    bars = _series("510300.SH", _STRONG) + _series("159915.SZ", _STRONG)
    params = dict(PARAMS, holdings_count=1)

    ordered = EtfRotationStrategy().generate_targets(
        _context(bars, ("159915.SZ", "510300.SH")),
        params,
    )
    reversed_ctx = StrategyContext(
        as_of_date=END,
        universe_symbols=("510300.SH", "159915.SZ"),
        market_data=MarketDataView(as_of_date=END, bars=tuple(reversed(bars))),
    )
    shuffled = EtfRotationStrategy().generate_targets(reversed_ctx, params)

    assert [a.symbol for a in ordered.allocations] == ["159915.SZ"]
    assert ordered.allocations[0].symbol == shuffled.allocations[0].symbol


def test_warmup_shortfall_skips_symbol() -> None:
    bars = _series("510300.SH", ["4.0", "4.1", "4.2"])  # far fewer than warmup

    portfolio = EtfRotationStrategy().generate_targets(
        _context(bars, ("510300.SH",)),
        PARAMS,
    )

    assert portfolio.allocations == ()
    assert WARNING_NO_CANDIDATE in portfolio.warnings


def test_registers_and_runs_through_service() -> None:
    registry = StrategyRegistry()
    registry.register(EtfRotationStrategy())
    service = StrategyService(registry)

    assert [d.strategy_id for d in service.list_strategies()] == [StrategyId.ETF_ROTATION]

    bars = _series("510300.SH", _STRONG) + _series("159915.SZ", _MILD)
    portfolio = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=PARAMS,
        as_of_date=END,
        universe_symbols=("159915.SZ", "510300.SH"),
        market_data=MarketDataView(as_of_date=END, bars=bars),
    )

    invested = sum((a.target_weight for a in portfolio.allocations), Decimal(0))
    assert invested + portfolio.cash_weight == Decimal(1)


def test_code_hash_and_description_stable() -> None:
    strategy = EtfRotationStrategy()

    assert strategy.code_hash() == EtfRotationStrategy().code_hash()
    assert "轮动" in strategy.describe()


def test_bad_parameter_type_rejected() -> None:
    bars = _series("510300.SH", _STRONG)
    params = dict(PARAMS, holdings_count="three")

    with pytest.raises(ValueError, match="holdings_count"):
        EtfRotationStrategy().generate_targets(_context(bars, ("510300.SH",)), params)
