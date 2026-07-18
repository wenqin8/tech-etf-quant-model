"""Strategy protocol and decision-time context.

A concrete strategy (nodes 8 and 9) implements :class:`Strategy` and turns an
as-of-bounded :class:`StrategyContext` into a :class:`TargetPortfolio`.  Per
STYLE §11 a strategy is pure: no network, database, clock or account access, only
the provided data slice and parameters.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable

from etf_quant_lab.contracts.enums import StrategyId
from etf_quant_lab.contracts.strategy import ParameterSpec, TargetPortfolio
from etf_quant_lab.domain.market_view import MarketDataView


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy may read for one decision date.

    ``market_data`` is already bounded to ``as_of_date``; ``current_weights`` and
    ``cash_weight`` describe the portfolio the target will be compared against.
    """

    as_of_date: date
    universe_symbols: tuple[str, ...]
    market_data: MarketDataView
    dataset_id: str | None = None
    quality_report_id: str | None = None
    current_weights: Mapping[str, Decimal] = field(default_factory=dict)
    cash_weight: Decimal = Decimal(1)

    def __post_init__(self) -> None:
        if self.market_data.as_of_date != self.as_of_date:
            raise ValueError("market_data.as_of_date must match the context as_of_date")
        if any(weight < 0 for weight in self.current_weights.values()):
            raise ValueError("current_weights must not contain negative values")
        if self.cash_weight < 0 or self.cash_weight > 1:
            raise ValueError("cash_weight must be within [0, 1]")


@runtime_checkable
class Strategy(Protocol):
    """Uniform interface every ETF strategy version must satisfy."""

    strategy_id: StrategyId
    version: str

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        """Return the declarative parameter schema for this strategy version."""
        ...

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        """Return the minimum history length required before targets are valid."""
        ...

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        """Return the target portfolio for ``context.as_of_date``."""
        ...
