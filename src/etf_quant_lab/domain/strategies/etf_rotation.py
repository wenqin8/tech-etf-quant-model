"""Momentum rotation strategy over the ETF universe (node 9).

Ranking pipeline per decision date: composite momentum over three trailing
windows, risk-adjusted by realized volatility, gated by a long-trend filter,
then the top ``holdings_count`` symbols are equally weighted under a per-symbol
cap and a minimum cash floor.  Every allocation carries its score and readable
reason codes so the UI and reports can explain each position.

Anti-lookahead: only trailing statistics over the as-of-bounded slice are used.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise

from etf_quant_lab.contracts.enums import SignalAction, StrategyId
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
)
from etf_quant_lab.domain.strategy import StrategyContext

REASON_MOMENTUM_TOP_RANK = "MOMENTUM_TOP_RANK"
REASON_ABOVE_TREND_FILTER = "ABOVE_TREND_FILTER"
REASON_TREND_FILTER_BYPASSED = "TREND_FILTER_BYPASSED"
WARNING_NO_CANDIDATE = "NO_TRADEABLE_SYMBOL"
WARNING_INSUFFICIENT_CANDIDATES = "INSUFFICIENT_CANDIDATES"


@dataclass(frozen=True, slots=True)
class _Candidate:
    """One symbol that passed all filters, with its explainable score."""

    symbol: str
    score: Decimal
    reason_codes: tuple[str, ...]


class EtfRotationStrategy:
    """Rotate into the strongest risk-adjusted momentum ETFs in an uptrend."""

    strategy_id = StrategyId.ETF_ROTATION
    version = "1.0.0"

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        return (
            ParameterSpec(
                name="momentum_window_short",
                type="int",
                default=20,
                minimum=Decimal(2),
                maximum=Decimal(250),
                description="短动量窗口(交易日)",
            ),
            ParameterSpec(
                name="momentum_window_medium",
                type="int",
                default=60,
                minimum=Decimal(2),
                maximum=Decimal(250),
                description="中动量窗口(交易日)",
            ),
            ParameterSpec(
                name="momentum_window_long",
                type="int",
                default=120,
                minimum=Decimal(2),
                maximum=Decimal(500),
                description="长动量窗口(交易日)",
            ),
            ParameterSpec(
                name="volatility_window",
                type="int",
                default=20,
                minimum=Decimal(5),
                maximum=Decimal(250),
                description="已实现波动窗口(交易日)",
            ),
            ParameterSpec(
                name="trend_filter_days",
                type="int",
                default=120,
                minimum=Decimal(2),
                maximum=Decimal(500),
                description="趋势过滤均线窗口(交易日)",
            ),
            ParameterSpec(
                name="holdings_count",
                type="int",
                default=3,
                minimum=Decimal(1),
                maximum=Decimal(10),
                description="目标持仓数量",
            ),
            ParameterSpec(
                name="maximum_position_weight",
                type="float",
                default=Decimal("0.35"),
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
            ParameterSpec(
                name="cash_when_all_filtered",
                type="bool",
                default=True,
                description="全部被趋势过滤时是否持币(否则按动量放行并警告)",
            ),
        )

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        windows = (
            _optional_int(parameters, "momentum_window_long", 120),
            _optional_int(parameters, "trend_filter_days", 120),
            _optional_int(parameters, "volatility_window", 20),
        )
        return max(windows) + 1  # returns need one prior close

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        holdings_count = _require_int(parameters, "holdings_count")
        max_weight = Decimal(str(parameters["maximum_position_weight"]))
        min_cash = Decimal(str(parameters["minimum_cash_weight"]))
        cash_when_all_filtered = bool(parameters["cash_when_all_filtered"])

        scored, filtered_out = self._score_universe(context, parameters)
        warnings: list[str] = []

        candidates = scored
        if not candidates and filtered_out and not cash_when_all_filtered:
            # Explicit opt-out of the trend gate: fall back to momentum ranking.
            candidates = [
                _Candidate(
                    symbol=item.symbol,
                    score=item.score,
                    reason_codes=(*item.reason_codes, REASON_TREND_FILTER_BYPASSED),
                )
                for item in filtered_out
            ]
            warnings.append(REASON_TREND_FILTER_BYPASSED)

        if not candidates:
            return TargetPortfolio(
                as_of_date=context.as_of_date,
                strategy_id=self.strategy_id,
                version=self.version,
                allocations=(),
                cash_weight=Decimal(1),
                warnings=(WARNING_NO_CANDIDATE,),
            )

        # Rank by score descending; ties break by symbol for determinism.
        ranked = sorted(candidates, key=lambda item: (-item.score, item.symbol))
        selected = ranked[:holdings_count]
        if len(selected) < holdings_count:
            warnings.append(WARNING_INSUFFICIENT_CANDIDATES)

        investable = Decimal(1) - min_cash
        weight = min(max_weight, investable / Decimal(len(selected)))
        allocations = tuple(
            TargetAllocation(
                symbol=candidate.symbol,
                target_weight=weight,
                action=SignalAction.BUY,
                score=candidate.score,
                reason_codes=(REASON_MOMENTUM_TOP_RANK, *candidate.reason_codes),
            )
            for candidate in sorted(selected, key=lambda item: item.symbol)
        )
        cash_weight = Decimal(1) - weight * Decimal(len(allocations))
        return TargetPortfolio(
            as_of_date=context.as_of_date,
            strategy_id=self.strategy_id,
            version=self.version,
            allocations=allocations,
            cash_weight=cash_weight,
            warnings=tuple(warnings),
        )

    def _score_universe(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> tuple[list[_Candidate], list[_Candidate]]:
        """Return (passed, trend-filtered-out) candidates with scores."""

        short_window = _require_int(parameters, "momentum_window_short")
        medium_window = _require_int(parameters, "momentum_window_medium")
        long_window = _require_int(parameters, "momentum_window_long")
        volatility_window = _require_int(parameters, "volatility_window")
        trend_days = _require_int(parameters, "trend_filter_days")
        warmup = self.warmup_bars(parameters)

        passed: list[_Candidate] = []
        filtered_out: list[_Candidate] = []
        for symbol in context.universe_symbols:
            history = context.market_data.history(symbol)
            if len(history) < warmup:
                continue
            closes = [bar.close for bar in history]
            momentum = _composite_momentum(
                closes, (short_window, medium_window, long_window)
            )
            volatility = _realized_volatility(closes, volatility_window)
            if volatility <= 0:
                # A flat price series over the window is untradeable data noise.
                continue
            score = momentum / volatility
            candidate = _Candidate(symbol=symbol, score=score, reason_codes=())
            trend_ma = _simple_moving_average(closes, trend_days)
            if closes[-1] > trend_ma:
                passed.append(
                    _Candidate(
                        symbol=symbol,
                        score=score,
                        reason_codes=(REASON_ABOVE_TREND_FILTER,),
                    )
                )
            else:
                filtered_out.append(candidate)
        return passed, filtered_out

    def code_hash(self) -> str:
        """Return a stable digest of the strategy source for version pinning."""

        source = inspect.getsource(type(self))
        return hashlib.sha256(source.encode()).hexdigest()

    def describe(self) -> str:
        """Return human-readable strategy logic for the UI and reports."""

        return (
            "ETF 轮动策略: 以三窗口复合动量除以已实现波动得到风险调整评分, "
            "仅保留收盘价高于趋势均线的标的, 按评分取前 N 名等权持有, "
            "受单标的上限与最小现金比例约束; 无合格标的时全部持币。"
        )


def _require_int(parameters: Mapping[str, object], name: str) -> int:
    value = parameters[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"parameter {name} must be an integer")
    return value


def _optional_int(parameters: Mapping[str, object], name: str, fallback: int) -> int:
    value = parameters.get(name, fallback)
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value


def _composite_momentum(closes: list[Decimal], windows: tuple[int, ...]) -> Decimal:
    """Average trailing return across the configured lookback windows."""

    total = Decimal(0)
    for window in windows:
        past = closes[-(window + 1)]
        if past <= 0:
            raise ValueError("historical close must be positive")
        total += (closes[-1] - past) / past
    return total / Decimal(len(windows))


def _realized_volatility(closes: list[Decimal], window: int) -> Decimal:
    """Population standard deviation of daily simple returns over the window."""

    if window < 2:
        raise ValueError("volatility window must cover at least two returns")
    tail = closes[-(window + 1) :]
    returns = [
        (current - previous) / previous
        for previous, current in pairwise(tail)
        if previous > 0
    ]
    if len(returns) < 2:
        return Decimal(0)
    mean = sum(returns, Decimal(0)) / Decimal(len(returns))
    variance = sum(((value - mean) ** 2 for value in returns), Decimal(0)) / Decimal(
        len(returns)
    )
    return variance.sqrt()


def _simple_moving_average(values: list[Decimal], window: int) -> Decimal:
    if window <= 0:
        raise ValueError("window must be positive")
    tail = values[-window:]
    return sum(tail, Decimal(0)) / Decimal(len(tail))
