"""Application service for configured ETF universe management."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Self
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from etf_quant_lab.contracts import DomainError, ErrorCode, Page
from etf_quant_lab.contracts.enums import (
    Exchange,
    InstrumentStatus,
    InstrumentType,
    SortOrder,
)
from etf_quant_lab.contracts.universe import (
    ListInstrumentsRequest,
    ReloadUniverseRequest,
    ReloadUniverseResult,
)
from etf_quant_lab.domain.market import EtfInstrument
from etf_quant_lab.domain.repositories import InstrumentRepository

_MARKET_TIMEZONE = ZoneInfo("Asia/Shanghai")


class _InstrumentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    symbol: str
    name: str
    exchange: Exchange
    instrument_type: InstrumentType = InstrumentType.ETF
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    list_date: date | None = None
    delist_date: date | None = None
    lot_size: int = Field(default=100, gt=0)
    price_tick: Decimal = Field(default=Decimal("0.001"), gt=0)
    category: str
    benchmark_symbol: str | None = None
    enabled: bool = True
    metadata: dict[str, object] = Field(default_factory=dict)

    def to_domain(self, metadata_version: str) -> EtfInstrument:
        status = self.status
        if not self.enabled and status != InstrumentStatus.DELISTED:
            status = InstrumentStatus.DISABLED
        try:
            return EtfInstrument(
                symbol=self.symbol,
                name=self.name,
                exchange=self.exchange,
                instrument_type=self.instrument_type,
                status=status,
                list_date=self.list_date,
                delist_date=self.delist_date,
                lot_size=self.lot_size,
                price_tick=self.price_tick,
                category=self.category,
                benchmark_symbol=self.benchmark_symbol,
                enabled=self.enabled,
                metadata_version=metadata_version,
                metadata=self.metadata,
            )
        except ValueError as exc:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                "ETF 标的配置不合法",
                details={"symbol": self.symbol, "reason": str(exc)},
            ) from exc


class _UniverseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    metadata_version: str = Field(min_length=1)
    instruments: tuple[_InstrumentConfig, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_unique_symbols(self) -> Self:
        symbols = [instrument.symbol for instrument in self.instruments]
        duplicate_symbols = sorted(
            symbol for symbol in set(symbols) if symbols.count(symbol) > 1
        )
        if duplicate_symbols:
            raise ValueError(f"duplicate ETF symbols: {', '.join(duplicate_symbols)}")
        return self


class UniverseService:
    """Manage ETF metadata without coupling callers to a storage engine."""

    def __init__(
        self,
        repository: InstrumentRepository,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._clock = clock or (lambda: datetime.now(_MARKET_TIMEZONE))

    def list_instruments(
        self,
        request: ListInstrumentsRequest,
    ) -> Page[EtfInstrument]:
        """Return a deterministic filtered page of configured instruments."""

        instruments = list(self._repository.list_all())
        if request.enabled_only:
            instruments = [instrument for instrument in instruments if instrument.enabled]
        if request.categories:
            categories = frozenset(request.categories)
            instruments = [
                instrument for instrument in instruments if instrument.category in categories
            ]
        if request.exchanges:
            exchanges = frozenset(request.exchanges)
            instruments = [
                instrument for instrument in instruments if instrument.exchange in exchanges
            ]
        if request.keyword is not None and request.keyword.strip():
            keyword = request.keyword.strip().casefold()
            instruments = [
                instrument
                for instrument in instruments
                if keyword in instrument.symbol.casefold() or keyword in instrument.name.casefold()
            ]

        def sort_key(instrument: EtfInstrument) -> str:
            if request.sort_by == "name":
                return instrument.name
            if request.sort_by == "category":
                return instrument.category
            if request.sort_by == "exchange":
                return instrument.exchange.value
            return instrument.symbol

        instruments.sort(key=sort_key, reverse=request.sort_order == SortOrder.DESC)

        total = len(instruments)
        offset = (request.page - 1) * request.page_size
        items = tuple(instruments[offset : offset + request.page_size])
        total_pages = (total + request.page_size - 1) // request.page_size if total else 0
        return Page(
            items=items,
            total=total,
            page=request.page,
            page_size=request.page_size,
            total_pages=total_pages,
        )

    def reload_from_config(self, request: ReloadUniverseRequest) -> ReloadUniverseResult:
        """Validate and atomically apply a YAML universe definition.

        Instruments removed from YAML are disabled instead of deleted so historical
        research remains reproducible.
        """

        config_path = request.config_path.expanduser().resolve()
        raw_bytes, config = self._load_config(config_path)
        configured = {
            item.symbol: item.to_domain(config.metadata_version)
            for item in config.instruments
        }
        existing = {item.symbol: item for item in self._repository.list_all()}

        added: list[str] = []
        updated: list[str] = []
        disabled: list[str] = []
        unchanged_count = 0
        pending: list[EtfInstrument] = []

        for symbol, instrument in configured.items():
            current = existing.get(symbol)
            if current is None:
                added.append(symbol)
                pending.append(instrument)
            elif current != instrument:
                updated.append(symbol)
                pending.append(instrument)
            else:
                unchanged_count += 1

        for symbol, current in existing.items():
            if symbol in configured:
                continue
            if current.enabled:
                disabled.append(symbol)
                pending.append(current.with_enabled(False))
            else:
                unchanged_count += 1

        loaded_at = self._clock()
        if loaded_at.tzinfo is None or loaded_at.utcoffset() is None:
            raise ValueError("UniverseService clock must return a timezone-aware datetime")
        if pending and not request.dry_run:
            self._repository.upsert_many(tuple(pending))

        return ReloadUniverseResult(
            config_version=f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}",
            added=tuple(sorted(added)),
            updated=tuple(sorted(updated)),
            disabled=tuple(sorted(disabled)),
            unchanged_count=unchanged_count,
            loaded_at=loaded_at,
        )

    def set_enabled(self, symbol: str, enabled: bool, reason: str) -> EtfInstrument:
        """Enable or disable one configured instrument idempotently."""

        if not reason.strip():
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "启用或停用 ETF 时必须填写原因",
                details={"symbol": symbol},
            )
        instrument = self._repository.get(symbol)
        if instrument is None:
            raise DomainError(
                ErrorCode.RESOURCE_NOT_FOUND,
                "ETF 标的不存在",
                details={"symbol": symbol},
            )
        if instrument.enabled is enabled:
            return instrument
        try:
            updated = instrument.with_enabled(enabled)
        except ValueError as exc:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "ETF 标的状态不允许该操作",
                details={"symbol": symbol, "reason": str(exc)},
            ) from exc
        self._repository.upsert_many((updated,))
        return updated

    @staticmethod
    def _load_config(config_path: Path) -> tuple[bytes, _UniverseConfig]:
        try:
            raw_bytes = config_path.read_bytes()
        except OSError as exc:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                "无法读取 ETF 标的池配置",
                details={"config_path": str(config_path), "reason": str(exc)},
            ) from exc
        try:
            payload = yaml.safe_load(raw_bytes.decode("utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("configuration root must be a mapping")
            config = _UniverseConfig.model_validate(payload)
        except (UnicodeDecodeError, yaml.YAMLError, ValidationError, ValueError) as exc:
            raise DomainError(
                ErrorCode.CONFIG_INVALID,
                "ETF 标的池配置校验失败",
                details={"config_path": str(config_path), "reason": str(exc)},
            ) from exc
        return raw_bytes, config
