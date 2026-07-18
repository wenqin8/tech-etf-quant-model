"""Stable contracts for the paper trading account."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from etf_quant_lab.contracts.enums import OrderSide


class PaperOrderType(StrEnum):
    MARKET_AT_NEXT_OPEN = "MARKET_AT_NEXT_OPEN"
    MANUAL = "MANUAL"


class PaperOrderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    CONFIRMED = "CONFIRMED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class PaperFillSource(StrEnum):
    NEXT_OPEN = "NEXT_OPEN"
    MANUAL = "MANUAL"


class PaperAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    FROZEN = "FROZEN"
    RESET = "RESET"


@dataclass(frozen=True, slots=True)
class PaperAccount:
    """One simulated account's identity and current cash."""

    account_id: str
    name: str
    initial_cash: Decimal
    cash_balance: Decimal
    status: PaperAccountStatus
    version: int
    created_at: datetime
    updated_at: datetime
    base_currency: str = "CNY"

    def __post_init__(self) -> None:
        if len(self.account_id) != 26:
            raise ValueError("account_id must contain exactly 26 characters")
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.cash_balance < 0:
            raise ValueError("cash_balance must not be negative")


@dataclass(frozen=True, slots=True)
class PaperOrder:
    """One simulated order proposal or its resolved state."""

    order_id: str
    account_id: str
    symbol: str
    side: OrderSide
    quantity: int
    order_type: PaperOrderType
    status: PaperOrderStatus
    idempotency_key: str
    created_at: datetime
    updated_at: datetime
    signal_id: str | None = None
    proposed_price: Decimal | None = None
    reject_reason: str | None = None

    def __post_init__(self) -> None:
        if len(self.order_id) != 26:
            raise ValueError("order_id must contain exactly 26 characters")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")


@dataclass(frozen=True, slots=True)
class PaperFill:
    """One executed simulated fill with its full cost breakdown."""

    fill_id: str
    order_id: str
    trade_date: date
    fill_time: datetime
    quantity: int
    price: Decimal
    commission: Decimal
    cash_delta: Decimal
    source: PaperFillSource
    slippage_cost: Decimal = Decimal(0)
    other_cost: Decimal = Decimal(0)

    def __post_init__(self) -> None:
        if len(self.fill_id) != 26:
            raise ValueError("fill_id must contain exactly 26 characters")
        if self.quantity <= 0:
            raise ValueError("quantity must be positive")
        if self.price <= 0:
            raise ValueError("price must be positive")
        if min(self.commission, self.slippage_cost, self.other_cost) < 0:
            raise ValueError("costs must not be negative")


@dataclass(frozen=True, slots=True)
class PaperPosition:
    """One symbol's holding split into sellable and T+1-pending quantities."""

    account_id: str
    symbol: str
    quantity: int
    available_quantity: int
    pending_quantity: int
    average_cost: Decimal
    updated_at: datetime
    pending_date: date | None = None

    def __post_init__(self) -> None:
        if min(self.quantity, self.available_quantity, self.pending_quantity) < 0:
            raise ValueError("position quantities must not be negative")
        if self.available_quantity + self.pending_quantity != self.quantity:
            raise ValueError("available plus pending must equal total quantity")


def order_idempotency_key(
    *,
    account_id: str,
    signal_id: str | None,
    symbol: str,
    side: OrderSide,
) -> str:
    """Build the natural order key from account, signal, symbol and side."""

    payload = "|".join((account_id, signal_id or "-", symbol, side.value))
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
