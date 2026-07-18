"""Stable request and response contracts for the ETF universe."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from etf_quant_lab.contracts.enums import Exchange, SortOrder


@dataclass(frozen=True, slots=True)
class ListInstrumentsRequest:
    """Filtering, sorting, and pagination options for universe queries."""

    enabled_only: bool = True
    categories: tuple[str, ...] = ()
    exchanges: tuple[Exchange, ...] = ()
    keyword: str | None = None
    page: int = 1
    page_size: int = 50
    sort_by: str = "symbol"
    sort_order: SortOrder = SortOrder.ASC

    def __post_init__(self) -> None:
        if self.page < 1:
            raise ValueError("page must be at least 1")
        if not 1 <= self.page_size <= 500:
            raise ValueError("page_size must be between 1 and 500")
        if self.sort_by not in {"symbol", "name", "category", "exchange"}:
            raise ValueError(f"unsupported instrument sort field: {self.sort_by}")


@dataclass(frozen=True, slots=True)
class ReloadUniverseRequest:
    """Request to load the ETF universe from a YAML configuration file."""

    config_path: Path = Path("config/universe.yaml")
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ReloadUniverseResult:
    """Summary of a universe configuration reload."""

    config_version: str
    added: tuple[str, ...]
    updated: tuple[str, ...]
    disabled: tuple[str, ...]
    unchanged_count: int
    loaded_at: datetime


@dataclass(frozen=True, slots=True)
class TradingDayRangeRequest:
    """Request a bounded section of one exchange's calendar."""

    exchange: Exchange
    start_date: date
    end_date: date
    open_only: bool = False

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be earlier than start_date")
