"""Holdout and walk-forward validation engines (node 12).

The engines never look at market data directly: an injected ``evaluate``
callable runs one backtest for ``(parameters, period)`` and returns metrics.
Anti-leakage is structural — parameter selection only ever sees train-period
evaluations, each walk-forward window freezes its parameters before touching its
test slice, and the holdout test period is evaluated exactly once.  Every
attempted trial is recorded, failed ones included (VAL-002).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import date
from hashlib import sha256
from itertools import product

from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.validation import (
    DateRange,
    HoldoutSplit,
    SampleRole,
    TrialRecord,
    TrialStatus,
    ValidationReport,
    ValidationVerdict,
    WalkForwardWindow,
    WindowResult,
)

Evaluator = Callable[[Mapping[str, object], DateRange], Mapping[str, float]]

VALIDATION_WINDOWS_INVALID = "VAL_WINDOWS_INVALID"

_DEGRADATION_WEAK = -0.40
_DEGRADATION_STRONG = -0.20


def expand_parameter_grid(
    parameter_grid: Mapping[str, Sequence[object]],
) -> tuple[dict[str, object], ...]:
    """Expand a name->values grid into all combinations, deterministically ordered."""

    if not parameter_grid:
        return ({},)
    names = sorted(parameter_grid)
    value_lists = [list(parameter_grid[name]) for name in names]
    if any(not values for values in value_lists):
        raise ValueError("every grid parameter needs at least one value")
    return tuple(
        dict(zip(names, combination, strict=True))
        for combination in product(*value_lists)
    )


def generate_walk_forward_windows(
    trading_dates: Sequence[date],
    *,
    train_bars: int,
    test_bars: int,
    step_bars: int,
    anchored: bool = False,
) -> tuple[WalkForwardWindow, ...]:
    """Slice an ordered trading-date sequence into non-peeking train/test windows."""

    if train_bars <= 0 or test_bars <= 0 or step_bars <= 0:
        raise ValueError("train_bars, test_bars and step_bars must be positive")
    dates = list(trading_dates)
    if sorted(dates) != dates or len(set(dates)) != len(dates):
        raise ValueError("trading_dates must be strictly increasing")
    if len(dates) < train_bars + test_bars:
        raise DomainError(
            VALIDATION_WINDOWS_INVALID,
            "历史长度不足以构成一个训练+测试窗口",
            details={
                "available_bars": len(dates),
                "required_bars": train_bars + test_bars,
            },
        )

    windows: list[WalkForwardWindow] = []
    index = 0
    train_start_position = 0
    test_end_position = train_bars + test_bars
    while test_end_position <= len(dates):
        train_end_position = test_end_position - test_bars
        start_position = 0 if anchored else train_start_position
        windows.append(
            WalkForwardWindow(
                index=index,
                train=DateRange(
                    start=dates[start_position],
                    end=dates[train_end_position - 1],
                ),
                test=DateRange(
                    start=dates[train_end_position],
                    end=dates[test_end_position - 1],
                ),
            )
        )
        index += 1
        train_start_position += step_bars
        test_end_position += step_bars
    return tuple(windows)


def run_holdout(
    evaluate: Evaluator,
    *,
    parameter_grid: Mapping[str, Sequence[object]],
    split: HoldoutSplit,
    selection_metric: str,
) -> ValidationReport:
    """Grid-search the train period, then score the frozen winner on test once."""

    combinations = expand_parameter_grid(parameter_grid)
    trials: list[TrialRecord] = []
    best_parameters: dict[str, object] | None = None
    best_objective: float | None = None
    best_train_metrics: Mapping[str, float] = {}

    for trial_no, parameters in enumerate(combinations):
        record = _evaluate_trial(
            evaluate,
            parameters,
            split.train,
            selection_metric,
            trial_no=trial_no,
            sample_role=SampleRole.IN_SAMPLE,
        )
        trials.append(record)
        if (
            record.status == TrialStatus.SUCCEEDED
            and record.objective_value is not None
            and (best_objective is None or record.objective_value > best_objective)
        ):
            best_objective = record.objective_value
            best_parameters = dict(parameters)
            best_train_metrics = record.metrics

    if best_parameters is None:
        return ValidationReport(
            validation_type="HOLDOUT",
            selection_metric=selection_metric,
            trials=tuple(trials),
            verdict=ValidationVerdict.INCONCLUSIVE,
            warnings=("训练期没有任何成功的参数评估",),
        )

    # The frozen winner touches the test period exactly once.
    test_record = _evaluate_trial(
        evaluate,
        best_parameters,
        split.test,
        selection_metric,
        trial_no=len(trials),
        sample_role=SampleRole.OUT_OF_SAMPLE,
    )
    trials.append(test_record)
    if test_record.status == TrialStatus.FAILED:
        return ValidationReport(
            validation_type="HOLDOUT",
            selection_metric=selection_metric,
            trials=tuple(trials),
            verdict=ValidationVerdict.INCONCLUSIVE,
            selected_parameters=best_parameters,
            train_metrics=best_train_metrics,
            warnings=("样本外评估失败",),
        )

    degradation = _degradation(best_train_metrics, test_record.metrics)
    verdict, warnings = _holdout_verdict(
        selection_metric, test_record.metrics, degradation
    )
    return ValidationReport(
        validation_type="HOLDOUT",
        selection_metric=selection_metric,
        trials=tuple(trials),
        verdict=verdict,
        selected_parameters=best_parameters,
        train_metrics=best_train_metrics,
        test_metrics=test_record.metrics,
        degradation=degradation,
        warnings=warnings,
    )


def run_walk_forward(
    evaluate: Evaluator,
    *,
    parameter_grid: Mapping[str, Sequence[object]],
    windows: Sequence[WalkForwardWindow],
    selection_metric: str,
) -> ValidationReport:
    """Select parameters per window from its own train slice, then test forward."""

    if not windows:
        raise ValueError("windows must not be empty")
    combinations = expand_parameter_grid(parameter_grid)
    trials: list[TrialRecord] = []
    window_results: list[WindowResult] = []
    warnings: list[str] = []
    trial_no = 0

    for window in windows:
        best_parameters: dict[str, object] | None = None
        best_objective: float | None = None
        for parameters in combinations:
            record = _evaluate_trial(
                evaluate,
                parameters,
                window.train,
                selection_metric,
                trial_no=trial_no,
                sample_role=SampleRole.WALK_FORWARD,
                window_index=window.index,
            )
            trials.append(record)
            trial_no += 1
            if (
                record.status == TrialStatus.SUCCEEDED
                and record.objective_value is not None
                and (best_objective is None or record.objective_value > best_objective)
            ):
                best_objective = record.objective_value
                best_parameters = dict(parameters)

        if best_parameters is None or best_objective is None:
            warnings.append(f"窗口 {window.index} 训练评估全部失败")
            continue

        test_record = _evaluate_trial(
            evaluate,
            best_parameters,
            window.test,
            selection_metric,
            trial_no=trial_no,
            sample_role=SampleRole.OUT_OF_SAMPLE,
            window_index=window.index,
        )
        trials.append(test_record)
        trial_no += 1
        if test_record.status == TrialStatus.FAILED:
            warnings.append(f"窗口 {window.index} 样本外评估失败")
            continue
        window_results.append(
            WindowResult(
                window=window,
                selected_parameters=best_parameters,
                selected_params_hash=hash_parameters(best_parameters),
                train_objective=best_objective,
                test_metrics=test_record.metrics,
            )
        )

    verdict = _walk_forward_verdict(
        window_results, len(windows), selection_metric, warnings
    )
    return ValidationReport(
        validation_type="WALK_FORWARD",
        selection_metric=selection_metric,
        trials=tuple(trials),
        verdict=verdict,
        window_results=tuple(window_results),
        warnings=tuple(warnings),
    )


def _evaluate_trial(
    evaluate: Evaluator,
    parameters: Mapping[str, object],
    period: DateRange,
    selection_metric: str,
    *,
    trial_no: int,
    sample_role: SampleRole,
    window_index: int | None = None,
) -> TrialRecord:
    try:
        metrics = dict(evaluate(parameters, period))
    except Exception as exc:
        return TrialRecord(
            trial_no=trial_no,
            parameters=dict(parameters),
            params_hash=hash_parameters(parameters),
            sample_role=sample_role,
            status=TrialStatus.FAILED,
            period=period,
            window_index=window_index,
            error_message=str(exc),
        )
    return TrialRecord(
        trial_no=trial_no,
        parameters=dict(parameters),
        params_hash=hash_parameters(parameters),
        sample_role=sample_role,
        status=TrialStatus.SUCCEEDED,
        period=period,
        metrics=metrics,
        objective_value=metrics.get(selection_metric),
        window_index=window_index,
    )


def _degradation(
    train_metrics: Mapping[str, float],
    test_metrics: Mapping[str, float],
) -> dict[str, float]:
    degradation: dict[str, float] = {}
    for name, train_value in train_metrics.items():
        test_value = test_metrics.get(name)
        if test_value is None or train_value == 0:
            continue
        degradation[name] = (test_value - train_value) / abs(train_value)
    return degradation


def _holdout_verdict(
    selection_metric: str,
    test_metrics: Mapping[str, float],
    degradation: Mapping[str, float],
) -> tuple[ValidationVerdict, tuple[str, ...]]:
    warnings: list[str] = []
    test_value = test_metrics.get(selection_metric)
    if test_value is None:
        return ValidationVerdict.INCONCLUSIVE, ("样本外缺少选择指标",)
    if test_value <= 0:
        warnings.append("样本外选择指标不为正")
        return ValidationVerdict.WEAK, tuple(warnings)
    metric_degradation = degradation.get(selection_metric)
    if metric_degradation is not None and metric_degradation <= _DEGRADATION_WEAK:
        warnings.append("样本外选择指标退化超过 40%")
        return ValidationVerdict.WEAK, tuple(warnings)
    if metric_degradation is not None and metric_degradation > _DEGRADATION_STRONG:
        return ValidationVerdict.STRONG, tuple(warnings)
    return ValidationVerdict.ACCEPTABLE, tuple(warnings)


def _walk_forward_verdict(
    window_results: Sequence[WindowResult],
    window_count: int,
    selection_metric: str,
    warnings: list[str],
) -> ValidationVerdict:
    if not window_results or len(window_results) < window_count:
        return ValidationVerdict.INCONCLUSIVE
    test_values = [
        result.test_metrics.get(selection_metric) for result in window_results
    ]
    if any(value is None for value in test_values):
        return ValidationVerdict.INCONCLUSIVE
    values = [value for value in test_values if value is not None]
    positive = sum(1 for value in values if value > 0)
    ratio = positive / len(values)
    if ratio >= 0.8:
        return ValidationVerdict.STRONG
    if ratio >= 0.5:
        return ValidationVerdict.ACCEPTABLE
    warnings.append("多数滚动窗口样本外表现不佳")
    return ValidationVerdict.WEAK


def hash_parameters(parameters: Mapping[str, object]) -> str:
    """Return a stable ``sha256:`` digest of one parameter combination."""

    payload = json.dumps(
        {key: str(value) for key, value in parameters.items()},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"sha256:{sha256(payload.encode()).hexdigest()}"


