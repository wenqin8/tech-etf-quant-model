"""Performance metrics over a portfolio ledger (node 11).

All metrics follow STYLE §11.4: simple daily returns, high-water-mark drawdown,
configurable annualization (A-share daily default 252), explicit ``None`` plus a
note when a metric cannot be computed honestly, and every figure derivable from
the equity curve and trade list alone.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from itertools import pairwise

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.performance import (
    DailyPortfolioRecord,
    PerformanceMetrics,
    PortfolioLedger,
)

NOTE_SHORT_SAMPLE = "SHORT_SAMPLE"
NOTE_ZERO_VOLATILITY = "ZERO_VOLATILITY"
NOTE_NO_DRAWDOWN = "NO_DRAWDOWN"
NOTE_NO_CLOSED_TRADE_PAIR = "NO_SELL_TRADES"

_MIN_RETURN_SAMPLE = 2


def compute_metrics(
    ledger: PortfolioLedger,
    *,
    annualization_days: int = 252,
    risk_free_rate: float = 0.0,
    benchmark_curve: tuple[Decimal, ...] = (),
) -> PerformanceMetrics:
    """Compute the standard metric set from one ledger.

    ``benchmark_curve`` is an optional equity/price series aligned to the same
    date range; only its end-to-end return is reported, keeping benchmark logic
    out of the ledger itself.
    """

    if not ledger.records:
        raise ValueError("ledger must contain at least one record")
    if annualization_days <= 0:
        raise ValueError("annualization_days must be positive")

    records = ledger.records
    equity = [float(record.total_equity) for record in records]
    daily_returns = _daily_returns(equity)
    notes: list[str] = []

    total_return = equity[-1] / float(ledger.initial_cash) - 1.0
    effective_days = len(records)

    annual_return = _annualized_return(
        total_return, effective_days, annualization_days, notes
    )
    annual_volatility = _annualized_volatility(daily_returns, annualization_days, notes)
    sharpe = _sharpe(annual_return, annual_volatility, risk_free_rate, notes)
    max_drawdown, longest_underwater = _drawdown_stats(equity)
    calmar = _calmar(annual_return, max_drawdown, notes)
    win_rate, profit_loss_ratio = _trade_stats(ledger, notes)
    turnover = _turnover(ledger, equity)

    benchmark_total_return: float | None = None
    if len(benchmark_curve) >= 2 and benchmark_curve[0] > 0:
        benchmark_total_return = float(
            benchmark_curve[-1] / benchmark_curve[0] - Decimal(1)
        )

    return PerformanceMetrics(
        start_date=records[0].trade_date,
        end_date=records[-1].trade_date,
        effective_days=effective_days,
        trade_count=len(ledger.trades),
        total_return=total_return,
        annual_return=annual_return,
        annual_volatility=annual_volatility,
        sharpe_ratio=sharpe,
        calmar_ratio=calmar,
        max_drawdown=max_drawdown,
        win_rate=win_rate,
        profit_loss_ratio=profit_loss_ratio,
        turnover=turnover,
        longest_underwater_days=longest_underwater,
        cost_total=ledger.total_cost,
        benchmark_total_return=benchmark_total_return,
        annualization_days=annualization_days,
        risk_free_rate=risk_free_rate,
        notes=tuple(dict.fromkeys(notes)),  # dedupe, keep order
    )


def mark_to_close(
    trade_date: date,
    cash: Decimal,
    positions: dict[str, int],
    close_prices: dict[str, Decimal],
) -> DailyPortfolioRecord:
    """Build one end-of-day record by valuing positions at close prices.

    A missing close means the last known valuation should be supplied by the
    caller; this helper requires every held symbol to be priceable so a silent
    zero-valuation can never happen.
    """

    market_value = Decimal(0)
    for symbol, shares in positions.items():
        price = close_prices.get(symbol)
        if price is None:
            raise ValueError(f"missing close price for held symbol: {symbol}")
        if price <= 0:
            raise ValueError(f"close price must be positive: {symbol}")
        market_value += price * Decimal(shares)
    return DailyPortfolioRecord(
        trade_date=trade_date,
        cash=cash,
        positions=dict(positions),
        market_value=market_value,
        total_equity=cash + market_value,
    )


def _daily_returns(equity: list[float]) -> list[float]:
    returns: list[float] = []
    for previous, current in pairwise(equity):
        if previous > 0:
            returns.append(current / previous - 1.0)
    return returns


def _annualized_return(
    total_return: float,
    effective_days: int,
    annualization_days: int,
    notes: list[str],
) -> float | None:
    if effective_days < _MIN_RETURN_SAMPLE:
        notes.append(NOTE_SHORT_SAMPLE)
        return None
    growth = 1.0 + total_return
    if growth <= 0:
        return -1.0
    exponent = annualization_days / effective_days
    return float(growth**exponent) - 1.0


def _annualized_volatility(
    daily_returns: list[float],
    annualization_days: int,
    notes: list[str],
) -> float | None:
    if len(daily_returns) < _MIN_RETURN_SAMPLE:
        notes.append(NOTE_SHORT_SAMPLE)
        return None
    mean = sum(daily_returns) / len(daily_returns)
    variance = sum((value - mean) ** 2 for value in daily_returns) / len(daily_returns)
    return math.sqrt(variance) * math.sqrt(annualization_days)


def _sharpe(
    annual_return: float | None,
    annual_volatility: float | None,
    risk_free_rate: float,
    notes: list[str],
) -> float | None:
    if annual_return is None or annual_volatility is None:
        return None
    if annual_volatility == 0:
        notes.append(NOTE_ZERO_VOLATILITY)
        return None
    return (annual_return - risk_free_rate) / annual_volatility


def _drawdown_stats(equity: list[float]) -> tuple[float | None, int | None]:
    """Max drawdown from the running high-water mark, plus the longest underwater run."""

    high_water = equity[0]
    max_drawdown = 0.0
    underwater_days = 0
    longest_underwater = 0
    for value in equity:
        if value >= high_water:
            high_water = value
            underwater_days = 0
        else:
            underwater_days += 1
            longest_underwater = max(longest_underwater, underwater_days)
            drawdown = value / high_water - 1.0
            max_drawdown = min(max_drawdown, drawdown)
    if max_drawdown == 0.0:
        return None, 0
    return max_drawdown, longest_underwater


def _calmar(
    annual_return: float | None,
    max_drawdown: float | None,
    notes: list[str],
) -> float | None:
    if annual_return is None:
        return None
    if max_drawdown is None or max_drawdown == 0:
        notes.append(NOTE_NO_DRAWDOWN)
        return None
    return annual_return / abs(max_drawdown)


def _trade_stats(
    ledger: PortfolioLedger,
    notes: list[str],
) -> tuple[float | None, float | None]:
    """Win rate and profit/loss ratio over completed sell trades (FIFO-free proxy).

    Each sell is graded against the volume-weighted average cost of prior buys in
    the same symbol; without any sell there is nothing to grade.
    """

    buy_cost: dict[str, list[tuple[int, Decimal]]] = {}
    outcomes: list[Decimal] = []
    for dated in ledger.trades:
        trade = dated.trade
        if trade.side == OrderSide.BUY:
            buy_cost.setdefault(trade.symbol, []).append(
                (trade.quantity, trade.executed_price)
            )
            continue
        lots = buy_cost.get(trade.symbol, [])
        total_quantity = sum(quantity for quantity, _ in lots)
        if total_quantity == 0:
            continue
        average_cost = (
            sum((Decimal(quantity) * price for quantity, price in lots), Decimal(0))
            / Decimal(total_quantity)
        )
        outcomes.append((trade.executed_price - average_cost) * Decimal(trade.quantity))

    if not outcomes:
        notes.append(NOTE_NO_CLOSED_TRADE_PAIR)
        return None, None

    wins = [outcome for outcome in outcomes if outcome > 0]
    losses = [outcome for outcome in outcomes if outcome < 0]
    win_rate = len(wins) / len(outcomes)
    if not losses:
        return win_rate, None
    if not wins:
        return win_rate, 0.0
    average_win = sum(wins, Decimal(0)) / Decimal(len(wins))
    average_loss = abs(sum(losses, Decimal(0)) / Decimal(len(losses)))
    return win_rate, float(average_win / average_loss)


def _turnover(ledger: PortfolioLedger, equity: list[float]) -> float | None:
    if not ledger.trades or not equity:
        return 0.0
    traded_notional = sum(
        (dated.trade.gross_amount for dated in ledger.trades), Decimal(0)
    )
    average_equity = sum(equity) / len(equity)
    if average_equity <= 0:
        return None
    return float(traded_notional) / average_equity
