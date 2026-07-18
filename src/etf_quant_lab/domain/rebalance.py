"""Rebalance proposals: diff target weights against actual holdings (node 16).

The planner compares a signal's target weights with the paper account's current
positions at reference prices, rounds to whole lots, and predicts the cash flow
of the suggested trades.  Sells are planned before buys so freed cash funds the
purchases; every buy is sized down until the predicted cash stays non-negative
after estimated fees.  The output is a proposal for the user to confirm — it
never touches the account by itself.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.execution import CostModel

SKIP_BELOW_ONE_LOT = "BELOW_ONE_LOT"
SKIP_NO_PRICE = "NO_REFERENCE_PRICE"
SKIP_CASH_EXHAUSTED = "CASH_EXHAUSTED"

_BPS = Decimal(10_000)
_CASH_QUANTIZE = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class ProposedTrade:
    """One suggested order with its predicted cash impact."""

    symbol: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    estimated_cash_delta: Decimal
    target_weight: Decimal
    achieved_weight: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0:
            raise ValueError("reference_price must be positive")


@dataclass(frozen=True, slots=True)
class SkippedProposal:
    """One target change that produced no trade, with the reason."""

    symbol: str
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class RebalanceProposal:
    """Complete suggested rebalance with cash prediction and safety guarantees."""

    trades: tuple[ProposedTrade, ...]
    skipped: tuple[SkippedProposal, ...]
    current_cash: Decimal
    predicted_cash: Decimal
    total_equity: Decimal

    def __post_init__(self) -> None:
        if self.predicted_cash < 0:
            raise ValueError("a proposal must never predict negative cash")


def build_rebalance_proposal(
    *,
    target_weights: Mapping[str, Decimal],
    current_positions: Mapping[str, int],
    cash: Decimal,
    reference_prices: Mapping[str, Decimal],
    lot_sizes: Mapping[str, int],
    cost_model: CostModel,
) -> RebalanceProposal:
    """Diff targets against holdings and propose lot-rounded, cash-safe trades."""

    if cash < 0:
        raise ValueError("cash must not be negative")
    equity = _total_equity(cash, current_positions, reference_prices)
    trades: list[ProposedTrade] = []
    skipped: list[SkippedProposal] = []
    predicted_cash = cash

    symbols = sorted(set(target_weights) | set(current_positions))
    desired = _desired_shares(target_weights, reference_prices, lot_sizes, equity, skipped)

    # Phase 1: sells (down-weights and full exits) free cash first.
    for symbol in symbols:
        current = current_positions.get(symbol, 0)
        target = desired.get(symbol, 0)
        if target >= current:
            continue
        price = reference_prices.get(symbol)
        if price is None or price <= 0:
            skipped.append(SkippedProposal(symbol=symbol, reason=SKIP_NO_PRICE))
            continue
        quantity = current - target
        cash_delta = _estimated_cash_delta(OrderSide.SELL, quantity, price, cost_model)
        predicted_cash = (predicted_cash + cash_delta).quantize(_CASH_QUANTIZE)
        trades.append(
            ProposedTrade(
                symbol=symbol,
                side=OrderSide.SELL,
                quantity=quantity,
                reference_price=price,
                estimated_cash_delta=cash_delta,
                target_weight=target_weights.get(symbol, Decimal(0)),
                achieved_weight=_weight(target, price, equity),
            )
        )

    # Phase 2: buys, each shrunk lot by lot until predicted cash stays >= 0.
    for symbol in symbols:
        current = current_positions.get(symbol, 0)
        target = desired.get(symbol, 0)
        if target <= current:
            continue
        price = reference_prices.get(symbol)
        if price is None or price <= 0:
            skipped.append(SkippedProposal(symbol=symbol, reason=SKIP_NO_PRICE))
            continue
        lot = lot_sizes.get(symbol, 1)
        quantity = target - current
        cash_delta = _estimated_cash_delta(OrderSide.BUY, quantity, price, cost_model)
        reduced = False
        while quantity > 0 and predicted_cash + cash_delta < 0:
            quantity -= lot
            reduced = True
            if quantity <= 0:
                break
            cash_delta = _estimated_cash_delta(OrderSide.BUY, quantity, price, cost_model)
        if quantity <= 0:
            skipped.append(SkippedProposal(symbol=symbol, reason=SKIP_CASH_EXHAUSTED))
            continue
        if reduced:
            skipped.append(
                SkippedProposal(
                    symbol=symbol,
                    reason=SKIP_CASH_EXHAUSTED,
                    detail=f"reduced to {quantity}",
                )
            )
        predicted_cash = (predicted_cash + cash_delta).quantize(_CASH_QUANTIZE)
        trades.append(
            ProposedTrade(
                symbol=symbol,
                side=OrderSide.BUY,
                quantity=quantity,
                reference_price=price,
                estimated_cash_delta=cash_delta,
                target_weight=target_weights.get(symbol, Decimal(0)),
                achieved_weight=_weight(current + quantity, price, equity),
            )
        )

    return RebalanceProposal(
        trades=tuple(trades),
        skipped=tuple(skipped),
        current_cash=cash,
        predicted_cash=predicted_cash,
        total_equity=equity,
    )


def _desired_shares(
    target_weights: Mapping[str, Decimal],
    reference_prices: Mapping[str, Decimal],
    lot_sizes: Mapping[str, int],
    equity: Decimal,
    skipped: list[SkippedProposal],
) -> dict[str, int]:
    desired: dict[str, int] = {}
    for symbol, weight in sorted(target_weights.items()):
        if weight < 0 or weight > 1:
            raise ValueError(f"target weight out of range for {symbol}")
        if weight == 0:
            desired[symbol] = 0
            continue
        price = reference_prices.get(symbol)
        if price is None or price <= 0:
            skipped.append(SkippedProposal(symbol=symbol, reason=SKIP_NO_PRICE))
            continue
        lot = lot_sizes.get(symbol, 1)
        raw_shares = equity * weight / price
        lots = (raw_shares / Decimal(lot)).to_integral_value(rounding=ROUND_DOWN)
        shares = int(lots) * lot
        if shares == 0:
            skipped.append(
                SkippedProposal(
                    symbol=symbol,
                    reason=SKIP_BELOW_ONE_LOT,
                    detail=f"target weight {weight} rounds below one lot of {lot}",
                )
            )
        desired[symbol] = shares
    return desired


def _estimated_cash_delta(
    side: OrderSide,
    quantity: int,
    price: Decimal,
    cost_model: CostModel,
) -> Decimal:
    slippage_factor = cost_model.slippage_bps / _BPS
    if side == OrderSide.BUY:
        executed = price * (Decimal(1) + slippage_factor)
    else:
        executed = price * (Decimal(1) - slippage_factor)
    gross = (executed * Decimal(quantity)).quantize(_CASH_QUANTIZE)
    commission = max(
        gross * cost_model.commission_rate, cost_model.minimum_commission
    ).quantize(_CASH_QUANTIZE)
    transfer = (gross * cost_model.transfer_fee_rate).quantize(_CASH_QUANTIZE)
    if side == OrderSide.BUY:
        return -(gross + commission + transfer)
    return gross - commission - transfer


def _total_equity(
    cash: Decimal,
    positions: Mapping[str, int],
    prices: Mapping[str, Decimal],
) -> Decimal:
    equity = cash
    for symbol, shares in positions.items():
        price = prices.get(symbol)
        if price is not None and price > 0:
            equity += price * Decimal(shares)
    return equity


def _weight(shares: int, price: Decimal, equity: Decimal) -> Decimal:
    if equity <= 0:
        return Decimal(0)
    return (price * Decimal(shares) / equity).quantize(Decimal("0.000001"))
