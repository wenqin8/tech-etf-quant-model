"""Dual moving-average trend baseline strategy (node 8).

The strategy holds every universe symbol whose fast simple moving average is
above its slow SMA as of the decision date, equally weighting the holdings under
a per-symbol cap and a minimum cash floor.  It is the reference implementation
that proves a strategy can be added by implementing the node-7 ``Strategy``
protocol alone, with no change to the service or (future) backtest layers.

Anti-lookahead: it only reads ``context.market_data`` (an as-of-bounded slice),
uses trailing SMAs, and never calls ``shift(-1)`` or a system clock.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from decimal import Decimal

from etf_quant_lab.contracts.enums import SignalAction, StrategyId
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
)
from etf_quant_lab.domain.strategy import StrategyContext

REASON_TREND_UP = "TREND_FAST_ABOVE_SLOW"
REASON_TREND_DOWN = "TREND_FAST_BELOW_SLOW"
REASON_INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"
WARNING_NO_TREND = "NO_UPTREND_SYMBOL"


class TrendBaselineStrategy:
    """Equal-weight the symbols in a confirmed fast-over-slow SMA uptrend."""

    strategy_id = StrategyId.TREND_BASELINE
    version = "1.0.0"

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        return (
            ParameterSpec(
                name="fast_window",
                type="int",
                default=20,
                minimum=Decimal(2),
                maximum=Decimal(120),
                description="快速均线窗口(交易日)",
            ),
            ParameterSpec(
                name="slow_window",
                type="int",
                default=60,
                minimum=Decimal(5),
                maximum=Decimal(250),
                description="慢速均线窗口(交易日)",
            ),
            ParameterSpec(
                name="maximum_position_weight",
                type="float",
                default=Decimal("0.25"),
                minimum=Decimal("0.01"),
                maximum=Decimal("1.0"),
                description="单标的最大权重",
            ),
            ParameterSpec(
                name="minimum_cash_weight",
                type="float",
                default=Decimal("0.10"),
                minimum=Decimal("0"),
                maximum=Decimal("1.0"),
                description="最小现金比例",
            ),
        )

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        slow_window = parameters.get("slow_window", 60)
        return slow_window if isinstance(slow_window, int) else 60

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        fast_window = _require_int(parameters, "fast_window")
        slow_window = _require_int(parameters, "slow_window")
        max_weight = Decimal(str(parameters["maximum_position_weight"]))
        min_cash = Decimal(str(parameters["minimum_cash_weight"]))
        if fast_window >= slow_window:
            raise ValueError("fast_window must be smaller than slow_window")

        uptrend = self._select_uptrend_symbols(
            context, fast_window=fast_window, slow_window=slow_window
        )
        if not uptrend:
            return TargetPortfolio(
                as_of_date=context.as_of_date,
                strategy_id=self.strategy_id,
                version=self.version,
                allocations=(),
                cash_weight=Decimal(1),
                warnings=(WARNING_NO_TREND,),
            )

        weight = self._per_symbol_weight(len(uptrend), max_weight=max_weight, min_cash=min_cash)
        allocations = tuple(
            TargetAllocation(
                symbol=symbol,
                target_weight=weight,
                action=SignalAction.BUY,
                score=score,
                reason_codes=(REASON_TREND_UP,),
            )
            for symbol, score in uptrend
        )
        cash_weight = Decimal(1) - weight * Decimal(len(allocations))
        return TargetPortfolio(
            as_of_date=context.as_of_date,
            strategy_id=self.strategy_id,
            version=self.version,
            allocations=allocations,
            cash_weight=cash_weight,
        )

    def _select_uptrend_symbols(
        self,
        context: StrategyContext,
        *,
        fast_window: int,
        slow_window: int,
    ) -> list[tuple[str, Decimal]]:
        selected: list[tuple[str, Decimal]] = []
        for symbol in context.universe_symbols:
            history = context.market_data.history(symbol)
            if len(history) < slow_window:
                continue
            closes = [bar.close for bar in history]
            fast_ma = _simple_moving_average(closes, fast_window)
            slow_ma = _simple_moving_average(closes, slow_window)
            if fast_ma > slow_ma:
                # Score is the trend spread, so ordering is deterministic and explainable.
                score = (fast_ma - slow_ma) / slow_ma
                selected.append((symbol, score))
        # Sort by symbol so equal weights are assigned in a stable order.
        selected.sort(key=lambda item: item[0])
        return selected

    @staticmethod
    def _per_symbol_weight(
        count: int,
        *,
        max_weight: Decimal,
        min_cash: Decimal,
    ) -> Decimal:
        investable = Decimal(1) - min_cash
        equal_weight = investable / Decimal(count)
        return min(max_weight, equal_weight)

    def code_hash(self) -> str:
        """Return a stable digest of the strategy source for version pinning."""

        source = inspect.getsource(type(self))
        return hashlib.sha256(source.encode()).hexdigest()

    def describe(self) -> str:
        """Return human-readable strategy logic for the UI and reports."""

        return (
            "趋势基准策略: 当某标的的快速均线高于慢速均线时判定为上升趋势并等权买入, "
            "受单标的上限与最小现金比例约束; 无标的处于上升趋势时全部持币。"
        )


def _require_int(parameters: Mapping[str, object], name: str) -> int:
    value = parameters[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"parameter {name} must be an integer")
    return value


def _simple_moving_average(values: list[Decimal], window: int) -> Decimal:
    if window <= 0:
        raise ValueError("window must be positive")
    tail = values[-window:]
    return sum(tail, Decimal(0)) / Decimal(len(tail))
