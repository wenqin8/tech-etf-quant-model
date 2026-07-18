"""Application service for the data-quality gate and cross-source comparison."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import Exchange, QualityGateStatus
from etf_quant_lab.contracts.quality import (
    QualityFinding,
    QualityReport,
    QualityThresholds,
    RunQualityChecksRequest,
    SourceComparisonReport,
    SourceDifference,
)
from etf_quant_lab.domain import quality as rules
from etf_quant_lab.domain.repositories import TradingCalendarRepository
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import DataBatchRepository


class QualityService:
    """Run batch-level quality rules and persist a gate report.

    The service reads canonical bars for a batch, applies the pure rule functions
    with calendar context, aggregates a gate status and stores the report and its
    issues.  It never mutates market data or the batch lifecycle; the caller
    decides whether a ``FAILED`` gate rejects the batch.
    """

    def __init__(
        self,
        *,
        batch_repository: DataBatchRepository,
        calendar_repository: TradingCalendarRepository,
        report_repository: QualityReportRepository,
        id_generator: IdGenerator,
        thresholds: QualityThresholds | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._batches = batch_repository
        self._calendar = calendar_repository
        self._reports = report_repository
        self._id_generator = id_generator
        self._thresholds = thresholds or QualityThresholds()
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_checks(self, request: RunQualityChecksRequest) -> QualityReport:
        """Validate one batch's canonical bars and persist the resulting report."""

        bars = self._batches.query_daily_bars_for_batch(request.batch_id)
        as_of_date = request.as_of_date or self._clock().astimezone(UTC).date()
        findings = self._collect_findings(bars, request.exchange, as_of_date)
        report = QualityReport(
            report_id=self._id_generator.new(),
            batch_id=request.batch_id,
            ruleset_version=request.ruleset_version,
            gate_status=rules.aggregate_gate_status(findings),
            checked_rows=len(bars),
            findings=tuple(findings),
            generated_at=self._clock(),
        )
        self._reports.save(report)
        return report

    def compare_sources(
        self,
        primary_bars: tuple[DailyBar, ...],
        secondary_closes: dict[tuple[str, date], float | str],
    ) -> SourceComparisonReport:
        """Compare primary closes against a secondary sample and grade the result."""

        secondary = {
            key: value if isinstance(value, Decimal) else Decimal(str(value))
            for key, value in secondary_closes.items()
        }
        differences, matched, missing_primary, missing_secondary = rules.compare_sources(
            primary_bars,
            secondary,
            thresholds=self._thresholds,
        )
        gate_status = self._comparison_gate_status(matched, differences)
        max_difference = max(
            (difference.relative_difference for difference in differences),
            default=Decimal("0"),
        )
        return SourceComparisonReport(
            gate_status=gate_status,
            matched_rows=matched,
            missing_in_primary=missing_primary,
            missing_in_secondary=missing_secondary,
            mismatch_count=len(differences),
            max_price_relative_difference=max_difference,
            differences=tuple(differences),
        )

    def get_report(self, batch_id: str) -> QualityReport | None:
        """Return the stored report for one batch when present."""

        return self._reports.get_for_batch(batch_id)

    def _collect_findings(
        self,
        bars: tuple[DailyBar, ...],
        exchange: Exchange,
        as_of_date: date,
    ) -> list[QualityFinding]:
        findings: list[QualityFinding] = []
        findings.extend(rules.check_duplicate_keys(bars))
        findings.extend(rules.check_future_dates(bars, as_of_date=as_of_date))
        findings.extend(rules.check_extreme_returns(bars, thresholds=self._thresholds))
        if not bars:
            return findings

        open_dates, ordered_open = self._open_dates(exchange, bars)
        if open_dates:
            findings.extend(rules.check_calendar_consistency(bars, open_dates=open_dates))
            findings.extend(
                rules.check_missing_trading_days(bars, expected_open_dates=open_dates)
            )
        findings.extend(
            rules.check_staleness(
                bars,
                expected_last_open_date=self._expected_last_open_date(exchange, as_of_date),
                thresholds=self._thresholds,
                open_dates=ordered_open,
            )
        )
        return findings

    def _open_dates(
        self,
        exchange: Exchange,
        bars: tuple[DailyBar, ...],
    ) -> tuple[frozenset[date], tuple[date, ...]]:
        lower = min(bar.trade_date for bar in bars)
        upper = max(bar.trade_date for bar in bars)
        days = self._calendar.list_days(exchange, lower, upper)
        ordered = tuple(day.cal_date for day in days if day.is_open)
        return frozenset(ordered), ordered

    def _expected_last_open_date(self, exchange: Exchange, as_of_date: date) -> date | None:
        return self._calendar.previous_open_date(exchange, as_of_date, inclusive=True)

    def _comparison_gate_status(
        self,
        matched: int,
        differences: list[SourceDifference],
    ) -> QualityGateStatus:
        if matched < self._thresholds.cross_source_min_overlap:
            return QualityGateStatus.FAILED
        if differences:
            return QualityGateStatus.FAILED
        return QualityGateStatus.PASSED
