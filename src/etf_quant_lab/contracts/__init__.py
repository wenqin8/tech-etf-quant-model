"""Public contracts shared by application modules."""

from etf_quant_lab.contracts.common import Page, PageRequest
from etf_quant_lab.contracts.data import (
    DailyBar,
    DailyBarsQuery,
    DataBatch,
    RawProviderBatch,
    TradeCalendarQuery,
)
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.contracts.quality import (
    QualityFinding,
    QualityReport,
    QualityThresholds,
    RunQualityChecksRequest,
    SourceComparisonReport,
    SourceDifference,
)
from etf_quant_lab.contracts.strategy import (
    ParameterSpec,
    ParameterValidationResult,
    StrategyDescriptor,
    TargetAllocation,
    TargetPortfolio,
    ValidateParametersRequest,
    parameter_hash,
)

__all__ = [
    "DailyBar",
    "DailyBarsQuery",
    "DataBatch",
    "DomainError",
    "ErrorCode",
    "Page",
    "PageRequest",
    "ParameterSpec",
    "ParameterValidationResult",
    "QualityFinding",
    "QualityReport",
    "QualityThresholds",
    "RawProviderBatch",
    "RunQualityChecksRequest",
    "SourceComparisonReport",
    "SourceDifference",
    "StrategyDescriptor",
    "TargetAllocation",
    "TargetPortfolio",
    "TradeCalendarQuery",
    "ValidateParametersRequest",
    "parameter_hash",
]
