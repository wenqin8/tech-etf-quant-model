"""Error-branch tests pushing performance/rebalance/validation domains past 90%."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.enums import CostScenario
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.contracts.performance import DailyPortfolioRecord, PortfolioLedger
from etf_quant_lab.contracts.validation import DateRange, WalkForwardWindow
from etf_quant_lab.domain.performance import compute_metrics, mark_to_close
from etf_quant_lab.domain.rebalance import build_rebalance_proposal
from etf_quant_lab.domain.validation import (
    expand_parameter_grid,
    generate_walk_forward_windows,
    run_walk_forward,
)

START = date(2026, 7, 6)
ZERO_COST = CostModel(
    scenario=CostScenario.IDEAL,
    commission_rate=Decimal("0"),
    minimum_commission=Decimal("0"),
    slippage_bps=Decimal("0"),
)


def _ledger(equities: list[str]) -> PortfolioLedger:
    records = tuple(
        DailyPortfolioRecord(
            trade_date=START + timedelta(days=index),
            cash=Decimal(value),
            positions={},
            market_value=Decimal(0),
            total_equity=Decimal(value),
        )
        for index, value in enumerate(equities)
    )
    return PortfolioLedger(
        records=records, trades=(), skipped=(), initial_cash=Decimal(equities[0])
    )


def test_metrics_reject_empty_ledger_and_bad_annualization() -> None:
    with pytest.raises(ValueError, match="at least one record"):
        compute_metrics(
            PortfolioLedger(records=(), trades=(), skipped=(), initial_cash=Decimal(1))
        )
    with pytest.raises(ValueError, match="annualization_days"):
        compute_metrics(_ledger(["100", "101"]), annualization_days=0)


def test_ledger_rejects_unordered_or_duplicate_records() -> None:
    first = DailyPortfolioRecord(
        trade_date=START,
        cash=Decimal("100"),
        positions={},
        market_value=Decimal(0),
        total_equity=Decimal("100"),
    )
    earlier = DailyPortfolioRecord(
        trade_date=START - timedelta(days=1),
        cash=Decimal("100"),
        positions={},
        market_value=Decimal(0),
        total_equity=Decimal("100"),
    )
    with pytest.raises(ValueError, match="ordered"):
        PortfolioLedger(
            records=(first, earlier), trades=(), skipped=(), initial_cash=Decimal("100")
        )
    with pytest.raises(ValueError, match="repeat"):
        PortfolioLedger(
            records=(first, first), trades=(), skipped=(), initial_cash=Decimal("100")
        )


def test_mark_to_close_rejects_non_positive_price() -> None:
    with pytest.raises(ValueError, match="positive"):
        mark_to_close(
            START,
            Decimal("100"),
            {"510300.SH": 100},
            {"510300.SH": Decimal("0")},
        )


def test_total_loss_annualizes_to_minus_one() -> None:
    # Equity collapsing to near zero: growth <= 0 path returns -1.0.
    metrics = compute_metrics(_ledger(["100", "0.0001"]))
    assert metrics.annual_return == pytest.approx(-1.0)


def test_rebalance_rejects_negative_cash_input() -> None:
    with pytest.raises(ValueError, match="cash"):
        build_rebalance_proposal(
            target_weights={},
            current_positions={},
            cash=Decimal("-1"),
            reference_prices={},
            lot_sizes={},
            cost_model=ZERO_COST,
        )


def test_rebalance_skips_symbol_without_price_even_when_held() -> None:
    proposal = build_rebalance_proposal(
        target_weights={"510300.SH": Decimal("0.5")},
        current_positions={"510300.SH": 100},
        cash=Decimal("1000"),
        reference_prices={},  # neither target nor holding has a price
        lot_sizes={"510300.SH": 100},
        cost_model=ZERO_COST,
    )

    assert proposal.trades == ()
    assert proposal.skipped


def test_grid_expansion_rejects_empty_value_list() -> None:
    with pytest.raises(ValueError, match="at least one value"):
        expand_parameter_grid({"lookback": []})


def test_walk_forward_rejects_unsorted_dates_and_empty_windows() -> None:
    dates = [START + timedelta(days=offset) for offset in range(12)]
    with pytest.raises(ValueError, match="strictly increasing"):
        generate_walk_forward_windows(
            list(reversed(dates)), train_bars=4, test_bars=2, step_bars=2
        )
    with pytest.raises(ValueError, match="positive"):
        generate_walk_forward_windows(dates, train_bars=0, test_bars=2, step_bars=2)
    with pytest.raises(ValueError, match="not be empty"):
        run_walk_forward(
            lambda parameters, period: {"sharpe_ratio": 1.0},
            parameter_grid={},
            windows=[],
            selection_metric="sharpe_ratio",
        )


def test_walk_forward_window_guards() -> None:
    train = DateRange(start=START, end=START + timedelta(days=4))
    with pytest.raises(ValueError, match="index"):
        WalkForwardWindow(
            index=-1,
            train=train,
            test=DateRange(start=START + timedelta(days=5), end=START + timedelta(days=7)),
        )
    with pytest.raises(ValueError, match="overlap"):
        WalkForwardWindow(index=0, train=train, test=train)
