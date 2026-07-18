"""Unit tests for the next-open execution engine and cost scenarios."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from etf_quant_lab.contracts.enums import CostScenario, OrderSide
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.execution import (
    SKIP_INSUFFICIENT_CASH,
    SKIP_NO_QUOTE,
    SKIP_SUSPENDED,
    CostModel,
    MarketQuote,
    PortfolioState,
)
from etf_quant_lab.domain.execution import execute_rebalance
from etf_quant_lab.services.costs import load_cost_scenarios

IDEAL = CostModel(
    scenario=CostScenario.IDEAL,
    commission_rate=Decimal("0.0001"),
    minimum_commission=Decimal("0"),
    slippage_bps=Decimal("0"),
)
NORMAL = CostModel(
    scenario=CostScenario.NORMAL,
    commission_rate=Decimal("0.00025"),
    minimum_commission=Decimal("5"),
    slippage_bps=Decimal("5"),
)
PESSIMISTIC = CostModel(
    scenario=CostScenario.PESSIMISTIC,
    commission_rate=Decimal("0.0005"),
    minimum_commission=Decimal("5"),
    slippage_bps=Decimal("15"),
)

QUOTES = {
    "510300.SH": MarketQuote(symbol="510300.SH", open_price=Decimal("4.000")),
    "159915.SZ": MarketQuote(symbol="159915.SZ", open_price=Decimal("2.000")),
}
LOTS = {"510300.SH": 100, "159915.SZ": 100}


def test_buy_rounds_down_to_lot_size_and_reserves_cash() -> None:
    result = execute_rebalance(
        target_weights={"510300.SH": Decimal("0.5")},
        state=PortfolioState(cash=Decimal("10000")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=IDEAL,
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    # 50% of 10000 = 5000 => 1250 shares raw => 1200 after lot rounding.
    assert trade.quantity == 1200
    assert trade.side is OrderSide.BUY
    assert result.state_after.positions == {"510300.SH": 1200}
    assert result.state_after.cash > 0


def test_minimum_commission_applies_to_small_orders() -> None:
    result = execute_rebalance(
        target_weights={"159915.SZ": Decimal("0.1")},
        state=PortfolioState(cash=Decimal("2500")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=NORMAL,
    )

    trade = result.trades[0]
    # Gross ~= 100 * 2.001 = 200.1; rate commission would be ~0.05 => minimum 5 applies.
    assert trade.commission == Decimal("5")
    assert trade.total_cost >= Decimal("5")


def test_insufficient_cash_reduces_quantity_and_records_skip() -> None:
    # Target wants ~1250 shares (5000/4) but cash only covers ~300 shares.
    result = execute_rebalance(
        target_weights={"510300.SH": Decimal("0.5")},
        state=PortfolioState(cash=Decimal("1300")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=NORMAL,
        total_equity=Decimal("10000"),
    )

    assert result.trades, "an affordable partial fill is expected"
    trade = result.trades[0]
    assert trade.quantity == 300
    assert result.state_after.cash >= 0
    assert any(
        skip.reason == SKIP_INSUFFICIENT_CASH and "reduced" in skip.detail
        for skip in result.skipped
    )


def test_completely_unaffordable_buy_is_skipped() -> None:
    result = execute_rebalance(
        target_weights={"510300.SH": Decimal("0.5")},
        state=PortfolioState(cash=Decimal("100")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=NORMAL,
        total_equity=Decimal("10000"),
    )

    assert result.trades == ()
    assert [skip.reason for skip in result.skipped] == [SKIP_INSUFFICIENT_CASH]
    assert result.state_after.cash == Decimal("100")


def test_suspended_symbol_cannot_trade_but_others_proceed() -> None:
    quotes = dict(QUOTES)
    quotes["159915.SZ"] = MarketQuote(
        symbol="159915.SZ", open_price=Decimal("2.000"), is_suspended=True
    )
    state = PortfolioState(
        cash=Decimal("2000"),
        positions={"159915.SZ": 500, "510300.SH": 500},
    )

    result = execute_rebalance(
        target_weights={},  # full exit everywhere
        state=state,
        quotes=quotes,
        lot_sizes=LOTS,
        cost_model=IDEAL,
    )

    # The suspended position is held, the live one is sold.
    sold = {trade.symbol for trade in result.trades}
    assert sold == {"510300.SH"}
    assert any(skip.reason == SKIP_SUSPENDED for skip in result.skipped)
    assert result.state_after.positions["159915.SZ"] == 500


def test_missing_quote_is_reported_not_guessed() -> None:
    state = PortfolioState(cash=Decimal("0"), positions={"512100.SH": 300})

    result = execute_rebalance(
        target_weights={},
        state=state,
        quotes=QUOTES,  # no quote for 512100.SH
        lot_sizes=LOTS,
        cost_model=IDEAL,
    )

    assert result.trades == ()
    assert [skip.reason for skip in result.skipped] == [SKIP_NO_QUOTE]
    assert result.state_after.positions["512100.SH"] == 300


def test_sells_settle_before_buys_fund_rotation() -> None:
    # Rotate everything from 510300.SH into 159915.SZ with almost no spare cash.
    state = PortfolioState(cash=Decimal("10"), positions={"510300.SH": 1000})

    result = execute_rebalance(
        target_weights={"159915.SZ": Decimal("0.9")},
        state=state,
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=IDEAL,
    )

    sides = [(trade.symbol, trade.side) for trade in result.trades]
    assert sides[0] == ("510300.SH", OrderSide.SELL)
    assert sides[1] == ("159915.SZ", OrderSide.BUY)
    assert result.state_after.cash >= 0
    assert result.state_after.positions["159915.SZ"] > 0


def test_cost_breakdown_reconciles_with_cash_delta() -> None:
    result = execute_rebalance(
        target_weights={"510300.SH": Decimal("0.5")},
        state=PortfolioState(cash=Decimal("10000")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=PESSIMISTIC,
    )

    trade = result.trades[0]
    # Every cost is auditable: gross = qty * price, cash covers gross + fees.
    assert trade.gross_amount == trade.executed_price * trade.quantity
    assert -trade.cash_delta == trade.gross_amount + trade.commission + trade.other_cost
    assert trade.slippage_cost == (
        abs(trade.executed_price - trade.reference_price) * trade.quantity
    )
    # Buy slippage pushes the executed price above the open reference.
    assert trade.executed_price > trade.reference_price


def test_pessimistic_costs_never_beat_ideal() -> None:
    def run(cost_model: CostModel) -> Decimal:
        result = execute_rebalance(
            target_weights={"510300.SH": Decimal("0.6"), "159915.SZ": Decimal("0.3")},
            state=PortfolioState(cash=Decimal("100000")),
            quotes=QUOTES,
            lot_sizes=LOTS,
            cost_model=cost_model,
        )
        return result.total_cost

    ideal_cost = run(IDEAL)
    normal_cost = run(NORMAL)
    pessimistic_cost = run(PESSIMISTIC)

    assert ideal_cost <= normal_cost <= pessimistic_cost


def test_zero_equity_returns_noop() -> None:
    result = execute_rebalance(
        target_weights={"510300.SH": Decimal("1")},
        state=PortfolioState(cash=Decimal("0")),
        quotes=QUOTES,
        lot_sizes=LOTS,
        cost_model=IDEAL,
    )

    assert result.trades == ()
    assert result.state_after == result.state_before


def test_load_cost_scenarios_from_project_config() -> None:
    models = load_cost_scenarios(Path("config/cost_scenarios.yaml"))

    assert set(models) == {
        CostScenario.IDEAL,
        CostScenario.NORMAL,
        CostScenario.PESSIMISTIC,
    }
    assert models[CostScenario.NORMAL].minimum_commission == Decimal("5.0")
    assert models[CostScenario.PESSIMISTIC].slippage_bps >= models[CostScenario.IDEAL].slippage_bps


def test_load_cost_scenarios_rejects_inverted_severity(tmp_path: Path) -> None:
    bad = tmp_path / "cost_scenarios.yaml"
    bad.write_text(
        """
version: 1
currency: CNY
scenarios:
  ideal:
    commission_rate: 0.001
    minimum_commission: 5.0
    slippage_bps: 20.0
  normal:
    commission_rate: 0.00025
    minimum_commission: 5.0
    slippage_bps: 5.0
  pessimistic:
    commission_rate: 0.0001
    minimum_commission: 0.0
    slippage_bps: 0.0
""",
        encoding="utf-8",
    )

    with pytest.raises(DomainError, match="悲观"):
        load_cost_scenarios(bad)
