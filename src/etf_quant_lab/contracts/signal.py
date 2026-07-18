"""Stable contracts for daily signal generation."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.enums import RiskState, SignalAction, StrategyId

DISCLAIMER = "仅供个人研究和模拟交易, 不是自动订单。"


class SignalStatus:
    """Stable string states for a stored signal batch."""

    VALID = "VALID"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class GenerateDailySignalRequest:
    """Request to produce (or idempotently fetch) one day's signal batch."""

    trade_date: date
    strategy_id: StrategyId
    strategy_version: str
    parameters: Mapping[str, object] = field(default_factory=dict)
    dataset_id: str | None = None
    quality_report_id: str | None = None
    current_weights: Mapping[str, Decimal] = field(default_factory=dict)
    allow_stale_data: bool = False


@dataclass(frozen=True, slots=True)
class SignalItem:
    """One symbol's target with the explanation shown to the user."""

    symbol: str
    action: SignalAction
    current_weight: Decimal
    target_weight: Decimal
    weight_delta: Decimal
    reference_close: Decimal | None = None
    score: Decimal | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.target_weight < 0 or self.target_weight > 1:
            raise ValueError("target_weight must be within [0, 1]")
        if self.current_weight < 0:
            raise ValueError("current_weight must not be negative")


@dataclass(frozen=True, slots=True)
class DailySignalBatch:
    """One immutable signal batch, valid or explicitly blocked."""

    signal_id: str
    trade_date: date
    generated_at: datetime
    strategy_id: StrategyId
    strategy_version: str
    parameter_hash: str
    universe_hash: str
    idempotency_key: str
    status: str
    risk_state: RiskState
    items: tuple[SignalItem, ...]
    target_cash_weight: Decimal
    data_as_of: date
    dataset_id: str | None = None
    quality_report_id: str | None = None
    blocked_reason: str | None = None
    warnings: tuple[str, ...] = ()
    disclaimer: str = DISCLAIMER

    def __post_init__(self) -> None:
        if len(self.signal_id) != 26:
            raise ValueError("signal_id must contain exactly 26 characters")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")
        if self.status not in {SignalStatus.VALID, SignalStatus.BLOCKED}:
            raise ValueError(f"unsupported signal status: {self.status}")
        if self.status == SignalStatus.BLOCKED and not self.blocked_reason:
            raise ValueError("a blocked signal must carry a reason")
        if self.target_cash_weight < 0 or self.target_cash_weight > 1:
            raise ValueError("target_cash_weight must be within [0, 1]")
        symbols = [item.symbol for item in self.items]
        if len(symbols) != len(set(symbols)):
            raise ValueError("signal items must not repeat a symbol")


def universe_hash(symbols: tuple[str, ...]) -> str:
    """Return a stable digest of the normalized universe membership."""

    payload = ",".join(sorted(symbol.strip().upper() for symbol in symbols))
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"


def signal_idempotency_key(
    *,
    strategy_id: StrategyId,
    strategy_version: str,
    trade_date: date,
    universe_digest: str,
    parameter_digest: str,
) -> str:
    """Build the natural idempotency key from the doc's four components."""

    payload = "|".join(
        (
            strategy_id.value,
            strategy_version,
            trade_date.isoformat(),
            universe_digest,
            parameter_digest,
        )
    )
    return f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
