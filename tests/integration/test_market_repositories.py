"""Integration tests for DuckDB instrument and trading-calendar repositories."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.contracts.enums import (
    DataSource,
    Exchange,
    InstrumentStatus,
    InstrumentType,
)
from etf_quant_lab.domain.market import EtfInstrument, TradingCalendarDay
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.repositories import (
    DuckDBInstrumentRepository,
    DuckDBTradingCalendarRepository,
)

UPDATED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5ND1"


def _database(tmp_path: Path) -> DuckDBDatabase:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    return database


def _instrument(symbol: str, *, category: str = "BROAD_BASED") -> EtfInstrument:
    return EtfInstrument(
        symbol=symbol,
        name="沪深300ETF",
        exchange=Exchange.SSE if symbol.endswith(".SH") else Exchange.SZSE,
        instrument_type=InstrumentType.ETF,
        status=InstrumentStatus.ACTIVE,
        category=category,
        benchmark_symbol="000300.SH",
        lot_size=100,
        price_tick=Decimal("0.001"),
        list_date=date(2012, 5, 28),
        enabled=True,
        metadata={"provider_note": "primary"},
    )


def test_instrument_repository_upserts_and_reads_back(tmp_path: Path) -> None:
    repository = DuckDBInstrumentRepository(_database(tmp_path))
    repository.upsert_many((_instrument("510300.SH"), _instrument("159915.SZ")))

    stored = repository.get("510300.SH")
    assert stored is not None
    assert stored.name == "沪深300ETF"
    assert stored.metadata["provider_note"] == "primary"
    assert stored.price_tick == Decimal("0.001")
    assert tuple(item.symbol for item in repository.list_all()) == ("159915.SZ", "510300.SH")


def test_instrument_repository_upsert_updates_existing_row(tmp_path: Path) -> None:
    repository = DuckDBInstrumentRepository(_database(tmp_path))
    repository.upsert_many((_instrument("510300.SH", category="BROAD_BASED"),))

    repository.upsert_many((_instrument("510300.SH", category="STYLE"),))

    stored = repository.get("510300.SH")
    assert stored is not None
    assert stored.category == "STYLE"
    assert len(repository.list_all()) == 1


def test_calendar_repository_round_trips_and_resolves_open_dates(tmp_path: Path) -> None:
    repository = DuckDBTradingCalendarRepository(_database(tmp_path))
    days = (
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 10),
            is_open=True,
            previous_open_date=date(2026, 7, 9),
            next_open_date=date(2026, 7, 13),
            source=DataSource.TUSHARE,
            batch_id=BATCH_ID,
            updated_at=UPDATED_AT,
        ),
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 11),
            is_open=False,
            previous_open_date=date(2026, 7, 10),
            next_open_date=date(2026, 7, 13),
            source=DataSource.TUSHARE,
            batch_id=BATCH_ID,
            updated_at=UPDATED_AT,
        ),
        TradingCalendarDay(
            exchange=Exchange.SSE,
            cal_date=date(2026, 7, 13),
            is_open=True,
            previous_open_date=date(2026, 7, 10),
            next_open_date=None,
            source=DataSource.TUSHARE,
            batch_id=BATCH_ID,
            updated_at=UPDATED_AT,
        ),
    )
    repository.upsert_many(days)

    saturday = repository.get_day(Exchange.SSE, date(2026, 7, 11))
    assert saturday is not None
    assert saturday.is_open is False

    listed = repository.list_days(Exchange.SSE, date(2026, 7, 10), date(2026, 7, 13))
    assert [day.cal_date for day in listed] == [
        date(2026, 7, 10),
        date(2026, 7, 11),
        date(2026, 7, 13),
    ]

    assert repository.next_open_date(Exchange.SSE, date(2026, 7, 11), inclusive=False) == date(
        2026, 7, 13
    )
    assert repository.previous_open_date(Exchange.SSE, date(2026, 7, 12), inclusive=False) == date(
        2026, 7, 10
    )
    assert repository.next_open_date(Exchange.SSE, date(2026, 7, 13), inclusive=True) == date(
        2026, 7, 13
    )


def test_calendar_repository_returns_none_beyond_known_range(tmp_path: Path) -> None:
    repository = DuckDBTradingCalendarRepository(_database(tmp_path))
    repository.upsert_many(
        (
            TradingCalendarDay(
                exchange=Exchange.SSE,
                cal_date=date(2026, 7, 10),
                is_open=True,
                previous_open_date=None,
                next_open_date=None,
                source=DataSource.TUSHARE,
                batch_id=BATCH_ID,
                updated_at=UPDATED_AT,
            ),
        )
    )

    assert repository.get_day(Exchange.SSE, date(2026, 7, 20)) is None
    assert repository.next_open_date(Exchange.SSE, date(2026, 7, 11), inclusive=True) is None
