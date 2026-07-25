"""Portfolio backtest orchestration (node 20).

``run_backtest`` chains the released pieces end to end: for every decision date
the strategy sees an as-of-bounded slice (node 7), targets execute at the next
session's open with lot/cash/cost constraints (node 10), each day marks to close
into the ledger (node 11), and the ledger reduces to metrics.  Signals decided
on day T therefore never trade before T+1 — the anti-lookahead execution rule is
structural, not conventional.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import CostScenario, OrderSide, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.execution import (
    CostModel,
    ExecutionResult,
    MarketQuote,
    PortfolioState,
)
from etf_quant_lab.contracts.performance import (
    DailyPortfolioRecord,
    DatedSkip,
    DatedTrade,
    PerformanceMetrics,
    PortfolioLedger,
)
from etf_quant_lab.domain.execution import execute_rebalance
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.performance import compute_metrics, mark_to_close
from etf_quant_lab.services.strategy import StrategyService

BACKTEST_RANGE_TOO_SHORT = "BT_RANGE_TOO_SHORT"

_DEFAULT_COST = CostModel(
    scenario=CostScenario.IDEAL,
    commission_rate=Decimal("0.0001"),
    minimum_commission=Decimal("0"),
    slippage_bps=Decimal("0"),
)


@dataclass(frozen=True, slots=True)
class BacktestRequest:
    """One deterministic backtest configuration."""

    strategy_id: StrategyId
    strategy_version: str
    parameters: Mapping[str, object]
    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    initial_cash: Decimal
    cost_model: CostModel = _DEFAULT_COST
    lot_sizes: Mapping[str, int] = field(default_factory=dict)
    rebalance_every_bars: int = 1

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.rebalance_every_bars <= 0:
            raise ValueError("rebalance_every_bars must be positive")
        if not self.symbols:
            raise ValueError("symbols must not be empty")


@dataclass(frozen=True, slots=True)
class BacktestResult:
    """The full auditable outcome: ledger plus reduced metrics."""

    request: BacktestRequest
    ledger: PortfolioLedger
    metrics: PerformanceMetrics


def run_backtest(
    strategy_service: StrategyService,
    *,
    request: BacktestRequest,
    bars: Sequence[DailyBar],
) -> BacktestResult:
    """Run one deterministic backtest over pre-loaded canonical bars.

    ``bars`` must cover the warmup period before ``start_date``; the engine
    slices per decision date so the strategy can never see past its as-of.
    """

    all_bars = tuple(sorted(bars, key=lambda bar: (bar.trade_date, bar.symbol)))
    trading_dates = sorted(
        {
            bar.trade_date
            for bar in all_bars
            if request.start_date <= bar.trade_date <= request.end_date
        }
    )
    if len(trading_dates) < 2:
        raise DomainError(
            BACKTEST_RANGE_TOO_SHORT,
            "回测区间内可交易日不足",
            details={"available": len(trading_dates)},
        )

    open_by_date: dict[date, dict[str, MarketQuote]] = {}
    close_by_date: dict[date, dict[str, Decimal]] = {}
    for bar in all_bars:
        open_by_date.setdefault(bar.trade_date, {})[bar.symbol] = MarketQuote(
            symbol=bar.symbol,
            open_price=bar.open,
            is_suspended=bar.is_suspended,
        )
        close_by_date.setdefault(bar.trade_date, {})[bar.symbol] = bar.close

    state = PortfolioState(cash=request.initial_cash)
    records: list[DailyPortfolioRecord] = []
    trades: list[DatedTrade] = []
    skips: list[DatedSkip] = []
    pending_weights: Mapping[str, Decimal] | None = None
    position_holding_bars: dict[str, int] = {}
    position_entry_prices: dict[str, Decimal] = {}

    for index, current_date in enumerate(trading_dates):
        # 1. Execute yesterday's decision at today's open (T+1 rule).
        if pending_weights is not None:
            quotes = open_by_date.get(current_date, {})
            result = execute_rebalance(
                target_weights=pending_weights,
                state=state,
                quotes=quotes,
                lot_sizes=request.lot_sizes,
                cost_model=request.cost_model,
            )
            state = result.state_after
            _update_position_state(
                result=result,
                holding_bars=position_holding_bars,
                entry_prices=position_entry_prices,
            )
            trades.extend(
                DatedTrade(trade_date=current_date, trade=trade) for trade in result.trades
            )
            skips.extend(
                DatedSkip(trade_date=current_date, skip=skip) for skip in result.skipped
            )
            pending_weights = None

        # A position bought at today's open has completed one holding session at
        # today's close. Existing positions advance by one bar as well.
        for symbol in state.positions:
            position_holding_bars[symbol] = position_holding_bars.get(symbol, 0) + 1

        # 2. Mark to close before deciding, so current weights are true T-close
        # weights while the eventual fills still occur no earlier than T+1 open.
        closes = close_by_date.get(current_date, {})
        known_closes = {
            symbol: closes[symbol]
            for symbol in state.positions
            if symbol in closes
        }
        missing = set(state.positions) - set(known_closes)
        if missing:
            raise DomainError(
                "BT_MISSING_CLOSE",
                "持仓标的缺少收盘价, 无法估值",
                details={"symbols": tuple(sorted(missing))},
            )
        record = mark_to_close(
            current_date, state.cash, dict(state.positions), known_closes
        )
        records.append(record)

        # 3. Decide targets at today's close on an as-of-bounded slice.
        if index % request.rebalance_every_bars == 0 and index < len(trading_dates) - 1:
            visible = tuple(bar for bar in all_bars if bar.trade_date <= current_date)
            if record.total_equity > 0:
                current_weights = {
                    symbol: closes[symbol] * Decimal(shares) / record.total_equity
                    for symbol, shares in state.positions.items()
                }
                cash_weight = state.cash / record.total_equity
            else:
                current_weights = {}
                cash_weight = Decimal(1)
            portfolio = strategy_service.generate_targets(
                strategy_id=request.strategy_id,
                version=request.strategy_version,
                parameters=request.parameters,
                as_of_date=current_date,
                universe_symbols=request.symbols,
                market_data=MarketDataView(as_of_date=current_date, bars=visible),
                current_weights=current_weights,
                cash_weight=cash_weight,
                position_holding_bars=position_holding_bars,
                position_entry_prices=position_entry_prices,
            )
            pending_weights = {
                allocation.symbol: allocation.target_weight
                for allocation in portfolio.allocations
            }

    ledger = PortfolioLedger(
        records=tuple(records),
        trades=tuple(trades),
        skipped=tuple(skips),
        initial_cash=request.initial_cash,
    )
    metrics = compute_metrics(ledger)
    return BacktestResult(request=request, ledger=ledger, metrics=metrics)


def _update_position_state(
    *,
    result: ExecutionResult,
    holding_bars: dict[str, int],
    entry_prices: dict[str, Decimal],
) -> None:
    """Update average entry prices and reset/remove holding ages after fills."""

    before = result.state_before
    after = result.state_after
    running_shares = dict(before.positions)
    for trade in result.trades:
        old_shares = running_shares.get(trade.symbol, 0)
        if trade.side == OrderSide.BUY:
            old_cost = entry_prices.get(trade.symbol, trade.executed_price)
            new_shares = old_shares + trade.quantity
            entry_prices[trade.symbol] = (
                old_cost * Decimal(old_shares)
                + trade.executed_price * Decimal(trade.quantity)
            ) / Decimal(new_shares)
            if old_shares == 0:
                holding_bars[trade.symbol] = 0
            running_shares[trade.symbol] = new_shares
        else:
            new_shares = old_shares - trade.quantity
            if new_shares <= 0:
                running_shares.pop(trade.symbol, None)
                holding_bars.pop(trade.symbol, None)
                entry_prices.pop(trade.symbol, None)
            else:
                running_shares[trade.symbol] = new_shares

    # Reconcile skipped/partial executions with the authoritative portfolio.
    for symbol in tuple(entry_prices):
        if after.positions.get(symbol, 0) <= 0:
            entry_prices.pop(symbol, None)
            holding_bars.pop(symbol, None)
