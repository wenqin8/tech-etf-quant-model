"""Stable contracts for rebalance execution, costs and fills."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal

from etf_quant_lab.contracts.enums import CostScenario, OrderSide

SKIP_SUSPENDED = "SUSPENDED"
SKIP_INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
SKIP_BELOW_LOT_SIZE = "BELOW_LOT_SIZE"
SKIP_NO_QUOTE = "NO_QUOTE"


@dataclass(frozen=True, slots=True)
class CostModel:
    """One cost scenario's commission, minimum commission and slippage terms."""

    scenario: CostScenario
    commission_rate: Decimal
    minimum_commission: Decimal
    slippage_bps: Decimal
    transfer_fee_rate: Decimal = Decimal(0)
    currency: str = "CNY"

    def __post_init__(self) -> None:
        for name, value in (
            ("commission_rate", self.commission_rate),
            ("minimum_commission", self.minimum_commission),
            ("slippage_bps", self.slippage_bps),
            ("transfer_fee_rate", self.transfer_fee_rate),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")


@dataclass(frozen=True, slots=True)
class MarketQuote:
    """Next-open execution inputs for one symbol on the execution date."""

    symbol: str
    open_price: Decimal
    is_suspended: bool = False

    def __post_init__(self) -> None:
        if self.open_price <= 0:
            raise ValueError("open_price must be positive")


@dataclass(frozen=True, slots=True)
class PortfolioState:
    """Cash plus integer share positions before or after one rebalance."""

    cash: Decimal
    positions: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.cash < 0:
            raise ValueError("cash must not be negative")
        for symbol, shares in self.positions.items():
            if shares < 0:
                raise ValueError(f"position must not be negative: {symbol}")


@dataclass(frozen=True, slots=True)
class ExecutedTrade:
    """One fill with a fully auditable cost breakdown.

    ``gross_amount = quantity * executed_price`` and
    ``slippage_cost = quantity * |executed_price - reference_price|`` hold by
    construction so every cost figure can be recomputed from the record.
    """

    symbol: str
    side: OrderSide
    quantity: int
    reference_price: Decimal
    executed_price: Decimal
    gross_amount: Decimal
    commission: Decimal
    slippage_cost: Decimal
    other_cost: Decimal
    cash_delta: Decimal

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.reference_price <= 0 or self.executed_price <= 0:
            raise ValueError("prices must be positive")
        if min(self.commission, self.slippage_cost, self.other_cost) < 0:
            raise ValueError("costs must not be negative")

    @property
    def total_cost(self) -> Decimal:
        """All frictions for this trade: commission, slippage and other fees."""

        return self.commission + self.slippage_cost + self.other_cost


@dataclass(frozen=True, slots=True)
class SkippedTrade:
    """One intended trade that could not execute, with an explicit reason."""

    symbol: str
    side: OrderSide
    reason: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Outcome of executing one rebalance at next-open prices."""

    trades: tuple[ExecutedTrade, ...]
    skipped: tuple[SkippedTrade, ...]
    state_before: PortfolioState
    state_after: PortfolioState

    @property
    def total_cost(self) -> Decimal:
        """Sum of all trade frictions in this rebalance."""

        return sum((trade.total_cost for trade in self.trades), Decimal(0))
