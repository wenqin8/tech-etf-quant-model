"""Unit tests for the pure daily-bar quality rules and gate aggregation."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import (
    DataSource,
    Exchange,
    QualityGateStatus,
    QualitySeverity,
)
from etf_quant_lab.contracts.quality import QualityFinding, QualityThresholds
from etf_quant_lab.domain import quality as rules

BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NE1"
INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
THRESHOLDS = QualityThresholds()


def _bar(*, symbol: str = "510300.SH", trade_date: date, close: str = "4.000") -> DailyBar:
    close_price = Decimal(close)
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        open=close_price,
        high=close_price + Decimal("0.050"),
        low=max(close_price - Decimal("0.050"), Decimal("0.001")),
        close=close_price,
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id=BATCH_ID,
        ingested_at=INGESTED_AT,
    )


def test_duplicate_keys_are_blocking() -> None:
    bars = (
        _bar(trade_date=date(2026, 7, 10)),
        _bar(trade_date=date(2026, 7, 10)),
        _bar(symbol="159915.SZ", trade_date=date(2026, 7, 10)),
    )

    findings = rules.check_duplicate_keys(bars)

    assert len(findings) == 1
    assert findings[0].rule_code == rules.RULE_DUPLICATE_KEY
    assert findings[0].severity is QualitySeverity.BLOCKING
    assert findings[0].symbol == "510300.SH"


def test_future_dates_are_blocking() -> None:
    bars = (
        _bar(trade_date=date(2026, 7, 10)),
        _bar(trade_date=date(2026, 7, 15)),
    )

    findings = rules.check_future_dates(bars, as_of_date=date(2026, 7, 13))

    assert [finding.trade_date for finding in findings] == [date(2026, 7, 15)]
    assert findings[0].severity is QualitySeverity.BLOCKING


def test_non_trading_dates_are_errors() -> None:
    bars = (
        _bar(trade_date=date(2026, 7, 10)),
        _bar(trade_date=date(2026, 7, 11)),
    )
    open_dates = frozenset({date(2026, 7, 10), date(2026, 7, 13)})

    findings = rules.check_calendar_consistency(bars, open_dates=open_dates)

    assert [finding.trade_date for finding in findings] == [date(2026, 7, 11)]
    assert findings[0].rule_code == rules.RULE_NON_TRADING_DATE


def test_missing_trading_days_flag_interior_gaps_only() -> None:
    bars = (
        _bar(trade_date=date(2026, 7, 8)),
        _bar(trade_date=date(2026, 7, 13)),
    )
    open_dates = frozenset(
        {date(2026, 7, 7), date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13)}
    )

    findings = rules.check_missing_trading_days(bars, expected_open_dates=open_dates)

    assert [finding.trade_date for finding in findings] == [
        date(2026, 7, 9),
        date(2026, 7, 10),
    ]
    assert all(finding.severity is QualitySeverity.ERROR for finding in findings)


def test_extreme_returns_use_warn_and_error_levels() -> None:
    bars = (
        _bar(trade_date=date(2026, 7, 8), close="4.000"),
        _bar(trade_date=date(2026, 7, 9), close="4.700"),
        _bar(trade_date=date(2026, 7, 10), close="6.500"),
    )

    findings = rules.check_extreme_returns(bars, thresholds=THRESHOLDS)

    by_date = {finding.trade_date: finding.severity for finding in findings}
    assert by_date == {
        date(2026, 7, 9): QualitySeverity.WARNING,
        date(2026, 7, 10): QualitySeverity.ERROR,
    }


def test_staleness_measured_in_trading_days() -> None:
    bars = (_bar(trade_date=date(2026, 7, 8)),)
    open_dates = (date(2026, 7, 8), date(2026, 7, 9), date(2026, 7, 10), date(2026, 7, 13))

    findings = rules.check_staleness(
        bars,
        expected_last_open_date=date(2026, 7, 13),
        thresholds=THRESHOLDS,
        open_dates=open_dates,
    )

    assert len(findings) == 1
    assert findings[0].severity is QualitySeverity.BLOCKING
    assert findings[0].observed_value["lag_trading_days"] == 3


def test_staleness_within_allowance_passes() -> None:
    bars = (_bar(trade_date=date(2026, 7, 10)),)
    open_dates = (date(2026, 7, 10), date(2026, 7, 13))

    findings = rules.check_staleness(
        bars,
        expected_last_open_date=date(2026, 7, 13),
        thresholds=THRESHOLDS,
        open_dates=open_dates,
    )

    assert findings == []


def test_compare_sources_reports_only_overlap_mismatches() -> None:
    primary = (
        _bar(trade_date=date(2026, 7, 9), close="4.000"),
        _bar(trade_date=date(2026, 7, 10), close="4.100"),
        _bar(trade_date=date(2026, 7, 13), close="4.200"),
    )
    secondary = {
        ("510300.SH", date(2026, 7, 9)): Decimal("4.000"),
        ("510300.SH", date(2026, 7, 10)): Decimal("4.500"),
        ("510300.SH", date(2026, 7, 14)): Decimal("4.300"),
    }

    differences, matched, missing_primary, missing_secondary = rules.compare_sources(
        primary,
        secondary,
        thresholds=THRESHOLDS,
    )

    assert matched == 2
    assert missing_primary == 1
    assert missing_secondary == 1
    assert [difference.trade_date for difference in differences] == [date(2026, 7, 10)]
    assert differences[0].relative_difference > Decimal("0.09")


def test_gate_status_aggregation_levels() -> None:
    def finding(severity: QualitySeverity) -> QualityFinding:
        return QualityFinding(rule_code="r", severity=severity, message="m")

    assert rules.aggregate_gate_status([]) is QualityGateStatus.PASSED
    assert (
        rules.aggregate_gate_status([finding(QualitySeverity.INFO)])
        is QualityGateStatus.PASSED
    )
    assert (
        rules.aggregate_gate_status([finding(QualitySeverity.WARNING)])
        is QualityGateStatus.PASSED_WITH_WARNINGS
    )
    assert (
        rules.aggregate_gate_status(
            [finding(QualitySeverity.WARNING), finding(QualitySeverity.ERROR)]
        )
        is QualityGateStatus.FAILED
    )
    assert (
        rules.aggregate_gate_status([finding(QualitySeverity.BLOCKING)])
        is QualityGateStatus.FAILED
    )


def test_thresholds_reject_inconsistent_configuration() -> None:
    with pytest.raises(ValueError, match="error"):
        QualityThresholds(
            extreme_return_warn=Decimal("0.30"),
            extreme_return_error=Decimal("0.15"),
        )
