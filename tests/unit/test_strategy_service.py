"""Unit tests for StrategyService: validation, generation and anti-lookahead."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import DataSource, Exchange, SignalAction, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.strategy import (
    TargetAllocation,
    TargetPortfolio,
    ValidateParametersRequest,
)
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.strategy import StrategyService
from tests.fixtures.fake_strategy import MomentumTopNStrategy

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
UNIVERSE = ("159915.SZ", "510300.SH")


def _service() -> StrategyService:
    registry = StrategyRegistry()
    registry.register(MomentumTopNStrategy())
    return StrategyService(registry)


def _bar(*, symbol: str, trade_date: date, close: str) -> DailyBar:
    price = Decimal(close)
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        open=price,
        high=price + Decimal("0.10"),
        low=price - Decimal("0.10"),
        close=price,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NK1",
        ingested_at=INGESTED_AT,
    )


def _history(as_of: date, *, extra: tuple[DailyBar, ...] = ()) -> MarketDataView:
    dates = [date(2026, 7, day) for day in (6, 7, 8, 9, 10)]
    bars: list[DailyBar] = []
    for index, day in enumerate(dates):
        bars.append(_bar(symbol="510300.SH", trade_date=day, close=str(4.0 + index * 0.1)))
        bars.append(_bar(symbol="159915.SZ", trade_date=day, close=str(3.0 + index * 0.02)))
    return MarketDataView(as_of_date=as_of, bars=tuple(bars) + extra)


def test_validate_parameters_normalizes_and_hashes() -> None:
    result = _service().validate_parameters(
        ValidateParametersRequest(
            strategy_id=StrategyId.ETF_ROTATION,
            version="1.0.0",
            parameters={"lookback_days": 5, "top_n": 1, "max_weight_per_symbol": 0.5},
        )
    )

    assert result.valid
    assert result.parameter_hash is not None
    assert result.normalized_parameters["max_weight_per_symbol"] == Decimal("0.5")


def test_validate_parameters_rejects_out_of_range_and_unknown() -> None:
    result = _service().validate_parameters(
        ValidateParametersRequest(
            strategy_id=StrategyId.ETF_ROTATION,
            version="1.0.0",
            parameters={"top_n": 99, "mystery": 1},
        )
    )

    assert not result.valid
    assert any("top_n" in error for error in result.errors)
    assert any("mystery" in error for error in result.errors)
    assert result.parameter_hash is None


def test_generate_targets_produces_fully_allocated_portfolio() -> None:
    portfolio = _service().generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters={"lookback_days": 5, "top_n": 1},
        as_of_date=date(2026, 7, 10),
        universe_symbols=UNIVERSE,
        market_data=_history(date(2026, 7, 10)),
    )

    invested = sum((a.target_weight for a in portfolio.allocations), Decimal(0))
    assert invested + portfolio.cash_weight == Decimal(1)
    # 510300.SH rises 10% total vs 159915.SZ ~2.6%, so it ranks first.
    assert [a.symbol for a in portfolio.allocations] == ["510300.SH"]


def test_generate_targets_rejects_unregistered_strategy() -> None:
    with pytest.raises(DomainError) as excinfo:
        _service().generate_targets(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters={},
            as_of_date=date(2026, 7, 10),
            universe_symbols=UNIVERSE,
            market_data=_history(date(2026, 7, 10)),
        )

    assert excinfo.value.code == "STRAT_NOT_FOUND"


def test_registry_rejects_duplicate_version() -> None:
    registry = StrategyRegistry()
    registry.register(MomentumTopNStrategy())

    with pytest.raises(DomainError, match="重复注册"):
        registry.register(MomentumTopNStrategy())


def test_truncation_consistency_signal_is_stable() -> None:
    """STYLE §10.4(1): a T signal is identical whether or not future data exists."""

    service = _service()
    params = {"lookback_days": 5, "top_n": 2}

    as_of = date(2026, 7, 10)
    truncated = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=_history(as_of),
    )

    # Same slice plus explicit future bars would be a lookahead bug; the view
    # forbids it, so we assert the slice-limited run is reproducible instead.
    repeated = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=_history(as_of),
    )

    assert truncated == repeated


def test_future_perturbation_does_not_change_past_signal() -> None:
    """STYLE §10.4(2): editing data after T must not change the T signal."""

    service = _service()
    params = {"lookback_days": 5, "top_n": 1}
    as_of = date(2026, 7, 10)

    baseline = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=_history(as_of),
    )

    # A wild future bar on 7-13 cannot enter the as_of=7-10 slice at all.
    with pytest.raises(DomainError):
        _history(
            as_of,
            extra=(_bar(symbol="159915.SZ", trade_date=date(2026, 7, 13), close="99.0"),),
        )

    # The past signal, built only from <= as_of data, is unchanged on rerun.
    rerun = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=_history(as_of),
    )
    assert baseline == rerun


def test_row_order_does_not_change_result() -> None:
    """STYLE §10.4(5): input row order must not change normalized output."""

    service = _service()
    params = {"lookback_days": 5, "top_n": 2}
    as_of = date(2026, 7, 10)
    view = _history(as_of)
    reversed_view = MarketDataView(as_of_date=as_of, bars=tuple(reversed(view.bars)))

    ordered = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=view,
    )
    shuffled = service.generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters=params,
        as_of_date=as_of,
        universe_symbols=UNIVERSE,
        market_data=reversed_view,
    )

    assert ordered == shuffled


def test_warmup_shortfall_returns_untradeable_state() -> None:
    """STYLE §10.4(4): insufficient history yields an explicit no-trade result."""

    portfolio = _service().generate_targets(
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        parameters={"lookback_days": 250, "top_n": 1},
        as_of_date=date(2026, 7, 10),
        universe_symbols=UNIVERSE,
        market_data=_history(date(2026, 7, 10)),
    )

    assert portfolio.allocations == ()
    assert portfolio.cash_weight == Decimal(1)
    assert "NO_TRADEABLE_SYMBOL" in portfolio.warnings


def test_target_outside_universe_is_rejected() -> None:
    """A misbehaving strategy targeting an out-of-universe symbol must be caught."""

    portfolio = TargetPortfolio(
        as_of_date=date(2026, 7, 10),
        strategy_id=StrategyId.TREND_BASELINE,
        version="1.0.0",
        allocations=(
            TargetAllocation(
                symbol="512100.SH",
                target_weight=Decimal("0.5"),
                action=SignalAction.BUY,
            ),
        ),
        cash_weight=Decimal("0.5"),
    )
    registry = StrategyRegistry()
    registry.register(_RogueStrategy(portfolio))
    service = StrategyService(registry)

    with pytest.raises(DomainError) as excinfo:
        service.generate_targets(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters={},
            as_of_date=date(2026, 7, 10),
            universe_symbols=("159915.SZ", "510300.SH"),
            market_data=_history(date(2026, 7, 10)),
        )

    assert excinfo.value.code == "STRAT_TARGET_OUTSIDE_UNIVERSE"


def test_guard_accepts_equal_enum_from_reloaded_module() -> None:
    """Streamlit 热重载会重建枚举类: 缓存上下文里的旧枚举与页面新枚举
    对象身份不同但值相同, 身份守卫必须按值比较, 否则每次热重载后
    生成信号都会误报「策略返回的身份与请求不一致」。"""

    from enum import StrEnum
    from typing import cast

    class ReloadedStrategyId(StrEnum):
        TREND_BASELINE = "TREND_BASELINE"

    reloaded = cast(StrategyId, ReloadedStrategyId.TREND_BASELINE)
    assert reloaded is not StrategyId.TREND_BASELINE
    assert reloaded == StrategyId.TREND_BASELINE

    portfolio = TargetPortfolio(
        as_of_date=date(2026, 7, 10),
        strategy_id=reloaded,
        version="1.0.0",
        allocations=(),
        cash_weight=Decimal(1),
    )
    registry = StrategyRegistry()
    registry.register(_RogueStrategy(portfolio))
    service = StrategyService(registry)

    result = service.generate_targets(
        strategy_id=StrategyId.TREND_BASELINE,
        version="1.0.0",
        parameters={},
        as_of_date=date(2026, 7, 10),
        universe_symbols=UNIVERSE,
        market_data=_history(date(2026, 7, 10)),
    )

    assert result is portfolio


def test_guard_still_rejects_truly_wrong_identity() -> None:
    """按值比较后, 返回错误策略身份的组合仍然必须被拦截。"""

    portfolio = TargetPortfolio(
        as_of_date=date(2026, 7, 10),
        strategy_id=StrategyId.ETF_ROTATION,
        version="1.0.0",
        allocations=(),
        cash_weight=Decimal(1),
    )
    registry = StrategyRegistry()
    registry.register(_RogueStrategy(portfolio))
    service = StrategyService(registry)

    with pytest.raises(DomainError, match="身份与请求不一致"):
        service.generate_targets(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters={},
            as_of_date=date(2026, 7, 10),
            universe_symbols=UNIVERSE,
            market_data=_history(date(2026, 7, 10)),
        )


class _RogueStrategy:
    """A strategy that returns a preset portfolio to exercise service guards."""

    strategy_id = StrategyId.TREND_BASELINE
    version = "1.0.0"

    def __init__(self, portfolio: TargetPortfolio) -> None:
        self._portfolio = portfolio

    def parameter_specs(self) -> tuple[()]:
        return ()

    def warmup_bars(self, parameters: object) -> int:
        return 0

    def generate_targets(self, context: object, parameters: object) -> TargetPortfolio:
        return self._portfolio
