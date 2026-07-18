"""Repository protocols used by the market-domain application services."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from etf_quant_lab.contracts.enums import Exchange
from etf_quant_lab.domain.market import EtfInstrument, TradingCalendarDay


class InstrumentRepository(Protocol):
    """Persistence boundary for ETF instruments."""

    def get(self, symbol: str) -> EtfInstrument | None:
        """Return one instrument or ``None`` when it does not exist."""

    def list_all(self) -> tuple[EtfInstrument, ...]:
        """Return all instruments, including disabled historical entries."""

    def upsert_many(self, instruments: tuple[EtfInstrument, ...]) -> None:
        """Insert or replace instruments atomically at the repository boundary."""


class TradingCalendarRepository(Protocol):
    """Persistence boundary for exchange trading calendars."""

    def get_day(self, exchange: Exchange, cal_date: date) -> TradingCalendarDay | None:
        """Return a calendar row for one natural date when present."""

    def list_days(
        self,
        exchange: Exchange,
        start_date: date,
        end_date: date,
    ) -> tuple[TradingCalendarDay, ...]:
        """Return rows ordered by calendar date, inclusive of both bounds."""

    def next_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        """Find the next open date relative to ``reference_date``."""

    def previous_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        """Find the previous open date relative to ``reference_date``."""

    def upsert_many(self, days: tuple[TradingCalendarDay, ...]) -> None:
        """Insert or replace calendar rows atomically at the repository boundary."""
