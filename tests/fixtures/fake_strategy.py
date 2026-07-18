"""A deterministic in-memory strategy used across strategy service tests."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from etf_quant_lab.contracts.enums import SignalAction, StrategyId
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
)
from etf_quant_lab.domain.strategy import StrategyContext


class MomentumTopNStrategy:
    """Rank symbols by close-over-first-bar return and equally weight the top N.

    Deterministic and pure: it reads only the as-of slice, so its output depends
    solely on data at or before ``as_of_date``.  Ties break by symbol so row order
    never changes the result.
    """

    strategy_id = StrategyId.ETF_ROTATION
    version = "1.0.0"

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        return (
            ParameterSpec(
                name="lookback_days",
                type="int",
                default=20,
                minimum=Decimal(2),
                maximum=Decimal(250),
            ),
            ParameterSpec(
                name="top_n",
                type="int",
                default=1,
                minimum=Decimal(1),
                maximum=Decimal(10),
            ),
            ParameterSpec(
                name="max_weight_per_symbol",
                type="float",
                default=Decimal("1.0"),
                minimum=Decimal("0.01"),
                maximum=Decimal("1.0"),
            ),
        )

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        lookback = parameters.get("lookback_days", 20)
        return int(lookback) if isinstance(lookback, int) else 20

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        top_n = int(parameters["top_n"])  # type: ignore[arg-type]
        max_weight = Decimal(str(parameters["max_weight_per_symbol"]))
        warmup = self.warmup_bars(parameters)

        scored: list[tuple[Decimal, str]] = []
        for symbol in context.universe_symbols:
            history = context.market_data.history(symbol)
            if len(history) < warmup:
                continue
            first_close = history[0].close
            last_close = history[-1].close
            if first_close <= 0:
                continue
            momentum = (last_close - first_close) / first_close
            scored.append((momentum, symbol))

        # Sort by score descending, breaking ties by symbol for stability.
        scored.sort(key=lambda item: (-item[0], item[1]))
        selected = scored[:top_n]
        if not selected:
            return TargetPortfolio(
                as_of_date=context.as_of_date,
                strategy_id=self.strategy_id,
                version=self.version,
                allocations=(),
                cash_weight=Decimal(1),
                warnings=("NO_TRADEABLE_SYMBOL",),
            )

        weight = min(max_weight, (Decimal(1) / Decimal(len(selected))))
        allocations = tuple(
            TargetAllocation(
                symbol=symbol,
                target_weight=weight,
                action=SignalAction.BUY,
                score=score,
                reason_codes=("MOMENTUM_TOP_RANK",),
            )
            for score, symbol in sorted(selected, key=lambda item: item[1])
        )
        cash_weight = Decimal(1) - weight * Decimal(len(allocations))
        return TargetPortfolio(
            as_of_date=context.as_of_date,
            strategy_id=self.strategy_id,
            version=self.version,
            allocations=allocations,
            cash_weight=cash_weight,
        )
