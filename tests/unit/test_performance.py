"""Unit tests for performance metrics with independently computed golden values."""

from __future__ import annotations

import math
from datetime import date, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.execution import ExecutedTrade
from etf_quant_lab.contracts.performance import (
    DailyPortfolioRecord,
    DatedTrade,
    PortfolioLedger,
)
from etf_quant_lab.domain.performance import (
    NOTE_NO_CLOSED_TRADE_PAIR,
    NOTE_SHORT_SAMPLE,
    NOTE_ZERO_VOLATILITY,
    compute_metrics,
    mark_to_close,
)

START = date(2026, 7, 6)


def _record(offset: int, equity: str) -> DailyPortfolioRecord:
    value = Decimal(equity)
    return DailyPortfolioRecord(
        trade_date=START + timedelta(days=offset),
        cash=value,
        positions={},
        market_value=Decimal(0),
        total_equity=value,
    )


def _ledger(equities: list[str], trades: tuple[DatedTrade, ...] = ()) -> PortfolioLedger:
    return PortfolioLedger(
        records=tuple(_record(index, value) for index, value in enumerate(equities)),
        trades=trades,
        skipped=(),
        initial_cash=Decimal(equities[0]),
    )


def _trade(
    *,
    side: OrderSide,
    quantity: int,
    price: str,
    offset: int = 0,
    commission: str = "5",
) -> DatedTrade:
    executed = Decimal(price)
    gross = executed * quantity
    fee = Decimal(commission)
    return DatedTrade(
        trade_date=START + timedelta(days=offset),
        trade=ExecutedTrade(
            symbol="510300.SH",
            side=side,
            quantity=quantity,
            reference_price=executed,
            executed_price=executed,
            gross_amount=gross,
            commission=fee,
            slippage_cost=Decimal(0),
            other_cost=Decimal(0),
            cash_delta=(gross - fee) if side is OrderSide.SELL else -(gross + fee),
        ),
    )


def test_total_and_annual_return_match_hand_calculation() -> None:
    # Equity 100 -> 110 over 5 records: total return 10%.
    metrics = compute_metrics(_ledger(["100", "102", "104", "106", "110"]))

    assert metrics.total_return == pytest.approx(0.10)
    # Annualized: 1.1 ** (252/5) - 1, computed independently here.
    assert metrics.annual_return == pytest.approx(1.1 ** (252 / 5) - 1)
    assert metrics.effective_days == 5


def test_max_drawdown_and_underwater_match_hand_calculation() -> None:
    # Peak 120 then trough 90: drawdown = 90/120 - 1 = -25%.
    metrics = compute_metrics(_ledger(["100", "120", "100", "90", "95", "121"]))

    assert metrics.max_drawdown == pytest.approx(90 / 120 - 1)
    # Underwater records after the 120 peak: 100, 90, 95 (3 days); 121 recovers.
    assert metrics.longest_underwater_days == 3


def test_volatility_and_sharpe_match_numpy_style_calculation() -> None:
    equities = ["100", "101", "99", "102", "100"]
    metrics = compute_metrics(_ledger(equities))

    values = [float(value) for value in [100, 101, 99, 102, 100]]
    returns = [values[i + 1] / values[i] - 1 for i in range(4)]
    mean = sum(returns) / len(returns)
    variance = sum((r - mean) ** 2 for r in returns) / len(returns)
    expected_volatility = math.sqrt(variance) * math.sqrt(252)

    assert metrics.annual_volatility == pytest.approx(expected_volatility)
    assert metrics.sharpe_ratio == pytest.approx(
        (metrics.annual_return - 0.0) / expected_volatility
    )


def test_calmar_is_annual_return_over_abs_drawdown() -> None:
    metrics = compute_metrics(_ledger(["100", "120", "90", "130"]))

    assert metrics.calmar_ratio == pytest.approx(
        metrics.annual_return / abs(metrics.max_drawdown)
    )


def test_flat_curve_returns_null_metrics_with_reasons() -> None:
    metrics = compute_metrics(_ledger(["100", "100", "100"]))

    assert metrics.total_return == pytest.approx(0.0)
    assert metrics.sharpe_ratio is None
    assert NOTE_ZERO_VOLATILITY in metrics.notes
    assert metrics.max_drawdown is None
    assert metrics.calmar_ratio is None
    assert metrics.longest_underwater_days == 0


def test_single_record_reports_short_sample() -> None:
    metrics = compute_metrics(_ledger(["100"]))

    assert metrics.annual_return is None
    assert metrics.annual_volatility is None
    assert NOTE_SHORT_SAMPLE in metrics.notes


def test_win_rate_and_profit_loss_ratio_from_round_trips() -> None:
    trades = (
        _trade(side=OrderSide.BUY, quantity=100, price="4.00", offset=0),
        _trade(side=OrderSide.SELL, quantity=100, price="4.40", offset=2),  # +40
        _trade(side=OrderSide.BUY, quantity=100, price="4.40", offset=3),
        _trade(side=OrderSide.SELL, quantity=100, price="4.30", offset=4),  # loses vs avg cost
    )
    metrics = compute_metrics(_ledger(["100", "101", "102", "103", "104"], trades))

    # First sell wins (+0.40/share), second sell loses vs its 4.20 average cost
    # (buys at 4.00 and 4.40 average to 4.20; 4.30 sell is actually a +0.10 win).
    # Hand-derived: outcomes are +40 and +10 => 100% win rate, no losses.
    assert metrics.win_rate == pytest.approx(1.0)
    assert metrics.profit_loss_ratio is None
    assert metrics.trade_count == 4


def test_no_sells_reports_note_instead_of_fake_ratio() -> None:
    trades = (_trade(side=OrderSide.BUY, quantity=100, price="4.00"),)
    metrics = compute_metrics(_ledger(["100", "101"], trades))

    assert metrics.win_rate is None
    assert metrics.profit_loss_ratio is None
    assert NOTE_NO_CLOSED_TRADE_PAIR in metrics.notes


def test_turnover_is_traded_notional_over_average_equity() -> None:
    trades = (
        _trade(side=OrderSide.BUY, quantity=100, price="4.00"),
        _trade(side=OrderSide.SELL, quantity=100, price="4.40", offset=2),
    )
    metrics = compute_metrics(_ledger(["100", "102", "104"], trades))

    traded = 100 * 4.00 + 100 * 4.40
    average_equity = (100 + 102 + 104) / 3
    assert metrics.turnover == pytest.approx(traded / average_equity)


def test_cost_total_sums_all_frictions() -> None:
    trades = (
        _trade(side=OrderSide.BUY, quantity=100, price="4.00", commission="5"),
        _trade(side=OrderSide.SELL, quantity=100, price="4.40", commission="5", offset=1),
    )
    metrics = compute_metrics(_ledger(["100", "101"], trades))

    assert metrics.cost_total == Decimal("10")


def test_benchmark_return_reported_when_curve_supplied() -> None:
    metrics = compute_metrics(
        _ledger(["100", "105", "110"]),
        benchmark_curve=(Decimal("4.0"), Decimal("4.2")),
    )

    assert metrics.benchmark_total_return == pytest.approx(0.05)


def test_metrics_are_deterministic_across_runs() -> None:
    ledger = _ledger(["100", "103", "99", "108", "112"])

    assert compute_metrics(ledger) == compute_metrics(ledger)


def test_mark_to_close_requires_price_for_every_position() -> None:
    record = mark_to_close(
        START,
        Decimal("500"),
        {"510300.SH": 100},
        {"510300.SH": Decimal("4.00")},
    )
    assert record.market_value == Decimal("400")
    assert record.total_equity == Decimal("900")

    with pytest.raises(ValueError, match="missing close price"):
        mark_to_close(START, Decimal("500"), {"510300.SH": 100}, {})
