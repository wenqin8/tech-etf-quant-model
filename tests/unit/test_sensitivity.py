"""Unit tests for parameter sensitivity and cost stress engines."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.enums import CostScenario
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.contracts.validation import DateRange, TrialStatus, ValidationVerdict
from etf_quant_lab.domain.sensitivity import (
    run_cost_stress,
    run_sensitivity,
    split_by_market_phase,
)

PERIOD = DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30))
GRID = {"lookback": [20, 60, 120], "top_n": [2, 3]}


def _cost_models() -> dict[CostScenario, CostModel]:
    def model(scenario: CostScenario, rate: str, minimum: str, slippage: str) -> CostModel:
        return CostModel(
            scenario=scenario,
            commission_rate=Decimal(rate),
            minimum_commission=Decimal(minimum),
            slippage_bps=Decimal(slippage),
        )

    return {
        CostScenario.IDEAL: model(CostScenario.IDEAL, "0.0001", "0", "0"),
        CostScenario.NORMAL: model(CostScenario.NORMAL, "0.00025", "5", "5"),
        CostScenario.PESSIMISTIC: model(CostScenario.PESSIMISTIC, "0.0005", "5", "15"),
    }


def test_sensitivity_keeps_every_trial_and_builds_full_heatmap() -> None:
    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        if parameters["lookback"] == 120:
            raise RuntimeError("insufficient history")
        return {"sharpe_ratio": 0.5 + 0.1 * int(parameters["top_n"])}  # type: ignore[call-overload]

    report = run_sensitivity(
        evaluate,
        parameter_grid=GRID,
        period=PERIOD,
        objective_metric="sharpe_ratio",
    )

    assert len(report.trials) == 6  # every combination, failures included
    assert len(report.heatmap) == 6
    assert report.failed_trial_count == 2
    failed = [cell for cell in report.heatmap if cell.status is TrialStatus.FAILED]
    assert all(cell.objective_value is None for cell in failed)


def test_sensitivity_plateau_is_acceptable() -> None:
    # Flat surface: every neighbor scores the same as the best point.
    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        return {"sharpe_ratio": 0.8}

    report = run_sensitivity(
        evaluate,
        parameter_grid=GRID,
        period=PERIOD,
        objective_metric="sharpe_ratio",
    )

    assert report.neighborhood_stability_score == pytest.approx(1.0)
    assert report.verdict is ValidationVerdict.ACCEPTABLE
    assert report.fragile_parameters == ()


def test_sensitivity_lone_spike_is_rejected() -> None:
    # One spike at (60, 3); every neighbor is near zero.
    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        if parameters["lookback"] == 60 and parameters["top_n"] == 3:
            return {"sharpe_ratio": 2.0}
        return {"sharpe_ratio": 0.05}

    report = run_sensitivity(
        evaluate,
        parameter_grid=GRID,
        period=PERIOD,
        objective_metric="sharpe_ratio",
    )

    assert report.best_parameters == {"lookback": 60, "top_n": 3}
    assert report.neighborhood_stability_score is not None
    assert report.neighborhood_stability_score < 0.4
    assert report.verdict is ValidationVerdict.REJECTED
    assert set(report.fragile_parameters) == {"lookback", "top_n"}


def test_sensitivity_enforces_trial_limit() -> None:
    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        return {"sharpe_ratio": 0.5}

    with pytest.raises(DomainError) as excinfo:
        run_sensitivity(
            evaluate,
            parameter_grid=GRID,
            period=PERIOD,
            objective_metric="sharpe_ratio",
            max_trials=3,
        )

    assert excinfo.value.code == "VAL_TRIAL_LIMIT_EXCEEDED"


def test_sensitivity_all_failed_is_inconclusive() -> None:
    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        raise RuntimeError("boom")

    report = run_sensitivity(
        evaluate,
        parameter_grid={"lookback": [20]},
        period=PERIOD,
        objective_metric="sharpe_ratio",
    )

    assert report.verdict is ValidationVerdict.INCONCLUSIVE
    assert report.best_parameters is None


def test_cost_stress_reports_erosion_and_survival() -> None:
    returns = {
        CostScenario.IDEAL: 0.10,
        CostScenario.NORMAL: 0.07,
        CostScenario.PESSIMISTIC: 0.03,
    }

    def evaluate(cost_model: CostModel) -> dict[str, float]:
        return {"annual_return": returns[cost_model.scenario]}

    report = run_cost_stress(
        evaluate,
        cost_models=_cost_models(),
        survival_metric="annual_return",
    )

    assert report.verdict is ValidationVerdict.ACCEPTABLE
    by_scenario = {result.scenario: result for result in report.results}
    assert by_scenario[CostScenario.PESSIMISTIC].erosion["annual_return"] == pytest.approx(-0.7)
    assert all(result.survives for result in report.results)


def test_cost_stress_rejects_strategy_that_dies_under_pessimistic_costs() -> None:
    returns = {
        CostScenario.IDEAL: 0.08,
        CostScenario.NORMAL: 0.02,
        CostScenario.PESSIMISTIC: -0.03,
    }

    def evaluate(cost_model: CostModel) -> dict[str, float]:
        return {"annual_return": returns[cost_model.scenario]}

    report = run_cost_stress(
        evaluate,
        cost_models=_cost_models(),
        survival_metric="annual_return",
    )

    assert report.verdict is ValidationVerdict.REJECTED
    assert any("PESSIMISTIC" in warning for warning in report.warnings)


def test_cost_stress_weak_when_normal_fails_but_pessimistic_survives() -> None:
    # Unusual but possible with survival thresholds on other metrics; the grade
    # then reflects the NORMAL failure without a full rejection.
    returns = {
        CostScenario.IDEAL: 0.08,
        CostScenario.NORMAL: -0.01,
        CostScenario.PESSIMISTIC: 0.01,
    }

    def evaluate(cost_model: CostModel) -> dict[str, float]:
        return {"annual_return": returns[cost_model.scenario]}

    report = run_cost_stress(
        evaluate,
        cost_models=_cost_models(),
        survival_metric="annual_return",
    )

    assert report.verdict is ValidationVerdict.WEAK


def test_cost_stress_requires_all_three_scenarios() -> None:
    models = _cost_models()
    del models[CostScenario.PESSIMISTIC]

    with pytest.raises(ValueError, match="PESSIMISTIC"):
        run_cost_stress(
            lambda model: {"annual_return": 0.05},
            cost_models=models,
            survival_metric="annual_return",
        )


def test_market_phase_segments_must_not_overlap() -> None:
    ordered = split_by_market_phase(
        [
            DateRange(start=date(2026, 7, 1), end=date(2026, 12, 31)),
            DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
        ]
    )
    assert ordered[0].start == date(2026, 1, 1)

    with pytest.raises(ValueError, match="overlap"):
        split_by_market_phase(
            [
                DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
                DateRange(start=date(2026, 6, 1), end=date(2026, 12, 31)),
            ]
        )
