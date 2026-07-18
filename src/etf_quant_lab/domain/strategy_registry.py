"""In-process registry of strategy implementations keyed by id and version."""

from __future__ import annotations

from etf_quant_lab.contracts.enums import StrategyId
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.domain.strategy import Strategy

STRATEGY_NOT_FOUND = "STRAT_NOT_FOUND"


class StrategyRegistry:
    """Hold strategy versions so services resolve them without hard imports.

    A strategy is registered once per ``(strategy_id, version)``; re-registering
    the same pair is rejected so a run can never bind to an ambiguous version.
    """

    def __init__(self) -> None:
        self._strategies: dict[tuple[StrategyId, str], Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        """Register one strategy version, rejecting duplicates."""

        key = (strategy.strategy_id, strategy.version)
        if key in self._strategies:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "策略版本重复注册",
                details={"strategy_id": strategy.strategy_id.value, "version": strategy.version},
            )
        self._strategies[key] = strategy

    def get(self, strategy_id: StrategyId, version: str) -> Strategy:
        """Return one registered strategy version or fail explicitly."""

        try:
            return self._strategies[(strategy_id, version)]
        except KeyError as exc:
            raise DomainError(
                STRATEGY_NOT_FOUND,
                "策略未注册或版本不存在",
                details={"strategy_id": strategy_id.value, "version": version},
            ) from exc

    def list_all(self) -> tuple[Strategy, ...]:
        """Return all registered strategies sorted by id then version."""

        return tuple(
            self._strategies[key]
            for key in sorted(
                self._strategies,
                key=lambda item: (item[0].value, item[1]),
            )
        )
