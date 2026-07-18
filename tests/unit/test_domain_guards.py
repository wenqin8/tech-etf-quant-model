"""Validation-branch tests raising core-domain coverage to the ≥90% target."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import (
    DataSource,
    Exchange,
    InstrumentStatus,
)
from etf_quant_lab.domain.market import EtfInstrument, TradingCalendarDay
from etf_quant_lab.domain.market_view import MarketDataView
from etf_quant_lab.domain.strategy import StrategyContext

UPDATED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)


def _instrument(**overrides: object) -> EtfInstrument:
    payload: dict[str, object] = {
        "symbol": "510300.SH",
        "name": "沪深300ETF",
        "exchange": Exchange.SSE,
    }
    payload.update(overrides)
    return EtfInstrument(**payload)  # type: ignore[arg-type]


def test_instrument_rejects_blank_name_and_bad_category() -> None:
    with pytest.raises(ValueError, match="name"):
        _instrument(name="   ")
    with pytest.raises(ValueError, match="category"):
        _instrument(category="lower_case")


def test_instrument_rejects_non_positive_tick_and_lot() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        _instrument(lot_size=0)
    with pytest.raises(ValueError, match="price_tick"):
        _instrument(price_tick=Decimal("0"))


def test_instrument_rejects_inverted_dates_and_blank_metadata_version() -> None:
    with pytest.raises(ValueError, match="delist_date"):
        _instrument(list_date=date(2020, 1, 2), delist_date=date(2020, 1, 1))
    with pytest.raises(ValueError, match="metadata_version"):
        _instrument(metadata_version=" ")


def test_instrument_rejects_enabled_disabled_conflict() -> None:
    with pytest.raises(ValueError, match="DISABLED"):
        _instrument(status=InstrumentStatus.DISABLED, enabled=True)


def test_instrument_enable_disable_transitions() -> None:
    delisted = _instrument(status=InstrumentStatus.DELISTED, enabled=False)
    with pytest.raises(ValueError, match="delisted"):
        delisted.with_enabled(True)
    # Disabling a DELISTED instrument keeps its DELISTED status.
    assert delisted.with_enabled(False).status is InstrumentStatus.DELISTED

    disabled = _instrument(status=InstrumentStatus.DISABLED, enabled=False)
    re_enabled = disabled.with_enabled(True)
    assert re_enabled.status is InstrumentStatus.ACTIVE
    assert re_enabled.enabled


def test_calendar_day_rejects_inconsistent_neighbors() -> None:
    with pytest.raises(ValueError, match="previous_open_date"):
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 10),
            is_open=True,
            previous_open_date=date(2026, 7, 10),
            next_open_date=None,
            source=DataSource.TUSHARE,
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NW1",
            updated_at=UPDATED_AT,
        )
    with pytest.raises(ValueError, match="next_open_date"):
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 10),
            is_open=True,
            previous_open_date=None,
            next_open_date=date(2026, 7, 10),
            source=DataSource.TUSHARE,
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NW1",
            updated_at=UPDATED_AT,
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 10),
            is_open=True,
            previous_open_date=None,
            next_open_date=None,
            source=DataSource.TUSHARE,
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NW1",
            updated_at=datetime(2026, 7, 13, 8, 0),  # naive
        )


def _bar(trade_date: date) -> DailyBar:
    return DailyBar(
        symbol="510300.SH",
        trade_date=trade_date,
        exchange=Exchange.SSE,
        open=Decimal("4.00"),
        high=Decimal("4.05"),
        low=Decimal("3.95"),
        close=Decimal("4.02"),
        volume=Decimal("1000"),
        amount=Decimal("4000"),
        source=DataSource.TUSHARE,
        batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NW2",
        ingested_at=UPDATED_AT,
    )


def test_strategy_context_guards() -> None:
    view = MarketDataView(as_of_date=date(2026, 7, 10), bars=(_bar(date(2026, 7, 10)),))

    with pytest.raises(ValueError, match="as_of_date"):
        StrategyContext(
            as_of_date=date(2026, 7, 11),  # mismatched with the view
            universe_symbols=("510300.SH",),
            market_data=view,
        )
    with pytest.raises(ValueError, match="negative"):
        StrategyContext(
            as_of_date=date(2026, 7, 10),
            universe_symbols=("510300.SH",),
            market_data=view,
            current_weights={"510300.SH": Decimal("-0.1")},
        )
    with pytest.raises(ValueError, match="cash_weight"):
        StrategyContext(
            as_of_date=date(2026, 7, 10),
            universe_symbols=("510300.SH",),
            market_data=view,
            cash_weight=Decimal("1.5"),
        )
