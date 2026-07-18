"""Next-open portfolio execution with lot rounding and auditable costs (node 10).

The engine turns target weights into integer-share trades executed at the next
session's open, applying per-side slippage, commission with a per-order minimum,
and cash/lot/suspension constraints.  Sells settle before buys so freed cash can
fund purchases.  It never mutates market data and returns a fully reconstructable
:class:`ExecutionResult`; costs are modelled here, not inside strategies.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.execution import (
    SKIP_INSUFFICIENT_CASH,
    SKIP_NO_QUOTE,
    SKIP_SUSPENDED,
    CostModel,
    ExecutedTrade,
    ExecutionResult,
    MarketQuote,
    PortfolioState,
    SkippedTrade,
)

_BPS = Decimal(10_000)
_CASH_QUANTIZE = Decimal("0.0001")


def execute_rebalance(
    *,
    target_weights: Mapping[str, Decimal],
    state: PortfolioState,
    quotes: Mapping[str, MarketQuote],
    lot_sizes: Mapping[str, int],
    cost_model: CostModel,
    total_equity: Decimal | None = None,
) -> ExecutionResult:
    """Execute one rebalance toward ``target_weights`` at next-open prices.

    ``total_equity`` anchors target notional; when omitted it is derived from
    current cash plus the open-price mark of held positions, so a caller without
    an independent valuation still gets consistent sizing.
    """

    equity = total_equity if total_equity is not None else _mark_to_open(state, quotes)
    if equity <= 0 and not state.positions:
        # Nothing to size and nothing to exit: a true no-op.
        return ExecutionResult(trades=(), skipped=(), state_before=state, state_after=state)

    desired_shares = _desired_shares(target_weights, quotes, lot_sizes, max(equity, Decimal(0)))
    cash = state.cash
    positions = dict(state.positions)
    trades: list[ExecutedTrade] = []
    skipped: list[SkippedTrade] = []

    symbols = sorted(set(positions) | set(desired_shares) | set(target_weights))

    # Phase 1: sells (including full exits) free cash for the buys that follow.
    for symbol in symbols:
        current = positions.get(symbol, 0)
        target = desired_shares.get(symbol, 0)
        if target >= current:
            continue
        quote = quotes.get(symbol)
        reason = _blocking_reason(quote)
        if reason is not None:
            skipped.append(SkippedTrade(symbol=symbol, side=OrderSide.SELL, reason=reason))
            continue
        assert quote is not None
        quantity = current - target
        trade = _build_trade(symbol, OrderSide.SELL, quantity, quote, cost_model)
        cash = (cash + trade.cash_delta).quantize(_CASH_QUANTIZE)
        positions[symbol] = current - quantity
        trades.append(trade)

    # Phase 2: buys, each guarded so cash can never go negative.
    for symbol in symbols:
        current = positions.get(symbol, 0)
        target = desired_shares.get(symbol, 0)
        if target <= current:
            continue
        quote = quotes.get(symbol)
        reason = _blocking_reason(quote)
        if reason is not None:
            skipped.append(SkippedTrade(symbol=symbol, side=OrderSide.BUY, reason=reason))
            continue
        assert quote is not None
        quantity = target - current
        trade = _build_trade(symbol, OrderSide.BUY, quantity, quote, cost_model)
        if -trade.cash_delta > cash:
            affordable = _largest_affordable_quantity(
                quantity, quote, cost_model, cash, lot_sizes.get(symbol, 1)
            )
            if affordable == 0:
                skipped.append(
                    SkippedTrade(symbol=symbol, side=OrderSide.BUY, reason=SKIP_INSUFFICIENT_CASH)
                )
                continue
            trade = _build_trade(symbol, OrderSide.BUY, affordable, quote, cost_model)
            skipped.append(
                SkippedTrade(
                    symbol=symbol,
                    side=OrderSide.BUY,
                    reason=SKIP_INSUFFICIENT_CASH,
                    detail=f"reduced from {quantity} to {affordable}",
                )
            )
            quantity = affordable
        cash = (cash + trade.cash_delta).quantize(_CASH_QUANTIZE)
        positions[symbol] = current + quantity
        trades.append(trade)

    final_positions = {symbol: shares for symbol, shares in positions.items() if shares > 0}
    state_after = PortfolioState(cash=cash, positions=final_positions)
    return ExecutionResult(
        trades=tuple(trades),
        skipped=tuple(skipped),
        state_before=state,
        state_after=state_after,
    )


def _desired_shares(
    target_weights: Mapping[str, Decimal],
    quotes: Mapping[str, MarketQuote],
    lot_sizes: Mapping[str, int],
    equity: Decimal,
) -> dict[str, int]:
    desired: dict[str, int] = {}
    for symbol, weight in target_weights.items():
        quote = quotes.get(symbol)
        if quote is None or quote.is_suspended or weight <= 0:
            continue
        lot = lot_sizes.get(symbol, 1)
        target_notional = equity * weight
        raw_shares = target_notional / quote.open_price
        lots = (raw_shares / Decimal(lot)).to_integral_value(rounding=ROUND_DOWN)
        shares = int(lots) * lot
        if shares > 0:
            desired[symbol] = shares
    return desired


def _build_trade(
    symbol: str,
    side: OrderSide,
    quantity: int,
    quote: MarketQuote,
    cost_model: CostModel,
) -> ExecutedTrade:
    slippage_factor = cost_model.slippage_bps / _BPS
    reference_price = quote.open_price
    if side == OrderSide.BUY:
        executed_price = reference_price * (Decimal(1) + slippage_factor)
    else:
        executed_price = reference_price * (Decimal(1) - slippage_factor)
    executed_price = _round_price(executed_price)

    gross = (executed_price * Decimal(quantity)).quantize(_CASH_QUANTIZE)
    commission = _commission(gross, cost_model)
    transfer_fee = (gross * cost_model.transfer_fee_rate).quantize(_CASH_QUANTIZE)
    slippage_cost = (abs(executed_price - reference_price) * Decimal(quantity)).quantize(
        _CASH_QUANTIZE
    )
    if side == OrderSide.BUY:
        cash_delta = -(gross + commission + transfer_fee)
    else:
        cash_delta = gross - commission - transfer_fee
    return ExecutedTrade(
        symbol=symbol,
        side=side,
        quantity=quantity,
        reference_price=reference_price,
        executed_price=executed_price,
        gross_amount=gross,
        commission=commission,
        slippage_cost=slippage_cost,
        other_cost=transfer_fee,
        cash_delta=cash_delta.quantize(_CASH_QUANTIZE),
    )


def _commission(gross: Decimal, cost_model: CostModel) -> Decimal:
    rate_commission = gross * cost_model.commission_rate
    return max(rate_commission, cost_model.minimum_commission).quantize(_CASH_QUANTIZE)


def _largest_affordable_quantity(
    desired_quantity: int,
    quote: MarketQuote,
    cost_model: CostModel,
    cash: Decimal,
    lot: int,
) -> int:
    """Return the largest lot-aligned quantity whose buy cost fits in ``cash``."""

    quantity = desired_quantity
    while quantity > 0:
        trade = _build_trade(quote.symbol, OrderSide.BUY, quantity, quote, cost_model)
        if -trade.cash_delta <= cash:
            return quantity
        quantity -= lot
    return 0


def _blocking_reason(quote: MarketQuote | None) -> str | None:
    if quote is None:
        return SKIP_NO_QUOTE
    if quote.is_suspended:
        return SKIP_SUSPENDED
    return None


def _mark_to_open(state: PortfolioState, quotes: Mapping[str, MarketQuote]) -> Decimal:
    equity = state.cash
    for symbol, shares in state.positions.items():
        quote = quotes.get(symbol)
        if quote is not None:
            equity += quote.open_price * Decimal(shares)
    return equity


def _round_price(price: Decimal) -> Decimal:
    return price.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
