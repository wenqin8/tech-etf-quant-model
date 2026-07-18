from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from etf_quant_lab.contracts.data import DailyBar, DailyBarsQuery, RawProviderBatch
from etf_quant_lab.contracts.enums import DataSource, Exchange, PriceAdjustment


def test_daily_bars_query_normalizes_symbols_and_rejects_duplicates() -> None:
    query = DailyBarsQuery(
        symbols=("510300.sh", "159915.sz"),
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        adjustment=PriceAdjustment.RAW,
    )

    assert query.symbols == ("510300.SH", "159915.SZ")

    with pytest.raises(ValueError, match="duplicates"):
        DailyBarsQuery(
            symbols=("510300.SH", "510300.sh"),
            start_date=date(2026, 1, 1),
            end_date=date(2026, 1, 31),
        )


def test_raw_provider_batch_requires_timezone_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        RawProviderBatch(
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5N12",
            source=DataSource.TUSHARE,
            dataset="daily_bars",
            records=(),
            fetched_at=datetime(2026, 1, 1),
        )


def test_daily_bar_enforces_ohlc_relationships() -> None:
    with pytest.raises(ValueError, match="high"):
        DailyBar(
            symbol="510300.SH",
            trade_date=date(2026, 1, 5),
            exchange=Exchange.SSE,
            open=Decimal("4.00"),
            high=Decimal("3.90"),
            low=Decimal("3.80"),
            close=Decimal("3.95"),
            volume=Decimal("1000"),
            amount=Decimal("3950"),
            source=DataSource.TUSHARE,
            batch_id="01K0D7F7P6XQ4M2Z8H9B3C5N12",
            ingested_at=datetime(2026, 1, 5, tzinfo=UTC),
        )

