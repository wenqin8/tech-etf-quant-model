"""Stable data-provider and market-data contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    PriceAdjustment,
)


@dataclass(frozen=True, slots=True)
class DailyBarsQuery:
    """Provider-neutral request for ETF daily bars."""

    symbols: tuple[str, ...]
    start_date: date
    end_date: date
    adjustment: PriceAdjustment = PriceAdjustment.RAW

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("symbols must not be empty")
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")
        normalized = tuple(symbol.strip().upper() for symbol in self.symbols)
        if any(not symbol for symbol in normalized):
            raise ValueError("symbols must not contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("symbols must not contain duplicates")
        object.__setattr__(self, "symbols", normalized)


@dataclass(frozen=True, slots=True)
class TradeCalendarQuery:
    """Provider-neutral request for an exchange calendar range."""

    exchange: Exchange
    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.start_date > self.end_date:
            raise ValueError("start_date must not be after end_date")


@dataclass(frozen=True, slots=True)
class RawProviderBatch:
    """Immutable description of one external response before canonicalization."""

    batch_id: str
    source: DataSource
    dataset: str
    records: tuple[Mapping[str, object], ...]
    fetched_at: datetime
    request_metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")
        if not self.dataset.strip():
            raise ValueError("dataset must not be blank")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class DailyBar:
    """Canonical daily OHLCV record used by research and storage."""

    symbol: str
    trade_date: date
    exchange: Exchange
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    amount: Decimal
    source: DataSource
    batch_id: str
    ingested_at: datetime
    pre_close: Decimal | None = None
    adj_factor: Decimal | None = None
    is_suspended: bool = False

    def __post_init__(self) -> None:
        normalized_symbol = self.symbol.strip().upper()
        if not normalized_symbol:
            raise ValueError("symbol must not be blank")
        object.__setattr__(self, "symbol", normalized_symbol)
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")
        if self.ingested_at.tzinfo is None:
            raise ValueError("ingested_at must be timezone-aware")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise ValueError("OHLC prices must be positive")
        if self.high < max(self.open, self.low, self.close):
            raise ValueError("high must not be lower than open, low or close")
        if self.low > min(self.open, self.high, self.close):
            raise ValueError("low must not be higher than open, high or close")
        if self.volume < 0 or self.amount < 0:
            raise ValueError("volume and amount must not be negative")
        if self.pre_close is not None and self.pre_close <= 0:
            raise ValueError("pre_close must be positive when provided")
        if self.adj_factor is not None and self.adj_factor <= 0:
            raise ValueError("adj_factor must be positive when provided")


@dataclass(frozen=True, slots=True)
class DataBatch:
    """Metadata for a persisted raw or canonical data batch."""

    batch_id: str
    provider: DataSource
    dataset: str
    status: DataBatchStatus
    fetched_at: datetime
    schema_version: str
    row_count: int = 0
    file_count: int = 0
    checksum: str | None = None
    parent_batch_id: str | None = None
    error_summary: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")
        if self.parent_batch_id is not None and len(self.parent_batch_id) != 26:
            raise ValueError("parent_batch_id must contain exactly 26 characters")
        if self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        if self.row_count < 0 or self.file_count < 0:
            raise ValueError("row_count and file_count must not be negative")
        if not self.schema_version.strip():
            raise ValueError("schema_version must not be blank")

