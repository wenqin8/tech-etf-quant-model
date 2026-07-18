"""Stable contracts for the data-quality gate and cross-source comparison."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.enums import (
    Exchange,
    QualityGateStatus,
    QualitySeverity,
)


@dataclass(frozen=True, slots=True)
class QualityThresholds:
    """Configurable limits for the batch-level quality rules."""

    extreme_return_warn: Decimal = Decimal("0.15")
    extreme_return_error: Decimal = Decimal("0.30")
    staleness_max_trading_days: int = 1
    cross_source_price_tolerance: Decimal = Decimal("0.005")
    cross_source_min_overlap: int = 1

    def __post_init__(self) -> None:
        if self.extreme_return_warn <= 0 or self.extreme_return_error <= 0:
            raise ValueError("extreme-return thresholds must be positive")
        if self.extreme_return_error < self.extreme_return_warn:
            raise ValueError("extreme_return_error must not be smaller than the warn level")
        if self.staleness_max_trading_days < 0:
            raise ValueError("staleness_max_trading_days must not be negative")
        if self.cross_source_price_tolerance <= 0:
            raise ValueError("cross_source_price_tolerance must be positive")
        if self.cross_source_min_overlap < 1:
            raise ValueError("cross_source_min_overlap must be at least 1")


@dataclass(frozen=True, slots=True)
class QualityFinding:
    """One immutable rule violation discovered while validating a batch."""

    rule_code: str
    severity: QualitySeverity
    message: str
    symbol: str | None = None
    trade_date: date | None = None
    observed_value: Mapping[str, object] = field(default_factory=dict)
    expected_value: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.rule_code.strip():
            raise ValueError("rule_code must not be blank")
        if not self.message.strip():
            raise ValueError("message must not be blank")


@dataclass(frozen=True, slots=True)
class QualityReport:
    """Aggregated result of running the quality gate against one batch."""

    report_id: str
    batch_id: str
    ruleset_version: str
    gate_status: QualityGateStatus
    checked_rows: int
    findings: tuple[QualityFinding, ...]
    generated_at: datetime

    def __post_init__(self) -> None:
        if len(self.report_id) != 26:
            raise ValueError("report_id must contain exactly 26 characters")
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")
        if self.checked_rows < 0:
            raise ValueError("checked_rows must not be negative")
        if self.generated_at.tzinfo is None:
            raise ValueError("generated_at must be timezone-aware")

    def count(self, severity: QualitySeverity) -> int:
        """Return how many findings carry the requested severity."""

        return sum(1 for finding in self.findings if finding.severity is severity)

    @property
    def is_blocking(self) -> bool:
        """Whether the gate rejects the batch for signals and backtests."""

        return self.gate_status == QualityGateStatus.FAILED


@dataclass(frozen=True, slots=True)
class RunQualityChecksRequest:
    """Request to validate one canonical daily-bar batch."""

    batch_id: str
    exchange: Exchange
    ruleset_version: str = "daily_bar_rules_v1"
    as_of_date: date | None = None

    def __post_init__(self) -> None:
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")


@dataclass(frozen=True, slots=True)
class SourceDifference:
    """One symbol/date where two sources disagree beyond tolerance."""

    symbol: str
    trade_date: date
    field_name: str
    primary_value: Decimal
    secondary_value: Decimal
    relative_difference: Decimal


@dataclass(frozen=True, slots=True)
class SourceComparisonReport:
    """Outcome of comparing a primary batch against a secondary sample."""

    gate_status: QualityGateStatus
    matched_rows: int
    missing_in_primary: int
    missing_in_secondary: int
    mismatch_count: int
    max_price_relative_difference: Decimal
    differences: tuple[SourceDifference, ...]

    def __post_init__(self) -> None:
        for name, value in (
            ("matched_rows", self.matched_rows),
            ("missing_in_primary", self.missing_in_primary),
            ("missing_in_secondary", self.missing_in_secondary),
            ("mismatch_count", self.mismatch_count),
        ):
            if value < 0:
                raise ValueError(f"{name} must not be negative")
