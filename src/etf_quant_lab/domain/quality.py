"""Framework-independent daily-bar quality rules and gate aggregation.

These functions are pure: they accept canonical :class:`DailyBar` records plus
calendar context and return :class:`QualityFinding` objects.  Persistence,
Parquet access and batch lifecycle transitions stay in the service and storage
layers.  Positive-price and OHLC-ordering invariants are already enforced by the
:class:`DailyBar` contract, so the batch-level rules here focus on completeness,
uniqueness, freshness, extreme moves and cross-source agreement.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from itertools import pairwise

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import QualityGateStatus, QualitySeverity
from etf_quant_lab.contracts.quality import (
    QualityFinding,
    QualityThresholds,
    SourceDifference,
)

RULE_DUPLICATE_KEY = "daily_bar.duplicate_key"
RULE_FUTURE_DATE = "daily_bar.future_date"
RULE_CALENDAR_GAP = "daily_bar.trading_calendar_gap"
RULE_NON_TRADING_DATE = "daily_bar.non_trading_date"
RULE_EXTREME_RETURN = "daily_bar.extreme_return"
RULE_STALENESS = "daily_bar.staleness"
RULE_CROSS_SOURCE_DIFFERENCE = "daily_bar.cross_source_difference"

_BLOCKING = QualitySeverity.BLOCKING
_ERROR = QualitySeverity.ERROR
_WARNING = QualitySeverity.WARNING


def check_duplicate_keys(bars: Sequence[DailyBar]) -> list[QualityFinding]:
    """Flag any repeated ``(symbol, trade_date)`` pair as a blocking violation."""

    seen: set[tuple[str, date]] = set()
    duplicates: dict[tuple[str, date], int] = {}
    for bar in bars:
        key = (bar.symbol, bar.trade_date)
        if key in seen:
            duplicates[key] = duplicates.get(key, 1) + 1
        else:
            seen.add(key)
    findings: list[QualityFinding] = []
    for (symbol, trade_date), occurrences in sorted(duplicates.items()):
        findings.append(
            QualityFinding(
                rule_code=RULE_DUPLICATE_KEY,
                severity=_BLOCKING,
                message="同一标的与交易日出现重复行情记录",
                symbol=symbol,
                trade_date=trade_date,
                observed_value={"occurrences": occurrences},
            )
        )
    return findings


def check_future_dates(bars: Iterable[DailyBar], *, as_of_date: date) -> list[QualityFinding]:
    """Flag any bar dated after the run's knowable date to prevent future data."""

    findings: list[QualityFinding] = []
    for bar in bars:
        if bar.trade_date > as_of_date:
            findings.append(
                QualityFinding(
                    rule_code=RULE_FUTURE_DATE,
                    severity=_BLOCKING,
                    message="行情日期晚于运行时可知日期",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    observed_value={"trade_date": bar.trade_date.isoformat()},
                    expected_value={"max_trade_date": as_of_date.isoformat()},
                )
            )
    return findings


def check_calendar_consistency(
    bars: Iterable[DailyBar],
    *,
    open_dates: frozenset[date],
) -> list[QualityFinding]:
    """Flag bars that fall on a known non-trading day.

    ``open_dates`` is the set of exchange open dates covering the batch range.  A
    bar on a date absent from that set is reported as an error; an empty set means
    the calendar is unknown and the rule is skipped by the caller.
    """

    findings: list[QualityFinding] = []
    for bar in bars:
        if bar.trade_date not in open_dates:
            findings.append(
                QualityFinding(
                    rule_code=RULE_NON_TRADING_DATE,
                    severity=_ERROR,
                    message="行情日期不在交易日历的开市日集合中",
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    observed_value={"trade_date": bar.trade_date.isoformat()},
                )
            )
    return findings


def check_missing_trading_days(
    bars: Sequence[DailyBar],
    *,
    expected_open_dates: frozenset[date],
) -> list[QualityFinding]:
    """Flag active symbols missing rows on expected open days within their range.

    Only gaps strictly inside each symbol's observed ``[min, max]`` date span are
    reported, so partial history at the batch edges is not treated as a defect.
    """

    by_symbol: dict[str, list[date]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar.trade_date)

    findings: list[QualityFinding] = []
    for symbol, symbol_dates in sorted(by_symbol.items()):
        present = set(symbol_dates)
        lower = min(symbol_dates)
        upper = max(symbol_dates)
        expected_in_range = sorted(
            day for day in expected_open_dates if lower <= day <= upper
        )
        missing = [day for day in expected_in_range if day not in present]
        for day in missing:
            findings.append(
                QualityFinding(
                    rule_code=RULE_CALENDAR_GAP,
                    severity=_ERROR,
                    message="活跃标的在预期交易日缺少行情",
                    symbol=symbol,
                    trade_date=day,
                    observed_value={"present": False},
                    expected_value={"trade_date": day.isoformat()},
                )
            )
    return findings


def check_extreme_returns(
    bars: Sequence[DailyBar],
    *,
    thresholds: QualityThresholds,
) -> list[QualityFinding]:
    """Flag large single-day close-to-close moves without altering the data."""

    by_symbol: dict[str, list[DailyBar]] = {}
    for bar in bars:
        by_symbol.setdefault(bar.symbol, []).append(bar)

    findings: list[QualityFinding] = []
    for symbol in sorted(by_symbol):
        ordered = sorted(by_symbol[symbol], key=lambda item: item.trade_date)
        for previous, current in pairwise(ordered):
            if previous.close <= 0:
                continue
            change = abs(current.close - previous.close) / previous.close
            severity = _return_severity(change, thresholds)
            if severity is None:
                continue
            findings.append(
                QualityFinding(
                    rule_code=RULE_EXTREME_RETURN,
                    severity=severity,
                    message="单日收盘价变动超过配置阈值",
                    symbol=symbol,
                    trade_date=current.trade_date,
                    observed_value={"relative_change": _format_ratio(change)},
                    expected_value={
                        "warn_threshold": _format_ratio(thresholds.extreme_return_warn),
                        "error_threshold": _format_ratio(thresholds.extreme_return_error),
                    },
                )
            )
    return findings


def check_staleness(
    bars: Iterable[DailyBar],
    *,
    expected_last_open_date: date | None,
    thresholds: QualityThresholds,
    open_dates: Sequence[date],
) -> list[QualityFinding]:
    """Flag a batch whose most recent bar lags the expected latest open date.

    ``open_dates`` is the ordered set of exchange open days; staleness is measured
    in trading days, not natural days, so weekends and holidays do not count.
    """

    if expected_last_open_date is None:
        return []
    observed_dates = [bar.trade_date for bar in bars]
    if not observed_dates:
        return []
    latest = max(observed_dates)
    if latest >= expected_last_open_date:
        return []
    lag = _trading_day_distance(latest, expected_last_open_date, open_dates)
    if lag <= thresholds.staleness_max_trading_days:
        return []
    return [
        QualityFinding(
            rule_code=RULE_STALENESS,
            severity=_BLOCKING,
            message="批次最新数据落后于预期最近交易日",
            trade_date=latest,
            observed_value={"latest_trade_date": latest.isoformat(), "lag_trading_days": lag},
            expected_value={
                "expected_last_open_date": expected_last_open_date.isoformat(),
                "max_lag_trading_days": thresholds.staleness_max_trading_days,
            },
        )
    ]


def compare_sources(
    primary: Sequence[DailyBar],
    secondary: Mapping[tuple[str, date], Decimal],
    *,
    thresholds: QualityThresholds,
) -> tuple[list[SourceDifference], int, int, int]:
    """Compare primary closes against a secondary ``(symbol, date) -> close`` map.

    Returns the differences beyond tolerance, the matched-row count, and the rows
    missing from each side.  The comparison only covers keys present in both
    inputs; non-overlapping keys are counted as missing, never as mismatches.
    """

    primary_keys = {(bar.symbol, bar.trade_date): bar.close for bar in primary}
    differences: list[SourceDifference] = []
    matched = 0
    for key, primary_close in sorted(primary_keys.items()):
        secondary_close = secondary.get(key)
        if secondary_close is None:
            continue
        matched += 1
        if primary_close <= 0:
            continue
        relative = abs(primary_close - secondary_close) / primary_close
        if relative > thresholds.cross_source_price_tolerance:
            symbol, trade_date = key
            differences.append(
                SourceDifference(
                    symbol=symbol,
                    trade_date=trade_date,
                    field_name="close",
                    primary_value=primary_close,
                    secondary_value=secondary_close,
                    relative_difference=relative,
                )
            )
    missing_in_secondary = sum(1 for key in primary_keys if key not in secondary)
    missing_in_primary = sum(1 for key in secondary if key not in primary_keys)
    return differences, matched, missing_in_primary, missing_in_secondary


def aggregate_gate_status(findings: Iterable[QualityFinding]) -> QualityGateStatus:
    """Reduce findings to a single gate status.

    Any ``BLOCKING`` or ``ERROR`` finding fails the gate; only ``WARNING`` or
    ``INFO`` yields a pass-with-warnings; no findings yields a clean pass.
    """

    has_blocking = False
    has_warning = False
    for finding in findings:
        if finding.severity in {_BLOCKING, _ERROR}:
            has_blocking = True
        elif finding.severity is _WARNING:
            has_warning = True
    if has_blocking:
        return QualityGateStatus.FAILED
    if has_warning:
        return QualityGateStatus.PASSED_WITH_WARNINGS
    return QualityGateStatus.PASSED


def _return_severity(
    change: Decimal,
    thresholds: QualityThresholds,
) -> QualitySeverity | None:
    if change >= thresholds.extreme_return_error:
        return _ERROR
    if change >= thresholds.extreme_return_warn:
        return _WARNING
    return None


def _trading_day_distance(
    start_exclusive: date,
    end_inclusive: date,
    open_dates: Sequence[date],
) -> int:
    """Count open days in ``(start_exclusive, end_inclusive]``.

    Falls back to a natural-day difference when the calendar does not cover the
    interval, so an unknown calendar never hides a stale batch.
    """

    known = sorted(day for day in open_dates if start_exclusive < day <= end_inclusive)
    if known:
        return len(known)
    return (end_inclusive - start_exclusive).days


def _format_ratio(value: Decimal) -> str:
    return format(value, "f")
