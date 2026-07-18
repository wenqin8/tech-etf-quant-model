"""Pre-confirmation risk gate for daily signals and rebalance proposals (node 17).

Every rule is a pure check over the signal, proposal and account state.  The
gate aggregates rule outcomes into a single risk decision: any BLOCKER stops the
proposal from ever reaching the user-confirmation state (SIG-004 / doc §4.4).
Signal- and order-level idempotency already guard duplicate creation (nodes 14
and 15); this module adds the positional and drawdown limits on top.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

RULE_MAX_POSITION_WEIGHT = "risk.max_position_weight"
RULE_MAX_TOTAL_WEIGHT = "risk.max_total_weight"
RULE_MIN_CASH_WEIGHT = "risk.min_cash_weight"
RULE_DRAWDOWN_HALT = "risk.drawdown_halt"
RULE_DATA_STALE = "risk.data_stale"


class RiskDecision(StrEnum):
    ALLOW = "ALLOW"
    WARN = "WARN"
    HALT = "HALT"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Configurable risk thresholds; validation makes misconfiguration loud."""

    max_position_weight: Decimal = Decimal("0.35")
    max_total_weight: Decimal = Decimal("0.95")
    min_cash_weight: Decimal = Decimal("0.05")
    drawdown_halt: Decimal = Decimal("0.20")
    max_data_lag_days: int = 1

    def __post_init__(self) -> None:
        for name, value in (
            ("max_position_weight", self.max_position_weight),
            ("max_total_weight", self.max_total_weight),
            ("min_cash_weight", self.min_cash_weight),
        ):
            if value < 0 or value > 1:
                raise ValueError(f"{name} must be within [0, 1]")
        if self.max_total_weight + self.min_cash_weight > Decimal("1.000001"):
            raise ValueError("max_total_weight plus min_cash_weight must not exceed 1")
        if self.drawdown_halt <= 0 or self.drawdown_halt >= 1:
            raise ValueError("drawdown_halt must be within (0, 1)")
        if self.max_data_lag_days < 0:
            raise ValueError("max_data_lag_days must not be negative")


@dataclass(frozen=True, slots=True)
class RiskViolation:
    """One triggered rule with its observed and permitted values."""

    rule_code: str
    blocking: bool
    message: str
    observed: str
    limit: str
    symbol: str | None = None


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    """Aggregated outcome; BLOCKED proposals must not reach confirmation."""

    decision: RiskDecision
    violations: tuple[RiskViolation, ...] = ()

    @property
    def is_blocked(self) -> bool:
        return self.decision == RiskDecision.HALT


def assess_rebalance_risk(
    *,
    target_weights: Mapping[str, Decimal],
    limits: RiskLimits,
    current_drawdown: Decimal | None = None,
    data_as_of: date | None = None,
    trade_date: date | None = None,
    trading_day_lag: int | None = None,
) -> RiskAssessment:
    """Run every risk rule and reduce to one decision.

    ``trading_day_lag`` (preferred) counts open days between ``data_as_of`` and
    ``trade_date``; callers without calendar access may pass the natural-day
    difference instead — the rule blocks on whichever is provided.
    """

    violations: list[RiskViolation] = []
    violations.extend(_check_position_weights(target_weights, limits))
    violations.extend(_check_total_and_cash(target_weights, limits))
    violations.extend(_check_drawdown(current_drawdown, limits))
    violations.extend(
        _check_staleness(data_as_of, trade_date, trading_day_lag, limits)
    )

    if any(violation.blocking for violation in violations):
        return RiskAssessment(decision=RiskDecision.HALT, violations=tuple(violations))
    if violations:
        return RiskAssessment(decision=RiskDecision.WARN, violations=tuple(violations))
    return RiskAssessment(decision=RiskDecision.ALLOW)


def _check_position_weights(
    target_weights: Mapping[str, Decimal],
    limits: RiskLimits,
) -> list[RiskViolation]:
    violations: list[RiskViolation] = []
    for symbol, weight in sorted(target_weights.items()):
        if weight > limits.max_position_weight:
            violations.append(
                RiskViolation(
                    rule_code=RULE_MAX_POSITION_WEIGHT,
                    blocking=True,
                    message="单标的目标权重超过上限",
                    observed=str(weight),
                    limit=str(limits.max_position_weight),
                    symbol=symbol,
                )
            )
    return violations


def _check_total_and_cash(
    target_weights: Mapping[str, Decimal],
    limits: RiskLimits,
) -> list[RiskViolation]:
    violations: list[RiskViolation] = []
    total = sum(target_weights.values(), Decimal(0))
    if total > limits.max_total_weight:
        violations.append(
            RiskViolation(
                rule_code=RULE_MAX_TOTAL_WEIGHT,
                blocking=True,
                message="总目标权重超过上限",
                observed=str(total),
                limit=str(limits.max_total_weight),
            )
        )
    cash_weight = Decimal(1) - total
    if cash_weight < limits.min_cash_weight:
        violations.append(
            RiskViolation(
                rule_code=RULE_MIN_CASH_WEIGHT,
                blocking=True,
                message="目标现金比例低于下限",
                observed=str(cash_weight),
                limit=str(limits.min_cash_weight),
            )
        )
    return violations


def _check_drawdown(
    current_drawdown: Decimal | None,
    limits: RiskLimits,
) -> list[RiskViolation]:
    if current_drawdown is None:
        return []
    if current_drawdown < 0:
        current_drawdown = abs(current_drawdown)
    if current_drawdown >= limits.drawdown_halt:
        return [
            RiskViolation(
                rule_code=RULE_DRAWDOWN_HALT,
                blocking=True,
                message="账户回撤触发 HALT",
                observed=str(current_drawdown),
                limit=str(limits.drawdown_halt),
            )
        ]
    # Within 80% of the halt level: warn so the user sees it coming.
    if current_drawdown >= limits.drawdown_halt * Decimal("0.8"):
        return [
            RiskViolation(
                rule_code=RULE_DRAWDOWN_HALT,
                blocking=False,
                message="账户回撤接近 HALT 阈值",
                observed=str(current_drawdown),
                limit=str(limits.drawdown_halt),
            )
        ]
    return []


def _check_staleness(
    data_as_of: date | None,
    trade_date: date | None,
    trading_day_lag: int | None,
    limits: RiskLimits,
) -> list[RiskViolation]:
    if trading_day_lag is None:
        if data_as_of is None or trade_date is None:
            return []
        trading_day_lag = max(0, (trade_date - data_as_of).days)
    if trading_day_lag > limits.max_data_lag_days:
        return [
            RiskViolation(
                rule_code=RULE_DATA_STALE,
                blocking=True,
                message="行情数据过期",
                observed=f"lag={trading_day_lag}",
                limit=f"max={limits.max_data_lag_days}",
            )
        ]
    return []


def enforce_confirmation_gate(
    assessment: RiskAssessment,
    proposals: Sequence[object],
) -> tuple[object, ...]:
    """Return proposals only when the gate allows; HALT yields an empty tuple.

    This is the single choke point between "建议已生成" and "等待用户确认": a
    blocked assessment can never leak a confirmable proposal.
    """

    if assessment.is_blocked:
        return ()
    return tuple(proposals)
