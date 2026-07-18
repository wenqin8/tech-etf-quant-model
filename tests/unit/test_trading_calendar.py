"""Unit tests for trading-calendar date semantics."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from etf_quant_lab.contracts import DomainError, ErrorCode
from etf_quant_lab.contracts.enums import DataSource, Exchange
from etf_quant_lab.contracts.universe import TradingDayRangeRequest
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.services.calendar import TradingCalendarService
from etf_quant_lab.storage.memory import InMemoryTradingCalendarRepository

UPDATED_AT = datetime(2026, 7, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _day(cal_date: date, *, is_open: bool) -> TradingCalendarDay:
    return TradingCalendarDay(
        exchange=Exchange.SSE,
        cal_date=cal_date,
        is_open=is_open,
        previous_open_date=None,
        next_open_date=None,
        source=DataSource.CSV,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NA2",
        updated_at=UPDATED_AT,
    )


@pytest.fixture
def calendar_service() -> TradingCalendarService:
    days = (
        _day(date(2026, 7, 10), is_open=True),
        _day(date(2026, 7, 11), is_open=False),
        _day(date(2026, 7, 12), is_open=False),
        _day(date(2026, 7, 13), is_open=True),
        _day(date(2026, 7, 14), is_open=False),
        _day(date(2026, 7, 15), is_open=True),
    )
    return TradingCalendarService(InMemoryTradingCalendarRepository(days))


def test_next_trading_day_skips_weekend(calendar_service: TradingCalendarService) -> None:
    assert calendar_service.next_trading_day(
        Exchange.SSE, date(2026, 7, 10)
    ) == date(2026, 7, 13)


def test_next_trading_day_can_include_reference_date(
    calendar_service: TradingCalendarService,
) -> None:
    assert calendar_service.next_trading_day(
        Exchange.SSE,
        date(2026, 7, 13),
        inclusive=True,
    ) == date(2026, 7, 13)


def test_previous_trading_day_skips_closed_date(
    calendar_service: TradingCalendarService,
) -> None:
    assert calendar_service.previous_trading_day(
        Exchange.SSE, date(2026, 7, 15)
    ) == date(2026, 7, 13)


def test_is_trading_day_fails_when_calendar_row_is_missing(
    calendar_service: TradingCalendarService,
) -> None:
    with pytest.raises(DomainError) as exc_info:
        calendar_service.is_trading_day(Exchange.SSE, date(2026, 7, 16))

    assert exc_info.value.code == ErrorCode.RESOURCE_NOT_FOUND.value


def test_list_days_can_return_only_open_dates(
    calendar_service: TradingCalendarService,
) -> None:
    days = calendar_service.list_days(
        TradingDayRangeRequest(
            exchange=Exchange.SSE,
            start_date=date(2026, 7, 10),
            end_date=date(2026, 7, 15),
            open_only=True,
        )
    )

    assert tuple(day.cal_date for day in days) == (
        date(2026, 7, 10),
        date(2026, 7, 13),
        date(2026, 7, 15),
    )


def test_upsert_rejects_duplicate_exchange_dates() -> None:
    service = TradingCalendarService(InMemoryTradingCalendarRepository())
    duplicate = _day(date(2026, 7, 13), is_open=True)

    with pytest.raises(DomainError) as exc_info:
        service.upsert_days((duplicate, duplicate))

    assert exc_info.value.code == ErrorCode.VALIDATION_ERROR.value
