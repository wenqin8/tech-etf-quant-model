"""Load validated cost scenarios from the YAML configuration."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from etf_quant_lab.contracts.enums import CostScenario
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.contracts.execution import CostModel

_SCENARIO_KEYS = {
    "ideal": CostScenario.IDEAL,
    "normal": CostScenario.NORMAL,
    "pessimistic": CostScenario.PESSIMISTIC,
}


class _ScenarioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    commission_rate: Decimal = Field(ge=0)
    minimum_commission: Decimal = Field(ge=0)
    slippage_bps: Decimal = Field(ge=0)
    transfer_fee_rate: Decimal = Field(default=Decimal(0), ge=0)


class _CostConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    currency: str = "CNY"
    scenarios: dict[str, _ScenarioConfig]


def load_cost_scenarios(config_path: Path) -> dict[CostScenario, CostModel]:
    """Read and validate all cost scenarios; missing standard keys are an error."""

    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        parsed = _CostConfig.model_validate(payload)
    except (OSError, yaml.YAMLError, ValueError) as exc:
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "成本情景配置无法读取或校验失败",
            details={"path": str(config_path)},
        ) from exc

    missing = sorted(set(_SCENARIO_KEYS) - set(parsed.scenarios))
    if missing:
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "成本情景配置缺少标准情景",
            details={"missing": tuple(missing)},
        )

    models: dict[CostScenario, CostModel] = {}
    for key, scenario in _SCENARIO_KEYS.items():
        config = parsed.scenarios[key]
        models[scenario] = CostModel(
            scenario=scenario,
            commission_rate=config.commission_rate,
            minimum_commission=config.minimum_commission,
            slippage_bps=config.slippage_bps,
            transfer_fee_rate=config.transfer_fee_rate,
            currency=parsed.currency,
        )
    _assert_monotonic_severity(models)
    return models


def _assert_monotonic_severity(models: dict[CostScenario, CostModel]) -> None:
    """Reject configs where the pessimistic scenario is cheaper than ideal.

    The validation acceptance rule "悲观成本结果不优于理想成本" starts here: a
    config that inverts scenario severity would silently break that guarantee.
    """

    ideal = models[CostScenario.IDEAL]
    pessimistic = models[CostScenario.PESSIMISTIC]
    if (
        pessimistic.commission_rate < ideal.commission_rate
        or pessimistic.slippage_bps < ideal.slippage_bps
        or pessimistic.minimum_commission < ideal.minimum_commission
    ):
        raise DomainError(
            ErrorCode.CONFIG_INVALID,
            "悲观成本情景不得低于理想情景",
            details={},
        )
