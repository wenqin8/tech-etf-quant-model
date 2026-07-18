"""Deterministic in-memory repositories for tests and pre-database bootstrap."""

from __future__ import annotations

from datetime import date
from threading import RLock

from etf_quant_lab.contracts.enums import Exchange
from etf_quant_lab.domain.market import EtfInstrument, TradingCalendarDay


class InMemoryInstrumentRepository:
    """Thread-safe process-local implementation of the instrument repository."""

    def __init__(self, instruments: tuple[EtfInstrument, ...] = ()) -> None:
        self._lock = RLock()
        self._instruments = {instrument.symbol: instrument for instrument in instruments}

    def get(self, symbol: str) -> EtfInstrument | None:
        with self._lock:
            return self._instruments.get(symbol)

    def list_all(self) -> tuple[EtfInstrument, ...]:
        with self._lock:
            return tuple(sorted(self._instruments.values(), key=lambda item: item.symbol))

    def upsert_many(self, instruments: tuple[EtfInstrument, ...]) -> None:
        with self._lock:
            self._instruments.update({instrument.symbol: instrument for instrument in instruments})


class InMemoryTradingCalendarRepository:
    """Thread-safe process-local implementation of the calendar repository."""

    def __init__(self, days: tuple[TradingCalendarDay, ...] = ()) -> None:
        self._lock = RLock()
        self._days = {(day.exchange, day.cal_date): day for day in days}

    def get_day(self, exchange: Exchange, cal_date: date) -> TradingCalendarDay | None:
        with self._lock:
            return self._days.get((exchange, cal_date))

    def list_days(
        self,
        exchange: Exchange,
        start_date: date,
        end_date: date,
    ) -> tuple[TradingCalendarDay, ...]:
        with self._lock:
            return tuple(
                sorted(
                    (
                        day
                        for (day_exchange, cal_date), day in self._days.items()
                        if day_exchange is exchange and start_date <= cal_date <= end_date
                    ),
                    key=lambda day: day.cal_date,
                )
            )

    def next_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        with self._lock:
            candidates = (
                cal_date
                for (day_exchange, cal_date), day in self._days.items()
                if day_exchange is exchange
                and day.is_open
                and (cal_date >= reference_date if inclusive else cal_date > reference_date)
            )
            return min(candidates, default=None)

    def previous_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        with self._lock:
            candidates = (
                cal_date
                for (day_exchange, cal_date), day in self._days.items()
                if day_exchange is exchange
                and day.is_open
                and (cal_date <= reference_date if inclusive else cal_date < reference_date)
            )
            return max(candidates, default=None)

    def upsert_many(self, days: tuple[TradingCalendarDay, ...]) -> None:
        with self._lock:
            self._days.update({(day.exchange, day.cal_date): day for day in days})
