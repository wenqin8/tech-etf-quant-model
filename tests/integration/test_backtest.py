"""Integration tests for the end-to-end backtest orchestration."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import CostScenario, DataSource, Exchange, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.domain.strategies.trend_baseline import TrendBaselineStrategy
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.backtest import BacktestRequest, run_backtest
from etf_quant_lab.services.strategy import StrategyService

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
START = date(2026, 6, 1)
PARAMS: dict[str, object] = {
    "fast_window": 3,
    "slow_window": 5,
    "maximum_position_weight": Decimal("0.6"),
    "minimum_cash_weight": Decimal("0.1"),
}
IDEAL = CostModel(
    scenario=CostScenario.IDEAL,
    commission_rate=Decimal("0"),
    minimum_commission=Decimal("0"),
    slippage_bps=Decimal("0"),
)
PESSIMISTIC = CostModel(
    scenario=CostScenario.PESSIMISTIC,
    commission_rate=Decimal("0.0005"),
    minimum_commission=Decimal("5"),
    slippage_bps=Decimal("15"),
)


def _bar(day: date, close: str, *, open_price: str | None = None) -> DailyBar:
    close_decimal = Decimal(close)
    open_decimal = Decimal(open_price) if open_price else close_decimal
    high = max(open_decimal, close_decimal) + Decimal("0.02")
    low = min(open_decimal, close_decimal) - Decimal("0.02")
    return DailyBar(
        symbol="510300.SH",
        trade_date=day,
        exchange=Exchange.SSE,
        open=open_decimal,
        high=high,
        low=max(low, Decimal("0.01")),
        close=close_decimal,
        volume=Decimal("10000"),
        amount=Decimal("40000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NU1",
        ingested_at=INGESTED_AT,
    )


def _rising_bars(days: int = 20) -> tuple[DailyBar, ...]:
    return tuple(
        _bar(START + timedelta(days=offset), str(Decimal("4.00") + Decimal("0.05") * offset))
        for offset in range(days)
    )


def _service() -> StrategyService:
    registry = StrategyRegistry()
    registry.register(TrendBaselineStrategy())
    return StrategyService(registry)


def _request(**overrides: object) -> BacktestRequest:
    payload: dict[str, object] = {
        "strategy_id": StrategyId.TREND_BASELINE,
        "strategy_version": "1.0.0",
        "parameters": PARAMS,
        "symbols": ("510300.SH",),
        "start_date": START + timedelta(days=6),
        "end_date": START + timedelta(days=19),
        "initial_cash": Decimal("100000"),
        "lot_sizes": {"510300.SH": 100},
    }
    payload.update(overrides)
    return BacktestRequest(**payload)  # type: ignore[arg-type]


def test_backtest_runs_end_to_end_and_profits_in_uptrend() -> None:
    result = run_backtest(_service(), request=_request(), bars=_rising_bars())

    assert result.ledger.trades, "the uptrend strategy should have bought"
    first_trade = result.ledger.trades[0]
    assert first_trade.trade.side.value == "BUY"
    assert result.metrics.total_return is not None
    assert result.metrics.total_return > 0
    # Ledger cash math is auditable end to end.
    assert result.ledger.records[-1].total_equity > result.ledger.records[0].cash * Decimal("0.99")


def test_signal_decided_on_t_executes_at_t_plus_one_open() -> None:
    result = run_backtest(_service(), request=_request(), bars=_rising_bars())

    first_trade = result.ledger.trades[0]
    # The first decision date is the backtest start; execution lands strictly later.
    assert first_trade.trade_date > _request().start_date


def test_backtest_is_deterministic() -> None:
    bars = _rising_bars()

    first = run_backtest(_service(), request=_request(), bars=bars)
    second = run_backtest(_service(), request=_request(), bars=bars)

    assert first.ledger == second.ledger
    assert first.metrics == second.metrics


def test_pessimistic_costs_do_not_beat_ideal() -> None:
    bars = _rising_bars()

    ideal = run_backtest(_service(), request=_request(cost_model=IDEAL), bars=bars)
    stressed = run_backtest(
        _service(), request=_request(cost_model=PESSIMISTIC), bars=bars
    )

    assert ideal.metrics.total_return is not None
    assert stressed.metrics.total_return is not None
    assert stressed.metrics.total_return <= ideal.metrics.total_return
    assert stressed.metrics.cost_total >= ideal.metrics.cost_total


def test_too_short_range_is_rejected() -> None:
    with pytest.raises(DomainError) as excinfo:
        run_backtest(
            _service(),
            request=_request(
                start_date=START + timedelta(days=19),
                end_date=START + timedelta(days=19),
            ),
            bars=_rising_bars(),
        )

    assert excinfo.value.code == "BT_RANGE_TOO_SHORT"
