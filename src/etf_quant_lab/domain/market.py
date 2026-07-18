"""Framework-independent ETF instrument and trading-calendar models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from types import MappingProxyType

from etf_quant_lab.contracts.enums import (
    DataSource,
    Exchange,
    InstrumentStatus,
    InstrumentType,
)

_SYMBOL_PATTERN = re.compile(r"^(?P<code>\d{6})\.(?P<suffix>SH|SZ)$")
_CATEGORY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")
_EXCHANGE_SUFFIX = {Exchange.SSE: "SH", Exchange.SZSE: "SZ"}


@dataclass(frozen=True, slots=True)
class EtfInstrument:
    """A configured A-share exchange-traded fund instrument."""

    symbol: str
    name: str
    exchange: Exchange
    instrument_type: InstrumentType = InstrumentType.ETF
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    list_date: date | None = None
    delist_date: date | None = None
    lot_size: int = 100
    price_tick: Decimal = Decimal("0.001")
    category: str = "OTHER"
    benchmark_symbol: str | None = None
    enabled: bool = True
    metadata_version: str = "1"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        match = _SYMBOL_PATTERN.fullmatch(self.symbol)
        if match is None:
            raise ValueError(f"invalid ETF symbol: {self.symbol}")
        if match.group("suffix") != _EXCHANGE_SUFFIX[self.exchange]:
            raise ValueError(
                f"symbol suffix does not match exchange: {self.symbol} {self.exchange.value}"
            )
        if not self.name.strip():
            raise ValueError("instrument name must not be blank")
        if self.instrument_type != InstrumentType.ETF:
            raise ValueError("the first release supports ETF instruments only")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be greater than zero")
        if self.price_tick <= 0:
            raise ValueError("price_tick must be greater than zero")
        if _CATEGORY_PATTERN.fullmatch(self.category) is None:
            raise ValueError(f"invalid instrument category: {self.category}")
        if (
            self.delist_date is not None
            and self.list_date is not None
            and self.delist_date < self.list_date
        ):
            raise ValueError("delist_date must not be earlier than list_date")
        if not self.metadata_version.strip():
            raise ValueError("metadata_version must not be blank")
        if self.enabled and self.status == InstrumentStatus.DISABLED:
            raise ValueError("a DISABLED instrument cannot be enabled")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def with_enabled(self, enabled: bool) -> EtfInstrument:
        """Return a copy with the user-enabled state changed safely."""

        if enabled and self.status == InstrumentStatus.DELISTED:
            raise ValueError(f"delisted instrument cannot be enabled: {self.symbol}")
        next_status: InstrumentStatus
        if enabled:
            next_status = (
                InstrumentStatus.ACTIVE
                if self.status == InstrumentStatus.DISABLED
                else self.status
            )
        else:
            next_status = (
                self.status
                if self.status == InstrumentStatus.DELISTED
                else InstrumentStatus.DISABLED
            )
        return replace(self, enabled=enabled, status=next_status)


@dataclass(frozen=True, slots=True)
class TradingCalendarDay:
    """One natural date in an exchange trading calendar."""

    exchange: Exchange
    cal_date: date
    is_open: bool
    previous_open_date: date | None
    next_open_date: date | None
    source: DataSource
    batch_id: str
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.previous_open_date is not None and self.previous_open_date >= self.cal_date:
            raise ValueError("previous_open_date must be earlier than cal_date")
        if self.next_open_date is not None and self.next_open_date <= self.cal_date:
            raise ValueError("next_open_date must be later than cal_date")
        if not self.batch_id.strip():
            raise ValueError("batch_id must not be blank")
        if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
