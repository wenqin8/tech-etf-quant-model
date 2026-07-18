"""Application service for safe exchange-calendar date arithmetic."""

from __future__ import annotations

from datetime import date

from etf_quant_lab.contracts import DomainError, ErrorCode
from etf_quant_lab.contracts.enums import Exchange
from etf_quant_lab.contracts.universe import TradingDayRangeRequest
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.domain.repositories import TradingCalendarRepository


class TradingCalendarService:
    """Query trading days without falling back to natural-day arithmetic."""

    def __init__(self, repository: TradingCalendarRepository) -> None:
        self._repository = repository

    def get_day(self, exchange: Exchange, cal_date: date) -> TradingCalendarDay:
        """Return a stored calendar date or fail explicitly when data is missing."""

        day = self._repository.get_day(exchange, cal_date)
        if day is None:
            raise self._missing_calendar_error(exchange, cal_date)
        return day

    def is_trading_day(self, exchange: Exchange, cal_date: date) -> bool:
        """Return whether a known calendar date is open."""

        return self.get_day(exchange, cal_date).is_open

    def list_days(
        self,
        request: TradingDayRangeRequest,
    ) -> tuple[TradingCalendarDay, ...]:
        """Return known calendar rows in a bounded inclusive range."""

        days = self._repository.list_days(
            request.exchange,
            request.start_date,
            request.end_date,
        )
        if request.open_only:
            return tuple(day for day in days if day.is_open)
        return days

    def next_trading_day(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool = False,
    ) -> date:
        """Return the next stored open date, optionally including the reference date."""

        next_date = self._repository.next_open_date(
            exchange,
            reference_date,
            inclusive=inclusive,
        )
        if next_date is None:
            raise self._missing_direction_error(exchange, reference_date, "next")
        return next_date

    def previous_trading_day(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool = False,
    ) -> date:
        """Return the previous stored open date, optionally including the reference date."""

        previous_date = self._repository.previous_open_date(
            exchange,
            reference_date,
            inclusive=inclusive,
        )
        if previous_date is None:
            raise self._missing_direction_error(exchange, reference_date, "previous")
        return previous_date

    def upsert_days(self, days: tuple[TradingCalendarDay, ...]) -> None:
        """Validate a batch for duplicate keys and pass it to the repository."""

        keys = [(day.exchange, day.cal_date) for day in days]
        if len(keys) != len(set(keys)):
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "交易日历批次包含重复日期",
                details={"row_count": len(days)},
            )
        if days:
            self._repository.upsert_many(days)

    @staticmethod
    def _missing_calendar_error(exchange: Exchange, cal_date: date) -> DomainError:
        return DomainError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "交易日历日期不存在",
            details={"exchange": exchange.value, "cal_date": cal_date.isoformat()},
        )

    @staticmethod
    def _missing_direction_error(
        exchange: Exchange,
        reference_date: date,
        direction: str,
    ) -> DomainError:
        return DomainError(
            ErrorCode.RESOURCE_NOT_FOUND,
            "交易日历范围不足, 无法定位交易日",
            details={
                "exchange": exchange.value,
                "reference_date": reference_date.isoformat(),
                "direction": direction,
            },
        )
