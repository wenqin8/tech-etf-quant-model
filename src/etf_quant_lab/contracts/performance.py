"""Stable contracts for portfolio ledgers and performance metrics."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from etf_quant_lab.contracts.execution import ExecutedTrade, SkippedTrade


@dataclass(frozen=True, slots=True)
class DailyPortfolioRecord:
    """One trading day's end-of-day portfolio snapshot, marked to close."""

    trade_date: date
    cash: Decimal
    positions: Mapping[str, int] = field(default_factory=dict)
    market_value: Decimal = Decimal(0)
    total_equity: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if self.cash < 0:
            raise ValueError("cash must not be negative")
        if self.market_value < 0:
            raise ValueError("market_value must not be negative")
        if self.total_equity <= 0:
            raise ValueError("total_equity must be positive")


@dataclass(frozen=True, slots=True)
class DatedTrade:
    """One executed trade annotated with its execution date."""

    trade_date: date
    trade: ExecutedTrade


@dataclass(frozen=True, slots=True)
class DatedSkip:
    """One skipped trade annotated with the date it was attempted."""

    trade_date: date
    skip: SkippedTrade


@dataclass(frozen=True, slots=True)
class PortfolioLedger:
    """Complete, auditable record of one simulation run.

    ``records`` is ordered by date; every equity figure can be recomputed from
    cash plus the day's close-marked positions, and every cash change traces to a
    trade in ``trades``.
    """

    records: tuple[DailyPortfolioRecord, ...]
    trades: tuple[DatedTrade, ...]
    skipped: tuple[DatedSkip, ...]
    initial_cash: Decimal

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        dates = [record.trade_date for record in self.records]
        if dates != sorted(dates):
            raise ValueError("records must be ordered by trade_date")
        if len(dates) != len(set(dates)):
            raise ValueError("records must not repeat a trade_date")

    @property
    def total_cost(self) -> Decimal:
        """All commissions, slippage and fees across the run."""

        return sum((dated.trade.total_cost for dated in self.trades), Decimal(0))


@dataclass(frozen=True, slots=True)
class PerformanceMetrics:
    """Backtest metrics with explicit nulls and their reasons.

    Metrics that cannot be computed honestly (short sample, zero volatility, no
    drawdown) are ``None`` and the reason appears in ``notes`` — never a
    misleading infinity.  ``return_type`` and ``risk_free_rate`` are recorded so
    every figure is reproducible.
    """

    start_date: date
    end_date: date
    effective_days: int
    trade_count: int
    total_return: float | None
    annual_return: float | None
    annual_volatility: float | None
    sharpe_ratio: float | None
    calmar_ratio: float | None
    max_drawdown: float | None
    win_rate: float | None
    profit_loss_ratio: float | None
    turnover: float | None
    longest_underwater_days: int | None
    cost_total: Decimal
    benchmark_total_return: float | None = None
    annualization_days: int = 252
    risk_free_rate: float = 0.0
    return_type: str = "simple"
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.effective_days < 0:
            raise ValueError("effective_days must not be negative")
        if self.trade_count < 0:
            raise ValueError("trade_count must not be negative")
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
