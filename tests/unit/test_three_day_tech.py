"""Unit tests for the regime-aware focused technology ETF strategy."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import DataSource, Exchange, SignalAction
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategies.three_day_tech import (
    REASON_EXIT_ATR_STOP,
    REASON_EXIT_BREAKEVEN_STOP,
    REASON_EXIT_CLIMAX,
    REASON_EXIT_EXTREME_PROTECTION,
    REASON_EXIT_SWING_WINDOW,
    REASON_EXIT_TIME_STOP,
    REASON_HOLD_UPTREND,
    REASON_REDUCE_FIRST_PROFIT,
    REASON_REDUCE_TRAILING_PROFIT,
    ThreeDayTechStrategy,
)
from etf_quant_lab.domain.strategy import StrategyContext

END = date(2026, 7, 24)
INGESTED_AT = datetime(2026, 7, 25, 1, 0, tzinfo=UTC)


def _params(**overrides: object) -> dict[str, object]:
    strategy = ThreeDayTechStrategy()
    values = {spec.name: spec.default for spec in strategy.parameter_specs()}
    values.update(overrides)
    return values


def _bars(
    symbol: str,
    closes: list[Decimal],
    *,
    opens: list[Decimal] | None = None,
    highs: list[Decimal] | None = None,
    volumes: list[Decimal] | None = None,
) -> tuple[DailyBar, ...]:
    start = END - timedelta(days=len(closes) - 1)
    resolved_opens = opens or closes
    resolved_highs = highs or [
        max(open_price, close) + Decimal("0.1")
        for open_price, close in zip(resolved_opens, closes, strict=True)
    ]
    resolved_volumes = volumes or [Decimal("100000")] * len(closes)
    return tuple(
        DailyBar(
            symbol=symbol,
            trade_date=start + timedelta(days=index),
            exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
            open=resolved_opens[index],
            high=resolved_highs[index],
            low=max(
                min(resolved_opens[index], close) - Decimal("0.1"),
                Decimal("0.01"),
            ),
            close=close,
            volume=resolved_volumes[index],
            amount=Decimal("1000000"),
            source=DataSource.TUSHARE,
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NT1",
            ingested_at=INGESTED_AT,
        )
        for index, close in enumerate(closes)
    )


def _uptrend_pullback(*, severity: str = "0.92") -> list[Decimal]:
    rising = [Decimal("10") + Decimal(index) * Decimal("0.10") for index in range(61)]
    peak = rising[-1]
    deep = Decimal(severity)
    return [
        *rising,
        peak,
        peak * Decimal("0.97"),
        peak * deep,
        peak * (deep + Decimal("0.02")),
    ]


def _downtrend() -> list[Decimal]:
    return [Decimal("20") - Decimal(index) * Decimal("0.12") for index in range(65)]


def _held_history(post_entry: list[str]) -> list[Decimal]:
    return [Decimal("10")] * 61 + [Decimal(value) for value in post_entry]


def _context(
    bars: tuple[DailyBar, ...],
    symbols: tuple[str, ...],
    *,
    current_weights: dict[str, Decimal] | None = None,
    holding_bars: dict[str, int] | None = None,
    entry_prices: dict[str, Decimal] | None = None,
) -> StrategyContext:
    return StrategyContext(
        as_of_date=END,
        universe_symbols=symbols,
        market_data=MarketDataView(as_of_date=END, bars=bars),
        current_weights=current_weights or {},
        position_holding_bars=holding_bars or {},
        position_entry_prices=entry_prices or {},
    )


def _held_context(
    closes: list[Decimal],
    *,
    current_weight: str = "0.40",
    holding_bars: int,
    entry_price: str = "10",
    opens: list[Decimal] | None = None,
    highs: list[Decimal] | None = None,
    volumes: list[Decimal] | None = None,
) -> StrategyContext:
    symbol = "159516.SZ"
    return _context(
        _bars(symbol, closes, opens=opens, highs=highs, volumes=volumes),
        (symbol,),
        current_weights={symbol: Decimal(current_weight)},
        holding_bars={symbol: holding_bars},
        entry_prices={symbol: Decimal(entry_price)},
    )


def test_builds_fixed_sleeves_and_selects_only_one_symbol_per_group() -> None:
    symbols = (
        "159516.SZ",
        "588170.SH",
        "159995.SZ",
        "159992.SZ",
        "562500.SH",
    )
    bars = (
        _bars("159516.SZ", _uptrend_pullback(severity="0.95"))
        + _bars("588170.SH", _uptrend_pullback(severity="0.92"))
        + _bars("159995.SZ", _uptrend_pullback())
        + _bars("159992.SZ", _uptrend_pullback(severity="0.95"))
        + _bars("562500.SH", _uptrend_pullback(severity="0.92"))
    )

    portfolio = ThreeDayTechStrategy().generate_targets(
        _context(bars, symbols),
        _params(),
    )

    weights = {item.symbol: item.target_weight for item in portfolio.allocations}
    assert weights == {
        "159995.SZ": Decimal("0.10"),
        "562500.SH": Decimal("0.05"),
        "588170.SH": Decimal("0.20"),
    }
    assert portfolio.cash_weight == Decimal("0.65")
    assert all(item.action == SignalAction.BUY for item in portfolio.allocations)


def test_uptrend_position_ignores_swing_age_and_keeps_holding() -> None:
    symbol = "159516.SZ"
    closes = _uptrend_pullback(severity="0.99")
    latest = closes[-1]

    portfolio = ThreeDayTechStrategy().generate_targets(
        _context(
            _bars(symbol, closes),
            (symbol,),
            current_weights={symbol: Decimal("0.40")},
            holding_bars={symbol: 25},
            entry_prices={symbol: latest / Decimal("1.03")},
        ),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.HOLD
    assert allocation.target_weight == Decimal("0.40")
    assert REASON_HOLD_UPTREND in allocation.reason_codes


def test_downtrend_position_exits_after_five_day_swing_window() -> None:
    symbol = "159516.SZ"
    closes = [
        Decimal("20") - Decimal(index) * Decimal("0.05")
        for index in range(60)
    ]
    closes.extend(
        (
            Decimal("17.00"),
            Decimal("17.40"),
            Decimal("17.35"),
            Decimal("17.30"),
            Decimal("17.20"),
        )
    )

    portfolio = ThreeDayTechStrategy().generate_targets(
        _context(
            _bars(symbol, closes),
            (symbol,),
            current_weights={symbol: Decimal("0.40")},
            holding_bars={symbol: 5},
            entry_prices={symbol: closes[-5]},
        ),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == 0
    assert REASON_EXIT_SWING_WINDOW in allocation.reason_codes


def test_initial_stop_uses_entry_time_atr_and_exits_unconditionally() -> None:
    closes = _held_history(["9.4"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, holding_bars=1),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == 0
    assert REASON_EXIT_ATR_STOP in allocation.reason_codes


def test_stop_moves_to_entry_after_prior_high_reaches_one_and_half_atr() -> None:
    closes = _held_history(["10.4", "10.1", "9.95"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, holding_bars=3),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert REASON_EXIT_BREAKEVEN_STOP in allocation.reason_codes


def test_five_day_no_breakout_triggers_time_stop() -> None:
    closes = _held_history(["10", "10.05", "10.10", "10.15", "10.10"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, holding_bars=5),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert REASON_EXIT_TIME_STOP in allocation.reason_codes


def test_first_profit_stage_reduces_original_sleeve_to_seventy_percent() -> None:
    closes = _held_history(["10.2", "10.7"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, holding_bars=2),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == Decimal("0.280")
    assert REASON_REDUCE_FIRST_PROFIT in allocation.reason_codes


def test_nine_percent_drawdown_after_first_stage_leaves_twenty_percent() -> None:
    closes = _held_history(["10.7", "12", "11.2", "10.9"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, current_weight="0.28", holding_bars=4),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == Decimal("0.080")
    assert REASON_REDUCE_TRAILING_PROFIT in allocation.reason_codes


def test_two_five_percent_days_and_bollinger_break_exit_final_twenty_percent() -> None:
    closes = _held_history(["10.5", "11.1", "11.7"])
    volumes = [Decimal("100000")] * len(closes)

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(
            closes,
            current_weight="0.08",
            holding_bars=3,
            volumes=volumes,
        ),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == 0
    assert REASON_EXIT_CLIMAX in allocation.reason_codes


def test_volume_surge_and_long_upper_shadow_exit_final_twenty_percent() -> None:
    closes = _held_history(["10.5", "10.8"])
    opens = closes.copy()
    opens[-1] = Decimal("10.7")
    highs = [
        max(open_price, close) + Decimal("0.1")
        for open_price, close in zip(opens, closes, strict=True)
    ]
    highs[-1] = Decimal("11.2")
    volumes = [Decimal("100000")] * len(closes)
    volumes[-1] = Decimal("300000")

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(
            closes,
            current_weight="0.08",
            holding_bars=2,
            opens=opens,
            highs=highs,
            volumes=volumes,
        ),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == 0
    assert REASON_EXIT_CLIMAX in allocation.reason_codes


def test_extreme_profit_and_seven_percent_daily_drop_exit_everything() -> None:
    closes = _held_history(["16.2", "15.0"])

    portfolio = ThreeDayTechStrategy().generate_targets(
        _held_context(closes, holding_bars=2),
        _params(),
    )

    allocation = portfolio.allocations[0]
    assert allocation.action == SignalAction.SELL
    assert allocation.target_weight == 0
    assert REASON_EXIT_EXTREME_PROTECTION in allocation.reason_codes


def test_no_pullback_means_no_new_position_and_full_cash() -> None:
    symbol = "159516.SZ"
    rising = [Decimal("10") + Decimal(index) * Decimal("0.1") for index in range(65)]

    portfolio = ThreeDayTechStrategy().generate_targets(
        _context(_bars(symbol, rising), (symbol,)),
        _params(),
    )

    assert portfolio.allocations == ()
    assert portfolio.cash_weight == Decimal(1)


def test_deep_pullback_waits_for_reversal_instead_of_buying_while_falling() -> None:
    symbol = "159516.SZ"
    rising = [Decimal("10") + Decimal(index) * Decimal("0.1") for index in range(61)]
    peak = rising[-1]
    closes = [
        *rising,
        peak,
        peak * Decimal("0.97"),
        peak * Decimal("0.92"),
        peak * Decimal("0.90"),
    ]

    portfolio = ThreeDayTechStrategy().generate_targets(
        _context(_bars(symbol, closes), (symbol,)),
        _params(),
    )

    assert portfolio.allocations == ()
    assert portfolio.cash_weight == Decimal(1)


def test_weight_window_and_profit_stage_relationships_are_guarded() -> None:
    symbol = "159516.SZ"
    context = _context(_bars(symbol, _uptrend_pullback()), (symbol,))
    strategy = ThreeDayTechStrategy()

    with pytest.raises(ValueError, match="smaller"):
        strategy.generate_targets(
            context,
            _params(trend_fast_window=60, trend_slow_window=20),
        )
    with pytest.raises(ValueError, match="minimum cash"):
        strategy.generate_targets(
            context,
            _params(equipment_weight=Decimal("0.60")),
        )
    with pytest.raises(ValueError, match="profit stages"):
        strategy.generate_targets(
            context,
            _params(
                first_profit_remaining_fraction=Decimal("0.20"),
                second_profit_remaining_fraction=Decimal("0.40"),
            ),
        )
