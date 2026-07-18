"""Unit tests for strategy parameter coercion and output guards."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.enums import SignalAction, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    TargetAllocation,
    TargetPortfolio,
    ValidateParametersRequest,
)
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategy import StrategyContext
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.strategy import StrategyService


class _TypedStrategy:
    """Exercises every parameter type and echoes a preset portfolio."""

    strategy_id = StrategyId.TREND_BASELINE
    version = "1.0.0"

    def __init__(self, portfolio: TargetPortfolio | None = None) -> None:
        self._portfolio = portfolio

    def parameter_specs(self) -> tuple[ParameterSpec, ...]:
        return (
            ParameterSpec(name="enabled", type="bool", default=True),
            ParameterSpec(
                name="rebalance",
                type="enum",
                default="WEEKLY",
                choices=("DAILY", "WEEKLY", "MONTHLY"),
            ),
            ParameterSpec(
                name="max_weight",
                type="float",
                default=Decimal("0.35"),
                minimum=Decimal("0.01"),
                maximum=Decimal("1.0"),
            ),
            ParameterSpec(
                name="top_n",
                type="int",
                default=3,
                minimum=Decimal(1),
                maximum=Decimal(10),
            ),
        )

    def warmup_bars(self, parameters: Mapping[str, object]) -> int:
        return 0

    def generate_targets(
        self,
        context: StrategyContext,
        parameters: Mapping[str, object],
    ) -> TargetPortfolio:
        assert self._portfolio is not None
        return self._portfolio


def _service(portfolio: TargetPortfolio | None = None) -> StrategyService:
    registry = StrategyRegistry()
    registry.register(_TypedStrategy(portfolio))
    return StrategyService(registry)


def _validate(parameters: Mapping[str, object]) -> object:
    return _service().validate_parameters(
        ValidateParametersRequest(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters=parameters,
        )
    )


def test_defaults_apply_when_parameters_are_omitted() -> None:
    result = _validate({})

    assert result.valid
    assert result.normalized_parameters == {
        "enabled": True,
        "rebalance": "WEEKLY",
        "max_weight": Decimal("0.35"),
        "top_n": 3,
    }


def test_bool_parameter_rejects_non_bool() -> None:
    result = _validate({"enabled": 1})

    assert not result.valid
    assert any("enabled" in error for error in result.errors)


def test_enum_parameter_rejects_unlisted_choice() -> None:
    result = _validate({"rebalance": "HOURLY"})

    assert not result.valid
    assert any("rebalance" in error for error in result.errors)


def test_float_parameter_accepts_string_and_enforces_range() -> None:
    ok = _validate({"max_weight": "0.5"})
    assert ok.valid
    assert ok.normalized_parameters["max_weight"] == Decimal("0.5")

    too_big = _validate({"max_weight": "1.5"})
    assert not too_big.valid


def test_int_parameter_rejects_bool_and_float() -> None:
    assert not _validate({"top_n": True}).valid
    assert not _validate({"top_n": 2.5}).valid


def test_generate_targets_rejects_identity_mismatch() -> None:
    wrong_identity = TargetPortfolio(
        as_of_date=date(2026, 7, 10),
        strategy_id=StrategyId.ETF_ROTATION,  # not the registered strategy
        version="1.0.0",
        allocations=(),
        cash_weight=Decimal(1),
    )
    service = _service(wrong_identity)

    with pytest.raises(DomainError, match="身份"):
        service.generate_targets(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters={},
            as_of_date=date(2026, 7, 10),
            universe_symbols=("510300.SH",),
            market_data=MarketDataView(as_of_date=date(2026, 7, 10), bars=()),
        )


def test_generate_targets_rejects_date_mismatch() -> None:
    wrong_date = TargetPortfolio(
        as_of_date=date(2026, 7, 9),  # not the requested as_of_date
        strategy_id=StrategyId.TREND_BASELINE,
        version="1.0.0",
        allocations=(
            TargetAllocation(
                symbol="510300.SH",
                target_weight=Decimal("0.5"),
                action=SignalAction.BUY,
            ),
        ),
        cash_weight=Decimal("0.5"),
    )
    service = _service(wrong_date)

    with pytest.raises(DomainError, match="as_of_date"):
        service.generate_targets(
            strategy_id=StrategyId.TREND_BASELINE,
            version="1.0.0",
            parameters={},
            as_of_date=date(2026, 7, 10),
            universe_symbols=("510300.SH",),
            market_data=MarketDataView(as_of_date=date(2026, 7, 10), bars=()),
        )


def test_list_strategies_returns_descriptor() -> None:
    descriptors = _service().list_strategies()

    assert len(descriptors) == 1
    assert descriptors[0].strategy_id is StrategyId.TREND_BASELINE
    assert len(descriptors[0].parameter_specs) == 4
