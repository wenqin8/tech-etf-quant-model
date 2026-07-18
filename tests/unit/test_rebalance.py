"""Unit tests for rebalance proposals: diffs, lot rounding and cash safety."""

from __future__ import annotations

from decimal import Decimal

import pytest

from etf_quant_lab.contracts.enums import CostScenario, OrderSide
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.domain.rebalance import (
    SKIP_BELOW_ONE_LOT,
    SKIP_CASH_EXHAUSTED,
    SKIP_NO_PRICE,
    build_rebalance_proposal,
)

NORMAL = CostModel(
    scenario=CostScenario.NORMAL,
    commission_rate=Decimal("0.00025"),
    minimum_commission=Decimal("5"),
    slippage_bps=Decimal("5"),
)
ZERO_COST = CostModel(
    scenario=CostScenario.IDEAL,
    commission_rate=Decimal("0"),
    minimum_commission=Decimal("0"),
    slippage_bps=Decimal("0"),
)
PRICES = {"510300.SH": Decimal("4.00"), "159915.SZ": Decimal("2.00")}
LOTS = {"510300.SH": 100, "159915.SZ": 100}


def test_buy_proposal_rounds_to_lot_and_predicts_cash() -> None:
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("0.5")},
        current_positions={},
        cash=Decimal("10000"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert len(proposal.trades) == 1
    trade = proposal.trades[0]
    # 50% of 10000 => 1250 shares raw => 1200 after lot rounding.
    assert trade.side is OrderSide.BUY
    assert trade.quantity == 1200
    assert proposal.predicted_cash == Decimal("10000") - Decimal("4800")
    assert trade.achieved_weight == Decimal("0.48")


def test_small_difference_below_one_lot_is_skipped() -> None:
    # Holding 1200 shares; target implies 1250 => diff 50 < one lot of 100.
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("0.5")},
        current_positions={"510300.SH": 1200},
        cash=Decimal("5200"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert proposal.trades == ()  # 1250 rounds down to 1200 = current, no trade


def test_full_exit_sells_everything() -> None:
    proposal = build_rebalance_proposal(
        target_weights={},
        current_positions={"510300.SH": 800, "159915.SZ": 500},
        cash=Decimal("100"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    sides = {(t.symbol, t.side) for t in proposal.trades}
    assert sides == {("510300.SH", OrderSide.SELL), ("159915.SZ", OrderSide.SELL)}
    assert proposal.predicted_cash == Decimal("100") + Decimal("3200") + Decimal("1000")


def test_zero_current_position_with_zero_target_is_noop() -> None:
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("0")},
        current_positions={},
        cash=Decimal("1000"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert proposal.trades == ()
    assert proposal.predicted_cash == Decimal("1000")


def test_target_weight_below_one_lot_reports_skip() -> None:
    # 1% of 1000 = 10 CNY => 2.5 shares of a 100-lot symbol: below one lot.
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("0.01")},
        current_positions={},
        cash=Decimal("1000"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert proposal.trades == ()
    assert any(skip.reason == SKIP_BELOW_ONE_LOT for skip in proposal.skipped)


def test_buys_never_drive_predicted_cash_negative() -> None:
    # Fees make the naive full-size buy unaffordable; the proposal must shrink it.
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("1.0")},
        current_positions={},
        cash=Decimal("4000"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=NORMAL,
    )

    assert proposal.predicted_cash >= 0
    assert len(proposal.trades) == 1
    assert proposal.trades[0].quantity < 1000  # shrunk below the naive 1000
    assert any(
        skip.reason == SKIP_CASH_EXHAUSTED and "reduced" in skip.detail
        for skip in proposal.skipped
    )


def test_sell_frees_cash_for_rotation_buy() -> None:
    # Rotate 510300 into 159915 with almost no starting cash.
    proposal = build_rebalance_proposal(
        target_weights={"159915.SZ": Decimal("0.9")},
        current_positions={"510300.SH": 1000},
        cash=Decimal("10"),
        reference_prices=PRICES,
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert [t.side for t in proposal.trades] == [OrderSide.SELL, OrderSide.BUY]
    assert proposal.predicted_cash >= 0
    buy = proposal.trades[1]
    assert buy.symbol == "159915.SZ"
    assert buy.quantity == 1800  # 0.9 * 4010 / 2.0 = 1804.5 -> 1800


def test_missing_reference_price_is_reported() -> None:
    proposal = build_rebalance_proposal(
        target_weights={"512100.SH": Decimal("0.5")},
        current_positions={},
        cash=Decimal("10000"),
        reference_prices=PRICES,  # no price for 512100.SH
        lot_sizes=LOTS,
        cost_model=ZERO_COST,
    )

    assert proposal.trades == ()
    assert any(skip.reason == SKIP_NO_PRICE for skip in proposal.skipped)


def test_invalid_target_weight_rejected() -> None:
    with pytest.raises(ValueError, match="out of range"):
        build_rebalance_proposal(
            target_weights={"510300.SH": Decimal("1.5")},
            current_positions={},
            cash=Decimal("10000"),
            reference_prices=PRICES,
            lot_sizes=LOTS,
            cost_model=ZERO_COST,
        )
