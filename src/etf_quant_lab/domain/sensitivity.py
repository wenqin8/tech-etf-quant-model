"""Parameter sensitivity, neighborhood stability and cost stress (node 13).

Sensitivity runs every grid combination (bounded by ``max_trials``), keeps every
trial, and grades whether the best configuration sits on a stable plateau or a
lone spike by comparing it with its immediate grid neighbors.  Cost stress
re-evaluates one configuration under the ideal/normal/pessimistic cost models
and reports erosion plus a survival check.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise

from etf_quant_lab.contracts.enums import CostScenario
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.contracts.validation import (
    DateRange,
    SampleRole,
    TrialRecord,
    TrialStatus,
    ValidationVerdict,
)
from etf_quant_lab.domain.validation import expand_parameter_grid, hash_parameters

TRIAL_LIMIT_EXCEEDED = "VAL_TRIAL_LIMIT_EXCEEDED"

Evaluator = Callable[[Mapping[str, object], DateRange], Mapping[str, float]]
CostEvaluator = Callable[[CostModel], Mapping[str, float]]

_STABLE_THRESHOLD = 0.7
_FRAGILE_THRESHOLD = 0.4


@dataclass(frozen=True, slots=True)
class HeatmapCell:
    """One grid point's outcome for parameter-surface visualization."""

    parameters: Mapping[str, object]
    objective_value: float | None
    status: TrialStatus


@dataclass(frozen=True, slots=True)
class SensitivityReport:
    """Full parameter-surface outcome with a stability grade.

    ``neighborhood_stability_score`` is the best point's mean neighbor objective
    divided by its own objective (clamped at 1): near 1 means a plateau, near 0
    means a lone spike.
    """

    trials: tuple[TrialRecord, ...]
    heatmap: tuple[HeatmapCell, ...]
    best_parameters: Mapping[str, object] | None
    best_objective: float | None
    neighborhood_stability_score: float | None
    fragile_parameters: tuple[str, ...]
    verdict: ValidationVerdict
    warnings: tuple[str, ...] = ()

    @property
    def failed_trial_count(self) -> int:
        return sum(1 for trial in self.trials if trial.status == TrialStatus.FAILED)


@dataclass(frozen=True, slots=True)
class CostStressResult:
    """One cost scenario's metrics and erosion versus the ideal scenario."""

    scenario: CostScenario
    metrics: Mapping[str, float]
    erosion: Mapping[str, float] = field(default_factory=dict)
    survives: bool = True


@dataclass(frozen=True, slots=True)
class CostStressReport:
    """Cost stress outcome across scenarios with a survival verdict."""

    results: tuple[CostStressResult, ...]
    survival_metric: str
    verdict: ValidationVerdict
    warnings: tuple[str, ...] = ()


def run_sensitivity(
    evaluate: Evaluator,
    *,
    parameter_grid: Mapping[str, Sequence[object]],
    period: DateRange,
    objective_metric: str,
    max_trials: int = 2000,
) -> SensitivityReport:
    """Evaluate the full grid, keep all trials, and grade plateau stability."""

    combinations = expand_parameter_grid(parameter_grid)
    if len(combinations) > max_trials:
        raise DomainError(
            TRIAL_LIMIT_EXCEEDED,
            "参数组合数量超过单次运行上限",
            details={"combinations": len(combinations), "max_trials": max_trials},
        )

    trials: list[TrialRecord] = []
    heatmap: list[HeatmapCell] = []
    for trial_no, parameters in enumerate(combinations):
        record = _evaluate(evaluate, parameters, period, objective_metric, trial_no)
        trials.append(record)
        heatmap.append(
            HeatmapCell(
                parameters=dict(parameters),
                objective_value=record.objective_value,
                status=record.status,
            )
        )

    best = _best_trial(trials)
    if best is None:
        return SensitivityReport(
            trials=tuple(trials),
            heatmap=tuple(heatmap),
            best_parameters=None,
            best_objective=None,
            neighborhood_stability_score=None,
            fragile_parameters=(),
            verdict=ValidationVerdict.INCONCLUSIVE,
            warnings=("没有任何成功的参数评估",),
        )

    stability, fragile = _neighborhood_stability(
        best, trials, parameter_grid, objective_metric
    )
    verdict, warnings = _sensitivity_verdict(stability)
    return SensitivityReport(
        trials=tuple(trials),
        heatmap=tuple(heatmap),
        best_parameters=best.parameters,
        best_objective=best.objective_value,
        neighborhood_stability_score=stability,
        fragile_parameters=fragile,
        verdict=verdict,
        warnings=warnings,
    )


def run_cost_stress(
    evaluate: CostEvaluator,
    *,
    cost_models: Mapping[CostScenario, CostModel],
    survival_metric: str,
    survival_threshold: float = 0.0,
) -> CostStressReport:
    """Re-evaluate one configuration across cost scenarios and grade survival."""

    required = (CostScenario.IDEAL, CostScenario.NORMAL, CostScenario.PESSIMISTIC)
    missing = [scenario.value for scenario in required if scenario not in cost_models]
    if missing:
        raise ValueError(f"cost_models missing scenarios: {', '.join(missing)}")

    metrics_by_scenario: dict[CostScenario, dict[str, float]] = {}
    for scenario in required:
        metrics_by_scenario[scenario] = dict(evaluate(cost_models[scenario]))

    ideal_metrics = metrics_by_scenario[CostScenario.IDEAL]
    results: list[CostStressResult] = []
    warnings: list[str] = []
    for scenario in required:
        metrics = metrics_by_scenario[scenario]
        erosion = _erosion(ideal_metrics, metrics)
        survives = _survives(metrics, survival_metric, survival_threshold)
        if not survives:
            warnings.append(f"{scenario.value} 情景未达到生存门槛")
        results.append(
            CostStressResult(
                scenario=scenario,
                metrics=metrics,
                erosion=erosion,
                survives=survives,
            )
        )

    verdict = _cost_verdict(results)
    return CostStressReport(
        results=tuple(results),
        survival_metric=survival_metric,
        verdict=verdict,
        warnings=tuple(warnings),
    )


def split_by_market_phase(
    equity_dates: Sequence[DateRange],
) -> tuple[DateRange, ...]:
    """Placeholder segmentation hook kept minimal until phase labels exist.

    First release segments externally (bull/bear/range labels come from research
    configuration); the engine only validates that segments do not overlap.
    """

    ordered = sorted(equity_dates, key=lambda item: item.start)
    for previous, current in pairwise(ordered):
        if previous.overlaps(current):
            raise ValueError("market phase segments must not overlap")
    return tuple(ordered)


def _evaluate(
    evaluate: Evaluator,
    parameters: Mapping[str, object],
    period: DateRange,
    objective_metric: str,
    trial_no: int,
) -> TrialRecord:
    try:
        metrics = dict(evaluate(parameters, period))
    except Exception as exc:
        return TrialRecord(
            trial_no=trial_no,
            parameters=dict(parameters),
            params_hash=hash_parameters(parameters),
            sample_role=SampleRole.IN_SAMPLE,
            status=TrialStatus.FAILED,
            period=period,
            error_message=str(exc),
        )
    return TrialRecord(
        trial_no=trial_no,
        parameters=dict(parameters),
        params_hash=hash_parameters(parameters),
        sample_role=SampleRole.IN_SAMPLE,
        status=TrialStatus.SUCCEEDED,
        period=period,
        metrics=metrics,
        objective_value=metrics.get(objective_metric),
    )


def _best_trial(trials: Sequence[TrialRecord]) -> TrialRecord | None:
    best: TrialRecord | None = None
    for trial in trials:
        if trial.status != TrialStatus.SUCCEEDED or trial.objective_value is None:
            continue
        if best is None or (
            best.objective_value is not None
            and trial.objective_value > best.objective_value
        ):
            best = trial
    return best


def _neighborhood_stability(
    best: TrialRecord,
    trials: Sequence[TrialRecord],
    parameter_grid: Mapping[str, Sequence[object]],
    objective_metric: str,
) -> tuple[float | None, tuple[str, ...]]:
    """Score the best point against its axis-adjacent grid neighbors."""

    if best.objective_value is None or best.objective_value <= 0:
        return None, ()
    by_parameters = {
        _freeze(trial.parameters): trial
        for trial in trials
        if trial.status == TrialStatus.SUCCEEDED
    }

    fragile: list[str] = []
    neighbor_values: list[float] = []
    for name, values in parameter_grid.items():
        ordered_values = list(values)
        best_value = best.parameters[name]
        position = ordered_values.index(best_value)
        axis_values: list[float] = []
        for neighbor_position in (position - 1, position + 1):
            if not 0 <= neighbor_position < len(ordered_values):
                continue
            neighbor_parameters = dict(best.parameters)
            neighbor_parameters[name] = ordered_values[neighbor_position]
            neighbor = by_parameters.get(_freeze(neighbor_parameters))
            if neighbor is None or neighbor.objective_value is None:
                continue
            axis_values.append(neighbor.objective_value)
            neighbor_values.append(neighbor.objective_value)
        if axis_values:
            axis_ratio = (sum(axis_values) / len(axis_values)) / best.objective_value
            if axis_ratio < _FRAGILE_THRESHOLD:
                fragile.append(name)

    if not neighbor_values:
        return None, tuple(sorted(fragile))
    mean_neighbor = sum(neighbor_values) / len(neighbor_values)
    score = max(0.0, min(1.0, mean_neighbor / best.objective_value))
    return score, tuple(sorted(fragile))


def _sensitivity_verdict(
    stability: float | None,
) -> tuple[ValidationVerdict, tuple[str, ...]]:
    if stability is None:
        return ValidationVerdict.INCONCLUSIVE, ("邻域样本不足或最优目标非正",)
    if stability >= _STABLE_THRESHOLD:
        return ValidationVerdict.ACCEPTABLE, ()
    if stability >= _FRAGILE_THRESHOLD:
        return ValidationVerdict.WEAK, ("最优参数邻域退化明显",)
    return ValidationVerdict.REJECTED, ("最优参数疑似单点尖峰",)


def _erosion(
    ideal: Mapping[str, float],
    scenario_metrics: Mapping[str, float],
) -> dict[str, float]:
    erosion: dict[str, float] = {}
    for name, ideal_value in ideal.items():
        value = scenario_metrics.get(name)
        if value is None or ideal_value == 0:
            continue
        erosion[name] = (value - ideal_value) / abs(ideal_value)
    return erosion


def _survives(
    metrics: Mapping[str, float],
    survival_metric: str,
    survival_threshold: float,
) -> bool:
    value = metrics.get(survival_metric)
    return value is not None and value > survival_threshold


def _cost_verdict(results: Sequence[CostStressResult]) -> ValidationVerdict:
    by_scenario = {result.scenario: result for result in results}
    if not by_scenario[CostScenario.PESSIMISTIC].survives:
        return ValidationVerdict.REJECTED
    if not by_scenario[CostScenario.NORMAL].survives:
        return ValidationVerdict.WEAK
    return ValidationVerdict.ACCEPTABLE


def _freeze(parameters: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key, str(value)) for key, value in parameters.items()))
