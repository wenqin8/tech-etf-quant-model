"""Stable contracts for the strategy interface: parameters, context and targets."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from etf_quant_lab.contracts.enums import SignalAction, StrategyId

_ALLOWED_PARAMETER_TYPES = ("int", "float", "bool", "enum")


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """Declarative schema for one strategy parameter.

    The spec is data, not code: a strategy publishes a tuple of these and the
    service validates and normalizes user input against them, so no strategy
    hand-rolls its own parameter parsing.
    """

    name: str
    type: str
    default: object
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: tuple[str, ...] = ()
    description: str = ""

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("parameter name must not be blank")
        if self.type not in _ALLOWED_PARAMETER_TYPES:
            raise ValueError(f"unsupported parameter type: {self.type}")
        if self.type == "enum" and not self.choices:
            raise ValueError("enum parameters must declare choices")
        if self.type != "enum" and self.choices:
            raise ValueError("only enum parameters may declare choices")
        if (
            self.minimum is not None
            and self.maximum is not None
            and self.minimum > self.maximum
        ):
            raise ValueError("parameter minimum must not exceed maximum")


@dataclass(frozen=True, slots=True)
class StrategyDescriptor:
    """Static, presentation-safe description of a registered strategy version."""

    strategy_id: StrategyId
    version: str
    display_name: str
    minimum_history_bars: int
    supports_multi_asset: bool
    parameter_specs: tuple[ParameterSpec, ...]

    def __post_init__(self) -> None:
        if not self.version.strip():
            raise ValueError("version must not be blank")
        if self.minimum_history_bars < 0:
            raise ValueError("minimum_history_bars must not be negative")
        names = [spec.name for spec in self.parameter_specs]
        if len(names) != len(set(names)):
            raise ValueError("parameter specs must have unique names")


@dataclass(frozen=True, slots=True)
class ValidateParametersRequest:
    """Request to validate and normalize raw user parameters for a strategy."""

    strategy_id: StrategyId
    version: str
    parameters: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ParameterValidationResult:
    """Outcome of validating parameters, with a stable hash when valid."""

    valid: bool
    normalized_parameters: Mapping[str, object]
    parameter_hash: str | None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TargetAllocation:
    """One symbol's target weight with an auditable explanation."""

    symbol: str
    target_weight: Decimal
    action: SignalAction
    score: Decimal | None = None
    reason_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.symbol.strip():
            raise ValueError("symbol must not be blank")
        if self.target_weight < 0:
            raise ValueError("target_weight must not be negative")
        if self.target_weight > 1:
            raise ValueError("target_weight must not exceed 1")


@dataclass(frozen=True, slots=True)
class TargetPortfolio:
    """A strategy's complete target state for one decision date.

    The invariant ``sum(weights) + cash_weight == 1`` is enforced here so every
    strategy output is fully allocated and no downstream layer has to guess the
    cash residual.
    """

    as_of_date: date
    strategy_id: StrategyId
    version: str
    allocations: tuple[TargetAllocation, ...]
    cash_weight: Decimal
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.cash_weight < 0 or self.cash_weight > 1:
            raise ValueError("cash_weight must be within [0, 1]")
        symbols = [allocation.symbol for allocation in self.allocations]
        if len(symbols) != len(set(symbols)):
            raise ValueError("allocations must not repeat a symbol")
        invested = sum((allocation.target_weight for allocation in self.allocations), Decimal(0))
        if invested > 1:
            raise ValueError("invested weights must not exceed 1")
        total = invested + self.cash_weight
        if abs(total - Decimal(1)) > Decimal("0.000001"):
            raise ValueError("allocations plus cash_weight must equal 1")


def parameter_hash(parameters: Mapping[str, object]) -> str:
    """Return a stable ``sha256:`` digest of normalized parameters.

    Parameters are serialized with sorted keys and string-coerced Decimals so the
    same logical parameters always hash identically across runs and processes.
    """

    payload = json.dumps(
        dict(parameters),
        sort_keys=True,
        separators=(",", ":"),
        default=_hash_default,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    return f"sha256:{digest}"


def _hash_default(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"unhashable parameter value: {type(value).__name__}")
