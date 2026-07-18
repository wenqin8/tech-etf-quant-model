"""Unit tests for the pre-confirmation risk gate."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from etf_quant_lab.domain.risk import (
    RULE_DATA_STALE,
    RULE_DRAWDOWN_HALT,
    RULE_MAX_POSITION_WEIGHT,
    RULE_MAX_TOTAL_WEIGHT,
    RULE_MIN_CASH_WEIGHT,
    RiskDecision,
    RiskLimits,
    assess_rebalance_risk,
    enforce_confirmation_gate,
)

LIMITS = RiskLimits(
    max_position_weight=Decimal("0.35"),
    max_total_weight=Decimal("0.90"),
    min_cash_weight=Decimal("0.10"),
    drawdown_halt=Decimal("0.20"),
    max_data_lag_days=1,
)


def test_compliant_targets_are_allowed() -> None:
    assessment = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.35"), "159915.SZ": Decimal("0.30")},
        limits=LIMITS,
        current_drawdown=Decimal("0.05"),
        trading_day_lag=0,
    )

    assert assessment.decision is RiskDecision.ALLOW
    assert assessment.violations == ()


def test_single_position_over_cap_blocks() -> None:
    assessment = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.40")},
        limits=LIMITS,
    )

    assert assessment.is_blocked
    assert assessment.violations[0].rule_code == RULE_MAX_POSITION_WEIGHT
    assert assessment.violations[0].symbol == "510300.SH"


def test_boundary_value_at_cap_is_allowed() -> None:
    # Exactly at the limit is compliant — the rule blocks only beyond it.
    assessment = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.35")},
        limits=LIMITS,
    )

    assert assessment.decision is RiskDecision.ALLOW


def test_total_weight_and_cash_floor_block_together() -> None:
    weights = {
        "510300.SH": Decimal("0.35"),
        "159915.SZ": Decimal("0.35"),
        "512100.SH": Decimal("0.25"),
    }  # total 0.95 > 0.90 cap, cash 0.05 < 0.10 floor

    assessment = assess_rebalance_risk(target_weights=weights, limits=LIMITS)

    codes = {violation.rule_code for violation in assessment.violations}
    assert assessment.is_blocked
    assert RULE_MAX_TOTAL_WEIGHT in codes
    assert RULE_MIN_CASH_WEIGHT in codes


def test_drawdown_halt_blocks_and_near_halt_warns() -> None:
    halted = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.30")},
        limits=LIMITS,
        current_drawdown=Decimal("0.20"),
    )
    assert halted.is_blocked
    assert halted.violations[0].rule_code == RULE_DRAWDOWN_HALT

    warned = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.30")},
        limits=LIMITS,
        current_drawdown=Decimal("0.17"),  # >= 80% of 0.20
    )
    assert warned.decision is RiskDecision.WARN
    assert not warned.is_blocked

    negative_convention = assess_rebalance_risk(
        target_weights={},
        limits=LIMITS,
        current_drawdown=Decimal("-0.25"),  # drawdown reported as negative
    )
    assert negative_convention.is_blocked


def test_stale_data_blocks_by_lag_or_dates() -> None:
    by_lag = assess_rebalance_risk(
        target_weights={},
        limits=LIMITS,
        trading_day_lag=2,
    )
    assert by_lag.is_blocked
    assert by_lag.violations[0].rule_code == RULE_DATA_STALE

    by_dates = assess_rebalance_risk(
        target_weights={},
        limits=LIMITS,
        data_as_of=date(2026, 7, 10),
        trade_date=date(2026, 7, 15),
    )
    assert by_dates.is_blocked

    fresh = assess_rebalance_risk(
        target_weights={},
        limits=LIMITS,
        data_as_of=date(2026, 7, 14),
        trade_date=date(2026, 7, 15),
    )
    assert fresh.decision is RiskDecision.ALLOW


def test_combined_triggers_report_every_violation() -> None:
    assessment = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.50"), "159915.SZ": Decimal("0.45")},
        limits=LIMITS,
        current_drawdown=Decimal("0.25"),
        trading_day_lag=3,
    )

    codes = {violation.rule_code for violation in assessment.violations}
    assert codes == {
        RULE_MAX_POSITION_WEIGHT,
        RULE_MAX_TOTAL_WEIGHT,
        RULE_MIN_CASH_WEIGHT,
        RULE_DRAWDOWN_HALT,
        RULE_DATA_STALE,
    }
    assert assessment.is_blocked


def test_confirmation_gate_drops_proposals_when_blocked() -> None:
    blocked = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.50")},
        limits=LIMITS,
    )
    allowed = assess_rebalance_risk(
        target_weights={"510300.SH": Decimal("0.30")},
        limits=LIMITS,
    )
    proposals = ("proposal-a", "proposal-b")

    assert enforce_confirmation_gate(blocked, proposals) == ()
    assert enforce_confirmation_gate(allowed, proposals) == proposals


def test_limits_reject_impossible_configuration() -> None:
    with pytest.raises(ValueError, match="exceed 1"):
        RiskLimits(max_total_weight=Decimal("0.95"), min_cash_weight=Decimal("0.10"))

    with pytest.raises(ValueError, match="drawdown_halt"):
        RiskLimits(drawdown_halt=Decimal("1.5"))
