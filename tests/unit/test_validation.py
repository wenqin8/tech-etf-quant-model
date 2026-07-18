"""Unit tests for holdout and walk-forward validation anti-leakage guarantees."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, timedelta

import pytest

from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.validation import (
    DateRange,
    HoldoutSplit,
    SampleRole,
    TrialStatus,
    ValidationVerdict,
)
from etf_quant_lab.domain.validation import (
    expand_parameter_grid,
    generate_walk_forward_windows,
    run_holdout,
    run_walk_forward,
)

TRADING_DATES = tuple(date(2026, 1, 1) + timedelta(days=offset) for offset in range(20))


def test_date_range_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError, match="end"):
        DateRange(start=date(2026, 2, 1), end=date(2026, 1, 1))


def test_holdout_split_rejects_overlap_and_wrong_order() -> None:
    with pytest.raises(ValueError, match="overlap"):
        HoldoutSplit(
            train=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
            test=DateRange(start=date(2026, 6, 1), end=date(2026, 12, 31)),
        )
    with pytest.raises(ValueError, match="after"):
        HoldoutSplit(
            train=DateRange(start=date(2026, 6, 1), end=date(2026, 12, 31)),
            test=DateRange(start=date(2026, 1, 1), end=date(2026, 5, 31)),
        )


def test_expand_parameter_grid_is_deterministic() -> None:
    grid = {"b": [1, 2], "a": ["x"]}

    combinations = expand_parameter_grid(grid)

    assert combinations == (
        {"a": "x", "b": 1},
        {"a": "x", "b": 2},
    )
    assert expand_parameter_grid({}) == ({},)


def test_walk_forward_windows_are_contiguous_and_non_peeking() -> None:
    windows = generate_walk_forward_windows(
        TRADING_DATES,
        train_bars=8,
        test_bars=4,
        step_bars=4,
    )

    assert len(windows) == 3
    for window in windows:
        # Train always ends strictly before its own test begins.
        assert window.train.end < window.test.start
    # Rolling (non-anchored): the train start advances with the step.
    assert windows[0].train.start == TRADING_DATES[0]
    assert windows[1].train.start == TRADING_DATES[4]
    # Consecutive test slices tile forward without gaps.
    assert windows[1].test.start == TRADING_DATES[12]
    assert windows[2].test.start == TRADING_DATES[16]


def test_anchored_windows_keep_first_train_start() -> None:
    windows = generate_walk_forward_windows(
        TRADING_DATES,
        train_bars=8,
        test_bars=4,
        step_bars=4,
        anchored=True,
    )

    assert all(window.train.start == TRADING_DATES[0] for window in windows)
    # The train end still grows with each step.
    assert windows[1].train.end > windows[0].train.end


def test_walk_forward_windows_require_enough_history() -> None:
    with pytest.raises(DomainError) as excinfo:
        generate_walk_forward_windows(
            TRADING_DATES[:5],
            train_bars=8,
            test_bars=4,
            step_bars=4,
        )

    assert excinfo.value.code == "VAL_WINDOWS_INVALID"


def _make_evaluator(
    train_score: dict[int, float],
    *,
    test_score: float = 0.5,
    train_end: date | None = None,
) -> tuple[list[tuple[Mapping[str, object], DateRange]], object]:
    calls: list[tuple[Mapping[str, object], DateRange]] = []

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        calls.append((dict(parameters), period))
        lookback = int(parameters["lookback"])  # type: ignore[arg-type]
        if train_end is not None and period.end <= train_end:
            return {"sharpe_ratio": train_score[lookback]}
        return {"sharpe_ratio": test_score}

    return calls, evaluate


def test_holdout_selects_on_train_and_tests_frozen_winner_once() -> None:
    split = HoldoutSplit(
        train=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
        test=DateRange(start=date(2026, 7, 1), end=date(2026, 12, 31)),
    )
    calls, evaluate = _make_evaluator(
        {20: 0.3, 60: 0.9, 120: 0.6},
        test_score=0.8,
        train_end=split.train.end,
    )

    report = run_holdout(
        evaluate,
        parameter_grid={"lookback": [20, 60, 120]},
        split=split,
        selection_metric="sharpe_ratio",
    )

    assert report.selected_parameters == {"lookback": 60}
    # Exactly one call touched the test period, and it used the frozen winner.
    test_calls = [(p, r) for p, r in calls if r == split.test]
    assert test_calls == [({"lookback": 60}, split.test)]
    # All trials are kept: 3 train + 1 test.
    assert len(report.trials) == 4
    assert report.verdict in {ValidationVerdict.ACCEPTABLE, ValidationVerdict.STRONG}
    roles = {trial.sample_role for trial in report.trials}
    assert roles == {SampleRole.IN_SAMPLE, SampleRole.OUT_OF_SAMPLE}


def test_holdout_flags_severe_degradation_as_weak() -> None:
    split = HoldoutSplit(
        train=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
        test=DateRange(start=date(2026, 7, 1), end=date(2026, 12, 31)),
    )
    _, evaluate = _make_evaluator(
        {20: 1.0},
        test_score=0.3,  # -70% degradation
        train_end=split.train.end,
    )

    report = run_holdout(
        evaluate,
        parameter_grid={"lookback": [20]},
        split=split,
        selection_metric="sharpe_ratio",
    )

    assert report.verdict is ValidationVerdict.WEAK
    assert report.degradation["sharpe_ratio"] == pytest.approx(-0.7)


def test_holdout_keeps_failed_trials() -> None:
    split = HoldoutSplit(
        train=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
        test=DateRange(start=date(2026, 7, 1), end=date(2026, 12, 31)),
    )

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        if parameters["lookback"] == 20:
            raise RuntimeError("insufficient history")
        return {"sharpe_ratio": 0.6}

    report = run_holdout(
        evaluate,
        parameter_grid={"lookback": [20, 60]},
        split=split,
        selection_metric="sharpe_ratio",
    )

    failed = [trial for trial in report.trials if trial.status is TrialStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].error_message == "insufficient history"
    assert report.selected_parameters == {"lookback": 60}


def test_holdout_with_no_successful_trial_is_inconclusive() -> None:
    split = HoldoutSplit(
        train=DateRange(start=date(2026, 1, 1), end=date(2026, 6, 30)),
        test=DateRange(start=date(2026, 7, 1), end=date(2026, 12, 31)),
    )

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        raise RuntimeError("always fails")

    report = run_holdout(
        evaluate,
        parameter_grid={"lookback": [20, 60]},
        split=split,
        selection_metric="sharpe_ratio",
    )

    assert report.verdict is ValidationVerdict.INCONCLUSIVE
    assert report.selected_parameters is None
    assert len(report.trials) == 2  # the test period was never touched


def test_walk_forward_freezes_parameters_per_window_without_peeking() -> None:
    windows = generate_walk_forward_windows(
        TRADING_DATES,
        train_bars=8,
        test_bars=4,
        step_bars=4,
    )
    calls: list[tuple[Mapping[str, object], DateRange]] = []

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        calls.append((dict(parameters), period))
        # Parameter 60 wins every train window; test slices score positive.
        lookback = int(parameters["lookback"])  # type: ignore[arg-type]
        is_train = any(period == window.train for window in windows)
        if is_train:
            return {"sharpe_ratio": 0.9 if lookback == 60 else 0.2}
        return {"sharpe_ratio": 0.4}

    report = run_walk_forward(
        evaluate,
        parameter_grid={"lookback": [20, 60]},
        windows=windows,
        selection_metric="sharpe_ratio",
    )

    assert len(report.window_results) == len(windows)
    for result in report.window_results:
        assert result.selected_parameters == {"lookback": 60}
    # Each window's test slice was evaluated exactly once, with frozen params.
    for window in windows:
        test_calls = [(p, r) for p, r in calls if r == window.test]
        assert test_calls == [({"lookback": 60}, window.test)]
    assert report.verdict is ValidationVerdict.STRONG
    # 2 train trials + 1 test trial per window.
    assert len(report.trials) == len(windows) * 3


def test_walk_forward_mostly_negative_windows_is_weak() -> None:
    windows = generate_walk_forward_windows(
        TRADING_DATES,
        train_bars=8,
        test_bars=4,
        step_bars=4,
    )

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        is_train = any(period == window.train for window in windows)
        return {"sharpe_ratio": 0.5 if is_train else -0.2}

    report = run_walk_forward(
        evaluate,
        parameter_grid={"lookback": [60]},
        windows=windows,
        selection_metric="sharpe_ratio",
    )

    assert report.verdict is ValidationVerdict.WEAK


def test_walk_forward_failed_window_is_inconclusive_and_reported() -> None:
    windows = generate_walk_forward_windows(
        TRADING_DATES,
        train_bars=8,
        test_bars=4,
        step_bars=4,
    )

    def evaluate(parameters: Mapping[str, object], period: DateRange) -> dict[str, float]:
        if period == windows[1].train:
            raise RuntimeError("window data corrupted")
        return {"sharpe_ratio": 0.5}

    report = run_walk_forward(
        evaluate,
        parameter_grid={"lookback": [60]},
        windows=windows,
        selection_metric="sharpe_ratio",
    )

    assert report.verdict is ValidationVerdict.INCONCLUSIVE
    assert any("窗口 1" in warning for warning in report.warnings)
    assert report.failed_trial_count == 1
