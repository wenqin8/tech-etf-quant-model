"""Unit tests for strategy contracts and the as-of market data view."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import DataSource, Exchange, SignalAction, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
    parameter_hash,
)
from etf_quant_lab.domain.market_view import FUTURE_DATA_ACCESS, MarketDataView

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def _bar(*, symbol: str = "510300.SH", trade_date: date, close: str = "4.000") -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE,
        open=price,
        high=price + Decimal("0.05"),
        low=price - Decimal("0.05"),
        close=price,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NJ1",
        ingested_at=INGESTED_AT,
    )


def test_parameter_spec_rejects_enum_without_choices() -> None:
    with pytest.raises(ValueError, match="choices"):
        ParameterSpec(name="freq", type="enum", default="WEEKLY")


def test_target_portfolio_requires_weights_to_sum_to_one() -> None:
    with pytest.raises(ValueError, match="equal 1"):
        TargetPortfolio(
            as_of_date=date(2026, 7, 10),
            strategy_id=StrategyId.ETF_ROTATION,
            version="1.0.0",
            allocations=(
                TargetAllocation(
                    symbol="510300.SH",
                    target_weight=Decimal("0.5"),
                    action=SignalAction.BUY,
                ),
            ),
            cash_weight=Decimal("0.3"),
        )


def test_target_portfolio_accepts_fully_allocated_state() -> None:
    portfolio = TargetPortfolio(
        as_of_date=date(2026, 7, 10),
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        allocations=(
            TargetAllocation(
                symbol="510300.SH",
                target_weight=Decimal("0.6"),
                action=SignalAction.BUY,
            ),
        ),
        cash_weight=Decimal("0.4"),
    )

    assert portfolio.cash_weight == Decimal("0.4")


def test_parameter_hash_is_order_independent_and_decimal_stable() -> None:
    first = parameter_hash({"top_n": 3, "max_weight": Decimal("0.35")})
    second = parameter_hash({"max_weight": Decimal("0.35"), "top_n": 3})

    assert first == second
    assert first.startswith("sha256:")


def test_market_view_rejects_future_dated_bar() -> None:
    with pytest.raises(DomainError) as excinfo:
        MarketDataView(
            as_of_date=date(2026, 7, 10),
            bars=(
                _bar(trade_date=date(2026, 7, 10)),
                _bar(trade_date=date(2026, 7, 13)),
            ),
        )

    assert excinfo.value.code == FUTURE_DATA_ACCESS


def test_market_view_history_is_sorted_and_tail_limited() -> None:
    view = MarketDataView(
        as_of_date=date(2026, 7, 13),
        bars=(
            _bar(trade_date=date(2026, 7, 13), close="4.030"),
            _bar(trade_date=date(2026, 7, 9), close="4.000"),
            _bar(trade_date=date(2026, 7, 10), close="4.010"),
        ),
    )

    history = view.history("510300.SH")
    assert [bar.trade_date for bar in history] == [
        date(2026, 7, 9),
        date(2026, 7, 10),
        date(2026, 7, 13),
    ]
    assert [bar.trade_date for bar in view.history("510300.SH", max_bars=2)] == [
        date(2026, 7, 10),
        date(2026, 7, 13),
    ]
    assert view.latest("510300.SH").trade_date == date(2026, 7, 13)
    assert view.bar_count("510300.SH") == 3
    assert view.symbols == ("510300.SH",)
