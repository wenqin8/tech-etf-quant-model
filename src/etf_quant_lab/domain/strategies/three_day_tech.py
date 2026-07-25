"""Regime-aware pullback strategy for the focused technology ETF portfolio.

The portfolio has three independent sleeves:

* semiconductor equipment: 40%;
* semiconductor chips: 30%;
* innovation drug or robotics: 15%;
* cash floor: 15%.

Entries require a trailing pullback.  Once held, an ETF may remain invested
without a time limit while its medium trend is up.  In sideways/down regimes the
position behaves as a short swing.  Risk is managed with entry-time ATR,
break-even and time stops.  Profits are harvested with a 3:5:2 staged plan,
trailing drawdown, volume-climax exit and extreme-profit protection.  All inputs
are explicit in :class:`StrategyContext`; no clock, network, database, or future
bar is accessible here.
"""

from __future__ import annotations

import hashlib
import inspect
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import SignalAction, StrategyId
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
)
from etf_quant_lab.domain.strategy import StrategyContext

EQUIPMENT_SYMBOLS = ("159516.SZ", "588170.SH")
CHIP_SYMBOLS = ("159995.SZ",)
SATELLITE_SYMBOLS = ("159992.SZ", "562500.SH")

REASON_ENTRY_PULLBACK = "ENTRY_THREE_DAY_PULLBACK"
REASON_REVERSAL_CONFIRMED = "ENTRY_REVERSAL_CONFIRMED"
REASON_REGIME_UPTREND = "REGIME_UPTREND"
REASON_REGIME_SIDEWAYS = "REGIME_SIDEWAYS"
REASON_REGIME_DOWNTREND = "REGIME_DOWNTREND"
REASON_HOLD_UPTREND = "HOLD_UPTREND_NO_TIME_LIMIT"
REASON_HOLD_SWING = "HOLD_SHORT_SWING"
REASON_EXIT_ATR_STOP = "EXIT_ATR_STOP"
REASON_EXIT_BREAKEVEN_STOP = "EXIT_BREAKEVEN_STOP"
REASON_EXIT_TIME_STOP = "EXIT_FIVE_DAY_NO_BREAKOUT"
REASON_REDUCE_FIRST_PROFIT = "REDUCE_FIRST_PROFIT_TO_SEVENTY_PERCENT"
REASON_REDUCE_TRAILING_PROFIT = "REDUCE_TRAILING_PROFIT_TO_TWENTY_PERCENT"
REASON_EXIT_CLIMAX = "EXIT_VOLUME_CLIMAX_REMAINDER"
REASON_EXIT_EXTREME_PROTECTION = "EXIT_EXTREME_PROFIT_PROTECTION"
REASON_EXIT_SWING_WINDOW = "EXIT_SWING_WINDOW_COMPLETE"
REASON_EXIT_DUPLICATE_GROUP = "EXIT_DUPLICATE_GROUP_POSITION"
REASON_GROUP_EQUIPMENT = "GROUP_SEMICONDUCTOR_EQUIPMENT"
REASON_GROUP_CHIP = "GROUP_SEMICONDUCTOR_CHIP"
REASON_GROUP_SATELLITE = "GROUP_INNOVATION_DRUG_OR_ROBOTICS"
WARNING_NO_NEW_ENTRY = "NO_NEW_PULLBACK_ENTRY"
WARNING_ENTRY_PRICE_MISSING = "POSITION_ENTRY_PRICE_MISSING"
WARNING_ENTRY_ATR_MISSING = "POSITION_ENTRY_ATR_MISSING"


@dataclass(frozen=True, slots=True)
class _Group:
    symbols: tuple[str, ...]
    weight_parameter: str
    entry_weight_parameter: str
    reason: str


@dataclass(frozen=True, slots=True)
class _Candidate:
    symbol: str
    score: Decimal
    pullback_return: Decimal
    regime_reason: str


_GROUPS = (
    _Group(
        EQUIPMENT_SYMBOLS,
        "equipment_weight",
        "equipment_entry_weight",
        REASON_GROUP_EQUIPMENT,
    ),
    _Group(
        CHIP_SYMBOLS,
        "chip_weight",
        "chip_entry_weight",
        REASON_GROUP_CHIP,
    ),
    _Group(
        SATELLITE_SYMBOLS,
        "satellite_weight",
        "satellite_entry_weight",
        REASON_GROUP_SATELLITE,
    ),
)


class ThreeDayTechStrategy:
    """Buy focused ETF pullbacks and adapt holding time to the market regime."""

    strategy_id = StrategyId.THREE_DAY_TECH
    version = "1.0.0"

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        return (
            ParameterSpec(
                name="pullback_window",
                type="int",
                default=3,
                minimum=Decimal(2),
                maximum=Decimal(10),
                description="回撤观察窗口(交易日)",
            ),
            ParameterSpec(
                name="pullback_threshold",
                type="float",
                default=Decimal("-0.075"),
                minimum=Decimal("-0.20"),
                maximum=Decimal("-0.005"),
                description="触发买入的窗口累计跌幅",
            ),
            ParameterSpec(
                name="require_reversal_confirmation",
                type="bool",
                default=True,
                description="深度回撤后是否等待首个反弹日再买入",
            ),
            ParameterSpec(
                name="trend_fast_window",
                type="int",
                default=20,
                minimum=Decimal(3),
                maximum=Decimal(120),
                description="趋势快速均线窗口",
            ),
            ParameterSpec(
                name="trend_slow_window",
                type="int",
                default=60,
                minimum=Decimal(10),
                maximum=Decimal(250),
                description="趋势慢速均线窗口",
            ),
            ParameterSpec(
                name="sideways_band",
                type="float",
                default=Decimal("0.02"),
                minimum=Decimal("0"),
                maximum=Decimal("0.10"),
                description="快慢均线判定震荡区间的容差",
            ),
            ParameterSpec(
                name="swing_holding_bars",
                type="int",
                default=5,
                minimum=Decimal(1),
                maximum=Decimal(20),
                description="震荡或下跌行情的最长持有交易日",
            ),
            ParameterSpec(
                name="atr_window",
                type="int",
                default=10,
                minimum=Decimal(5),
                maximum=Decimal(60),
                description="入场ATR计算窗口",
            ),
            ParameterSpec(
                name="initial_stop_atr_multiple",
                type="float",
                default=Decimal("3"),
                minimum=Decimal("1"),
                maximum=Decimal("6"),
                description="初始止损距离的ATR倍数",
            ),
            ParameterSpec(
                name="breakeven_trigger_atr_multiple",
                type="float",
                default=Decimal("1.5"),
                minimum=Decimal("0.5"),
                maximum=Decimal("5"),
                description="盈利达到该ATR倍数后止损抬至成本",
            ),
            ParameterSpec(
                name="time_stop_bars",
                type="int",
                default=5,
                minimum=Decimal(2),
                maximum=Decimal(20),
                description="爆发点未出现时的时间止损天数",
            ),
            ParameterSpec(
                name="time_stop_min_return",
                type="float",
                default=Decimal("0.02"),
                minimum=Decimal("0"),
                maximum=Decimal("0.10"),
                description="时间止损要求达到的最低最高收益",
            ),
            ParameterSpec(
                name="first_profit_atr_multiple",
                type="float",
                default=Decimal("3"),
                minimum=Decimal("1"),
                maximum=Decimal("10"),
                description="第一批止盈触发的ATR倍数",
            ),
            ParameterSpec(
                name="first_profit_remaining_fraction",
                type="float",
                default=Decimal("0.70"),
                minimum=Decimal("0.20"),
                maximum=Decimal("1"),
                description="第一批止盈后保留的原始仓位比例",
            ),
            ParameterSpec(
                name="trailing_profit_drawdown",
                type="float",
                default=Decimal("0.09"),
                minimum=Decimal("0.03"),
                maximum=Decimal("0.25"),
                description="第二批止盈的持仓高点回撤比例",
            ),
            ParameterSpec(
                name="second_profit_remaining_fraction",
                type="float",
                default=Decimal("0.20"),
                minimum=Decimal("0"),
                maximum=Decimal("0.50"),
                description="第二批止盈后保留的原始仓位比例",
            ),
            ParameterSpec(
                name="volume_window",
                type="int",
                default=20,
                minimum=Decimal(5),
                maximum=Decimal(60),
                description="放量判断的均量窗口",
            ),
            ParameterSpec(
                name="volume_surge_multiple",
                type="float",
                default=Decimal("2"),
                minimum=Decimal("1"),
                maximum=Decimal("10"),
                description="情绪高潮的成交量放大倍数",
            ),
            ParameterSpec(
                name="upper_shadow_body_multiple",
                type="float",
                default=Decimal("2"),
                minimum=Decimal("1"),
                maximum=Decimal("10"),
                description="长上影相对K线实体的最小倍数",
            ),
            ParameterSpec(
                name="acceleration_daily_return",
                type="float",
                default=Decimal("0.05"),
                minimum=Decimal("0.02"),
                maximum=Decimal("0.20"),
                description="连续加速条件的单日最低涨幅",
            ),
            ParameterSpec(
                name="bollinger_window",
                type="int",
                default=20,
                minimum=Decimal(10),
                maximum=Decimal(60),
                description="布林线上轨计算窗口",
            ),
            ParameterSpec(
                name="bollinger_std_multiple",
                type="float",
                default=Decimal("2"),
                minimum=Decimal("1"),
                maximum=Decimal("4"),
                description="布林线上轨的标准差倍数",
            ),
            ParameterSpec(
                name="extreme_profit",
                type="float",
                default=Decimal("0.50"),
                minimum=Decimal("0.20"),
                maximum=Decimal("2"),
                description="极值保护的最低持仓收益",
            ),
            ParameterSpec(
                name="extreme_daily_drop",
                type="float",
                default=Decimal("0.07"),
                minimum=Decimal("0.03"),
                maximum=Decimal("0.20"),
                description="极值保护触发的单日跌幅",
            ),
            ParameterSpec(
                name="equipment_weight",
                type="float",
                default=Decimal("0.40"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="半导体设备目标权重",
            ),
            ParameterSpec(
                name="equipment_entry_weight",
                type="float",
                default=Decimal("0.20"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="半导体设备首次底仓权重",
            ),
            ParameterSpec(
                name="chip_weight",
                type="float",
                default=Decimal("0.30"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="半导体芯片目标权重",
            ),
            ParameterSpec(
                name="chip_entry_weight",
                type="float",
                default=Decimal("0.10"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="半导体芯片首次底仓权重",
            ),
            ParameterSpec(
                name="satellite_weight",
                type="float",
                default=Decimal("0.15"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="创新药或机器人目标权重",
            ),
            ParameterSpec(
                name="satellite_entry_weight",
                type="float",
                default=Decimal("0.05"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="创新药或机器人出现独立信号时的机会仓权重",
            ),
            ParameterSpec(
                name="minimum_cash_weight",
                type="float",
                default=Decimal("0.15"),
                minimum=Decimal("0"),
                maximum=Decimal("1"),
                description="最低现金权重",
            ),
        )

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        slow = _optional_int(parameters, "trend_slow_window", 60)
        pullback = _optional_int(parameters, "pullback_window", 3)
        # Crossing detection compares today's pullback with yesterday's.
        return max(slow, pullback + 2)

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        self._validate_relationships(parameters)
        allocations: list[TargetAllocation] = []
        warnings: list[str] = []
        positive_allocations = 0

        for group in _GROUPS:
            group_weight = Decimal(str(parameters[group.weight_parameter]))
            group_allocations, group_warnings = self._allocate_group(
                context=context,
                parameters=parameters,
                group=group,
                target_weight=group_weight,
                entry_weight=Decimal(
                    str(parameters[group.entry_weight_parameter])
                ),
            )
            allocations.extend(group_allocations)
            warnings.extend(group_warnings)
            positive_allocations += sum(
                allocation.target_weight > 0 for allocation in group_allocations
            )

        if positive_allocations == 0 and not any(
            allocation.action == SignalAction.SELL for allocation in allocations
        ):
            warnings.append(WARNING_NO_NEW_ENTRY)

        invested = sum(
            (allocation.target_weight for allocation in allocations), Decimal(0)
        )
        return TargetPortfolio(
            as_of_date=context.as_of_date,
            strategy_id=self.strategy_id,
            version=self.version,
            allocations=tuple(sorted(allocations, key=lambda item: item.symbol)),
            cash_weight=Decimal(1) - invested,
            warnings=tuple(dict.fromkeys(warnings)),
        )

    def _allocate_group(
        self,
        *,
        context: StrategyContext,
        parameters: Mapping[str, object],
        group: _Group,
        target_weight: Decimal,
        entry_weight: Decimal,
    ) -> tuple[list[TargetAllocation], list[str]]:
        available = tuple(
            symbol for symbol in group.symbols if symbol in context.universe_symbols
        )
        held = sorted(
            (
                symbol
                for symbol in available
                if context.current_weights.get(symbol, Decimal(0)) > 0
            ),
            key=lambda symbol: (-context.current_weights[symbol], symbol),
        )
        warnings: list[str] = []
        allocations: list[TargetAllocation] = []

        if held:
            primary = held[0]
            for duplicate in held[1:]:
                allocations.append(
                    TargetAllocation(
                        symbol=duplicate,
                        target_weight=Decimal(0),
                        action=SignalAction.SELL,
                        reason_codes=(group.reason, REASON_EXIT_DUPLICATE_GROUP),
                    )
                )
            primary_allocation, warning = self._held_allocation(
                context=context,
                parameters=parameters,
                symbol=primary,
                target_weight=target_weight,
                group_reason=group.reason,
            )
            allocations.append(primary_allocation)
            if warning:
                warnings.append(warning)
            return allocations, warnings

        if target_weight <= 0 or entry_weight <= 0:
            return allocations, warnings
        candidate = self._select_entry(context, parameters, available)
        if candidate is None:
            return allocations, warnings
        allocations.append(
            TargetAllocation(
                symbol=candidate.symbol,
                target_weight=entry_weight,
                action=SignalAction.BUY,
                score=candidate.score,
                reason_codes=(
                    group.reason,
                    REASON_ENTRY_PULLBACK,
                    *(
                        (REASON_REVERSAL_CONFIRMED,)
                        if bool(parameters["require_reversal_confirmation"])
                        else ()
                    ),
                    candidate.regime_reason,
                ),
            )
        )
        return allocations, warnings

    def _held_allocation(
        self,
        *,
        context: StrategyContext,
        parameters: Mapping[str, object],
        symbol: str,
        target_weight: Decimal,
        group_reason: str,
    ) -> tuple[TargetAllocation, str | None]:
        latest = context.market_data.latest(symbol)
        if latest is None:
            return (
                TargetAllocation(
                    symbol=symbol,
                    target_weight=target_weight,
                    action=SignalAction.HOLD,
                    reason_codes=(group_reason, REASON_HOLD_SWING),
                ),
                None,
            )

        entry_price = context.position_entry_prices.get(symbol)
        profit: Decimal | None = None
        warning: str | None = None
        current_weight = context.current_weights.get(symbol, target_weight)
        if entry_price is None:
            warning = WARNING_ENTRY_PRICE_MISSING
        else:
            profit = (latest.close - entry_price) / entry_price
            history = context.market_data.history(symbol)
            holding_bars = context.position_holding_bars.get(symbol, 0)
            entry_atr = _entry_atr(
                history=history,
                holding_bars=holding_bars,
                window=_require_int(parameters, "atr_window"),
            )
            if entry_atr is None:
                warning = WARNING_ENTRY_ATR_MISSING
            else:
                visible = _bars_since_entry(
                    history=history,
                    holding_bars=holding_bars,
                )
                highest_high = max(bar.high for bar in visible)
                highest_close = max(bar.close for bar in visible)
                daily_return = (
                    (latest.close - history[-2].close) / history[-2].close
                    if len(history) >= 2 and history[-2].close > 0
                    else Decimal(0)
                )

                if (
                    profit >= Decimal(str(parameters["extreme_profit"]))
                    and daily_return
                    <= -Decimal(str(parameters["extreme_daily_drop"]))
                ):
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=Decimal(0),
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(
                                group_reason,
                                REASON_EXIT_EXTREME_PROTECTION,
                            ),
                        ),
                        warning,
                    )

                initial_stop = entry_price - entry_atr * Decimal(
                    str(parameters["initial_stop_atr_multiple"])
                )
                prior_high = max(
                    (bar.high for bar in visible[:-1]),
                    default=entry_price,
                )
                breakeven_active = prior_high >= entry_price + entry_atr * Decimal(
                    str(parameters["breakeven_trigger_atr_multiple"])
                )
                stop_line = max(
                    initial_stop,
                    entry_price if breakeven_active else initial_stop,
                )
                if latest.low <= stop_line:
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=Decimal(0),
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(
                                group_reason,
                                (
                                    REASON_EXIT_BREAKEVEN_STOP
                                    if breakeven_active
                                    else REASON_EXIT_ATR_STOP
                                ),
                            ),
                        ),
                        warning,
                    )

                if (
                    holding_bars >= _require_int(parameters, "time_stop_bars")
                    and highest_close
                    < entry_price
                    * (
                        Decimal(1)
                        + Decimal(str(parameters["time_stop_min_return"]))
                    )
                ):
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=Decimal(0),
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(group_reason, REASON_EXIT_TIME_STOP),
                        ),
                        warning,
                    )

                first_remaining = Decimal(
                    str(parameters["first_profit_remaining_fraction"])
                )
                second_remaining = Decimal(
                    str(parameters["second_profit_remaining_fraction"])
                )
                stage_one_done = current_weight <= target_weight * (
                    first_remaining + Decimal("0.10")
                )
                stage_two_done = current_weight <= target_weight * (
                    second_remaining + Decimal("0.10")
                )

                if stage_two_done and _is_final_exit_signal(history, parameters):
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=Decimal(0),
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(group_reason, REASON_EXIT_CLIMAX),
                        ),
                        warning,
                    )

                drawdown = (latest.close - highest_high) / highest_high
                if (
                    stage_one_done
                    and not stage_two_done
                    and drawdown
                    <= -Decimal(str(parameters["trailing_profit_drawdown"]))
                ):
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=target_weight * second_remaining,
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(
                                group_reason,
                                REASON_REDUCE_TRAILING_PROFIT,
                            ),
                        ),
                        warning,
                    )

                first_trigger = entry_price + entry_atr * Decimal(
                    str(parameters["first_profit_atr_multiple"])
                )
                if not stage_one_done and latest.close >= first_trigger:
                    return (
                        TargetAllocation(
                            symbol=symbol,
                            target_weight=target_weight * first_remaining,
                            action=SignalAction.SELL,
                            score=profit,
                            reason_codes=(
                                group_reason,
                                REASON_REDUCE_FIRST_PROFIT,
                            ),
                        ),
                        warning,
                    )

        regime_reason = self._regime_reason(context, parameters, symbol)
        if regime_reason == REASON_REGIME_UPTREND:
            return (
                TargetAllocation(
                    symbol=symbol,
                    target_weight=min(
                        current_weight,
                        target_weight,
                    ),
                    action=SignalAction.HOLD,
                    score=profit,
                    reason_codes=(group_reason, REASON_HOLD_UPTREND, regime_reason),
                ),
                warning,
            )

        holding_bars = context.position_holding_bars.get(symbol, 0)
        if holding_bars >= _require_int(parameters, "swing_holding_bars"):
            return (
                TargetAllocation(
                    symbol=symbol,
                    target_weight=Decimal(0),
                    action=SignalAction.SELL,
                    score=profit,
                    reason_codes=(
                        group_reason,
                        REASON_EXIT_SWING_WINDOW,
                        regime_reason,
                    ),
                ),
                warning,
            )
        return (
            TargetAllocation(
                symbol=symbol,
                target_weight=min(
                    current_weight,
                    target_weight,
                ),
                action=SignalAction.HOLD,
                score=profit,
                reason_codes=(group_reason, REASON_HOLD_SWING, regime_reason),
            ),
            warning,
        )

    def _select_entry(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
        symbols: tuple[str, ...],
    ) -> _Candidate | None:
        window = _require_int(parameters, "pullback_window")
        threshold = Decimal(str(parameters["pullback_threshold"]))
        candidates: list[_Candidate] = []
        for symbol in symbols:
            history = context.market_data.history(symbol)
            if len(history) < self.warmup_bars(parameters):
                continue
            current = history[-1].close
            prior = history[-(window + 1)].close
            previous_current = history[-2].close
            previous_prior = history[-(window + 2)].close
            if prior <= 0 or previous_prior <= 0:
                continue
            pullback = (current - prior) / prior
            previous_pullback = (previous_current - previous_prior) / previous_prior
            if bool(parameters["require_reversal_confirmation"]):
                # The prior session must have completed the deep pullback; buy
                # only after price turns up while the window remains depressed.
                if (
                    previous_pullback > threshold
                    or current <= previous_current
                    or pullback > threshold / Decimal(2)
                ):
                    continue
                score = -previous_pullback
            else:
                # One signal per pullback episode: fire only on the first
                # threshold crossing, not every day below the threshold.
                if pullback > threshold or previous_pullback <= threshold:
                    continue
                score = -pullback
            regime_reason = self._regime_reason(context, parameters, symbol)
            candidates.append(
                _Candidate(
                    symbol=symbol,
                    score=score,
                    pullback_return=pullback,
                    regime_reason=regime_reason,
                )
            )
        if not candidates:
            return None
        return sorted(candidates, key=lambda item: (-item.score, item.symbol))[0]

    def _regime_reason(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
        symbol: str,
    ) -> str:
        history = context.market_data.history(symbol)
        fast_window = _require_int(parameters, "trend_fast_window")
        slow_window = _require_int(parameters, "trend_slow_window")
        if len(history) < slow_window:
            return REASON_REGIME_SIDEWAYS
        closes = [bar.close for bar in history]
        fast = _simple_moving_average(closes, fast_window)
        slow = _simple_moving_average(closes, slow_window)
        latest = closes[-1]
        if fast > slow and latest > slow:
            return REASON_REGIME_UPTREND
        spread = (fast - slow) / slow
        if spread < -Decimal(str(parameters["sideways_band"])):
            return REASON_REGIME_DOWNTREND
        return REASON_REGIME_SIDEWAYS

    @staticmethod
    def _validate_relationships(parameters: Mapping[str, object]) -> None:
        fast = _require_int(parameters, "trend_fast_window")
        slow = _require_int(parameters, "trend_slow_window")
        if fast >= slow:
            raise ValueError("trend_fast_window must be smaller than trend_slow_window")
        invested = sum(
            (
                Decimal(str(parameters["equipment_weight"])),
                Decimal(str(parameters["chip_weight"])),
                Decimal(str(parameters["satellite_weight"])),
            ),
            Decimal(0),
        )
        minimum_cash = Decimal(str(parameters["minimum_cash_weight"]))
        if invested + minimum_cash > Decimal(1):
            raise ValueError("target sleeve weights plus minimum cash must not exceed 1")
        entries = (
            Decimal(str(parameters["equipment_entry_weight"])),
            Decimal(str(parameters["chip_entry_weight"])),
            Decimal(str(parameters["satellite_entry_weight"])),
        )
        caps = (
            Decimal(str(parameters["equipment_weight"])),
            Decimal(str(parameters["chip_weight"])),
            Decimal(str(parameters["satellite_weight"])),
        )
        if any(entry > cap for entry, cap in zip(entries, caps, strict=True)):
            raise ValueError("entry weights must not exceed sleeve caps")
        first_remaining = Decimal(
            str(parameters["first_profit_remaining_fraction"])
        )
        second_remaining = Decimal(
            str(parameters["second_profit_remaining_fraction"])
        )
        if not Decimal(0) <= second_remaining < first_remaining <= Decimal(1):
            raise ValueError(
                "profit stages must satisfy 0 <= second < first <= 1"
            )

    def code_hash(self) -> str:
        source = inspect.getsource(type(self))
        return hashlib.sha256(source.encode()).hexdigest()

    def describe(self) -> str:
        return (
            "科技ETF三日回撤策略: 设备/芯片/创新药或机器人分组建仓; "
            "ATR初始/保本止损与五日时间止损; "
            "3:5:2分批止盈、回撤止盈、放量高潮和极值保护; "
            "信号在下一交易日开盘执行。"
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


def _bars_since_entry(
    *,
    history: tuple[DailyBar, ...],
    holding_bars: int,
) -> tuple[DailyBar, ...]:
    count = min(max(holding_bars, 1), len(history))
    return history[-count:]


def _entry_atr(
    *,
    history: tuple[DailyBar, ...],
    holding_bars: int,
    window: int,
) -> Decimal | None:
    """Reconstruct ATR known immediately before the T+1-open entry."""

    if holding_bars <= 0:
        return None
    entry_index = max(len(history) - holding_bars, 0)
    return _average_true_range(history[:entry_index], window)


def _average_true_range(
    history: tuple[DailyBar, ...],
    window: int,
) -> Decimal | None:
    if len(history) < window + 1:
        return None
    true_ranges: list[Decimal] = []
    start = len(history) - window
    for index in range(start, len(history)):
        bar = history[index]
        previous_close = history[index - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )
    return sum(true_ranges, Decimal(0)) / Decimal(window)


def _is_final_exit_signal(
    history: tuple[DailyBar, ...],
    parameters: Mapping[str, object],
) -> bool:
    volume_window = _require_int(parameters, "volume_window")
    bollinger_window = _require_int(parameters, "bollinger_window")
    if len(history) < max(3, volume_window + 1, bollinger_window + 1):
        return False

    latest = history[-1]
    comparison = history[-(volume_window + 1) : -1]
    average_volume = sum(
        (bar.volume for bar in comparison),
        Decimal(0),
    ) / Decimal(len(comparison))
    volume_surge = bool(
        average_volume > 0
        and latest.volume
        >= average_volume * Decimal(str(parameters["volume_surge_multiple"]))
    )
    body = abs(latest.close - latest.open)
    upper_shadow = latest.high - max(latest.open, latest.close)
    long_upper_shadow = upper_shadow > body * Decimal(
        str(parameters["upper_shadow_body_multiple"])
    )
    condition_a = volume_surge and long_upper_shadow

    first_prior = history[-3].close
    second_prior = history[-2].close
    threshold = Decimal(str(parameters["acceleration_daily_return"]))
    two_day_acceleration = bool(
        first_prior > 0
        and second_prior > 0
        and (second_prior - first_prior) / first_prior > threshold
        and (latest.close - second_prior) / second_prior > threshold
    )

    closes = [bar.close for bar in history[-(bollinger_window + 1) : -1]]
    mean = sum(closes, Decimal(0)) / Decimal(len(closes))
    variance = sum(
        ((close - mean) ** 2 for close in closes),
        Decimal(0),
    ) / Decimal(len(closes))
    upper_band = mean + variance.sqrt() * Decimal(
        str(parameters["bollinger_std_multiple"])
    )
    condition_b = two_day_acceleration and latest.high > upper_band
    return condition_a or condition_b


def _simple_moving_average(values: list[Decimal], window: int) -> Decimal:
    tail = values[-window:]
    return sum(tail, Decimal(0)) / Decimal(len(tail))
