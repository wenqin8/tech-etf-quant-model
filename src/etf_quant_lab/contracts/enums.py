"""Stable string enums shared across application boundaries."""

from enum import StrEnum


class Exchange(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"


class InstrumentType(StrEnum):
    ETF = "ETF"


class InstrumentStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"
    DISABLED = "DISABLED"


class DataSource(StrEnum):
    TUSHARE = "TUSHARE"
    AKSHARE = "AKSHARE"
    CSV = "CSV"


class DataLayer(StrEnum):
    RAW = "RAW"
    CANONICAL = "CANONICAL"
    DERIVED = "DERIVED"
    ARTIFACT = "ARTIFACT"


class DataBatchStatus(StrEnum):
    FETCHING = "FETCHING"
    VALIDATING = "VALIDATING"
    ACTIVE = "ACTIVE"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class PriceAdjustment(StrEnum):
    RAW = "RAW"
    QFQ = "QFQ"
    HFQ = "HFQ"


class QualitySeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    BLOCKING = "BLOCKING"


class QualityGateStatus(StrEnum):
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"


class StrategyId(StrEnum):
    TREND_BASELINE = "TREND_BASELINE"
    ETF_ROTATION = "ETF_ROTATION"
    THREE_DAY_TECH = "THREE_DAY_TECH"


class CostScenario(StrEnum):
    IDEAL = "IDEAL"
    NORMAL = "NORMAL"
    PESSIMISTIC = "PESSIMISTIC"
    CUSTOM = "CUSTOM"


class ExecutionModel(StrEnum):
    NEXT_OPEN = "NEXT_OPEN"
    MANUAL_PRICE = "MANUAL_PRICE"


class RunStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class RiskState(StrEnum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    BLOCKED = "BLOCKED"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(StrEnum):
    DRAFT = "DRAFT"
    PENDING_CONFIRMATION = "PENDING_CONFIRMATION"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class JobType(StrEnum):
    DATA_SYNC = "DATA_SYNC"
    QUALITY_CHECK = "QUALITY_CHECK"
    DAILY_SIGNAL = "DAILY_SIGNAL"
    PAPER_SNAPSHOT = "PAPER_SNAPSHOT"
    FULL_DAILY_PIPELINE = "FULL_DAILY_PIPELINE"


class SortOrder(StrEnum):
    ASC = "ASC"
    DESC = "DESC"
