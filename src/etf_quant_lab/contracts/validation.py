"""Stable contracts for holdout and walk-forward validation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum


class SampleRole(StrEnum):
    IN_SAMPLE = "IN_SAMPLE"
    OUT_OF_SAMPLE = "OUT_OF_SAMPLE"
    WALK_FORWARD = "WALK_FORWARD"


class TrialStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class ValidationVerdict(StrEnum):
    STRONG = "STRONG"
    ACCEPTABLE = "ACCEPTABLE"
    WEAK = "WEAK"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"


@dataclass(frozen=True, slots=True)
class DateRange:
    """An inclusive evaluation period."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError("end must not precede start")

    def overlaps(self, other: DateRange) -> bool:
        """Whether two inclusive ranges share at least one day."""

        return self.start <= other.end and other.start <= self.end


@dataclass(frozen=True, slots=True)
class HoldoutSplit:
    """Mutually exclusive train and test periods; test strictly follows train."""

    train: DateRange
    test: DateRange

    def __post_init__(self) -> None:
        if self.train.overlaps(self.test):
            raise ValueError("train and test periods must not overlap")
        if self.test.start <= self.train.end:
            raise ValueError("test period must start after the train period ends")


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """One train/test pair inside a walk-forward sequence."""

    index: int
    train: DateRange
    test: DateRange

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("index must not be negative")
        if self.train.overlaps(self.test):
            raise ValueError("window train and test must not overlap")
        if self.test.start <= self.train.end:
            raise ValueError("window test must start after its train period")


@dataclass(frozen=True, slots=True)
class TrialRecord:
    """One parameter evaluation, kept whether it succeeded or failed."""

    trial_no: int
    parameters: Mapping[str, object]
    params_hash: str
    sample_role: SampleRole
    status: TrialStatus
    period: DateRange
    metrics: Mapping[str, float] = field(default_factory=dict)
    objective_value: float | None = None
    window_index: int | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.trial_no < 0:
            raise ValueError("trial_no must not be negative")


@dataclass(frozen=True, slots=True)
class WindowResult:
    """A walk-forward window's frozen parameters and out-of-sample outcome."""

    window: WalkForwardWindow
    selected_parameters: Mapping[str, object]
    selected_params_hash: str
    train_objective: float
    test_metrics: Mapping[str, float]


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Complete outcome of one holdout or walk-forward validation run.

    Every attempted trial appears in ``trials`` — never only the best one — so
    the research record cannot silently hide failed configurations.
    """

    validation_type: str
    selection_metric: str
    trials: tuple[TrialRecord, ...]
    verdict: ValidationVerdict
    selected_parameters: Mapping[str, object] | None = None
    train_metrics: Mapping[str, float] = field(default_factory=dict)
    test_metrics: Mapping[str, float] = field(default_factory=dict)
    degradation: Mapping[str, float] = field(default_factory=dict)
    window_results: tuple[WindowResult, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def failed_trial_count(self) -> int:
        return sum(1 for trial in self.trials if trial.status == TrialStatus.FAILED)
