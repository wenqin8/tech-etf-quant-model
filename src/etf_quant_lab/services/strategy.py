"""Application service for strategy registration, validation and target generation."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal, InvalidOperation

from etf_quant_lab.contracts.enums import StrategyId
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    ParameterValidationResult,
    StrategyDescriptor,
    TargetPortfolio,
    ValidateParametersRequest,
    parameter_hash,
)
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategy import Strategy, StrategyContext
from etf_quant_lab.domain.strategy_registry import StrategyRegistry


class StrategyService:
    """Resolve strategies, validate parameters and produce guarded targets.

    The service never lets a strategy see future data or emit an unsafe target:
    it builds the as-of-bounded slice, calls the pure strategy, then re-checks the
    returned portfolio against the universe and weight rules from API §8.
    """

    def __init__(self, registry: StrategyRegistry) -> None:
        self._registry = registry

    def list_strategies(self) -> tuple[StrategyDescriptor, ...]:
        """Return descriptors for every registered strategy version."""

        return tuple(_describe(strategy) for strategy in self._registry.list_all())

    def validate_parameters(
        self,
        request: ValidateParametersRequest,
    ) -> ParameterValidationResult:
        """Validate and normalize raw parameters against a strategy's schema."""

        strategy = self._registry.get(request.strategy_id, request.version)
        specs = {spec.name: spec for spec in strategy.parameter_specs()}
        normalized: dict[str, object] = {}
        errors: list[str] = []

        unknown = sorted(set(request.parameters) - set(specs))
        errors.extend(f"未知参数: {name}" for name in unknown)

        for name, spec in specs.items():
            raw = request.parameters.get(name, spec.default)
            try:
                normalized[name] = _coerce_parameter(spec, raw)
            except _ParameterError as exc:
                errors.append(str(exc))

        if errors:
            return ParameterValidationResult(
                valid=False,
                normalized_parameters={},
                parameter_hash=None,
                errors=tuple(errors),
            )
        return ParameterValidationResult(
            valid=True,
            normalized_parameters=normalized,
            parameter_hash=parameter_hash(normalized),
        )

    def generate_targets(
        self,
        *,
        strategy_id: StrategyId,
        version: str,
        parameters: Mapping[str, object],
        as_of_date: date,
        universe_symbols: tuple[str, ...],
        market_data: MarketDataView,
        current_weights: Mapping[str, Decimal] | None = None,
        cash_weight: Decimal = Decimal(1),
    ) -> TargetPortfolio:
        """Run a strategy for one date and enforce the target-portfolio contract."""

        strategy = self._registry.get(strategy_id, version)
        validation = self.validate_parameters(
            ValidateParametersRequest(
                strategy_id=strategy_id,
                version=version,
                parameters=parameters,
            )
        )
        if not validation.valid:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "策略参数校验失败",
                details={"errors": validation.errors},
            )
        context = StrategyContext(
            as_of_date=as_of_date,
            universe_symbols=universe_symbols,
            market_data=market_data,
            current_weights=dict(current_weights or {}),
            cash_weight=cash_weight,
        )
        portfolio = strategy.generate_targets(context, validation.normalized_parameters)
        self._guard_portfolio(portfolio, strategy_id, version, as_of_date, universe_symbols)
        return portfolio

    @staticmethod
    def _guard_portfolio(
        portfolio: TargetPortfolio,
        strategy_id: StrategyId,
        version: str,
        as_of_date: date,
        universe_symbols: tuple[str, ...],
    ) -> None:
        # 按值比较而非 `is`: Streamlit 热重载会重建 StrategyId 枚举类,
        # 缓存上下文里的旧枚举成员与页面传入的新成员身份不同但值相同。
        if portfolio.strategy_id != strategy_id or portfolio.version != version:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "策略返回的身份与请求不一致",
                details={"expected": f"{strategy_id.value}:{version}"},
            )
        if portfolio.as_of_date != as_of_date:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "策略目标日期与请求 as_of_date 不一致",
                details={"as_of_date": as_of_date.isoformat()},
            )
        allowed = set(universe_symbols)
        outside = sorted(
            allocation.symbol
            for allocation in portfolio.allocations
            if allocation.symbol not in allowed
        )
        if outside:
            raise DomainError(
                "STRAT_TARGET_OUTSIDE_UNIVERSE",
                "策略目标包含不在可选池中的标的",
                details={"symbols": tuple(outside)},
            )


class _ParameterError(ValueError):
    """Internal signal that one parameter failed validation."""


def _describe(strategy: Strategy) -> StrategyDescriptor:
    specs = strategy.parameter_specs()
    return StrategyDescriptor(
        strategy_id=strategy.strategy_id,
        version=strategy.version,
        display_name=strategy.strategy_id.value,
        minimum_history_bars=strategy.warmup_bars(_default_parameters(specs)),
        supports_multi_asset=True,
        parameter_specs=specs,
    )


def _default_parameters(specs: tuple[ParameterSpec, ...]) -> dict[str, object]:
    return {spec.name: spec.default for spec in specs}


def _coerce_parameter(spec: ParameterSpec, raw: object) -> object:
    if spec.type == "bool":
        return _coerce_bool(spec, raw)
    if spec.type == "int":
        return _coerce_int(spec, raw)
    if spec.type == "float":
        return _coerce_float(spec, raw)
    return _coerce_enum(spec, raw)


def _coerce_bool(spec: ParameterSpec, raw: object) -> bool:
    if isinstance(raw, bool):
        return raw
    raise _ParameterError(f"参数 {spec.name} 必须是布尔值")


def _coerce_int(spec: ParameterSpec, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _ParameterError(f"参数 {spec.name} 必须是整数")
    _check_range(spec, Decimal(raw))
    return raw


def _coerce_float(spec: ParameterSpec, raw: object) -> Decimal:
    if isinstance(raw, bool):
        raise _ParameterError(f"参数 {spec.name} 必须是数值")
    try:
        value = Decimal(str(raw)) if not isinstance(raw, Decimal) else raw
    except (InvalidOperation, ValueError) as exc:
        raise _ParameterError(f"参数 {spec.name} 不是有效数值") from exc
    _check_range(spec, value)
    return value


def _coerce_enum(spec: ParameterSpec, raw: object) -> str:
    if not isinstance(raw, str) or raw not in spec.choices:
        raise _ParameterError(
            f"参数 {spec.name} 必须是 {', '.join(spec.choices)} 之一"
        )
    return raw


def _check_range(spec: ParameterSpec, value: Decimal) -> None:
    if spec.minimum is not None and value < spec.minimum:
        raise _ParameterError(f"参数 {spec.name} 不得小于 {spec.minimum}")
    if spec.maximum is not None and value > spec.maximum:
        raise _ParameterError(f"参数 {spec.name} 不得大于 {spec.maximum}")
