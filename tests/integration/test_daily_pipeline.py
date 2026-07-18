"""Integration tests for incremental sync, NAV snapshots and the daily pipeline."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.contracts.data import DailyBarsQuery, RawProviderBatch, TradeCalendarQuery
from etf_quant_lab.contracts.enums import DataBatchStatus, DataSource, Exchange, OrderSide
from etf_quant_lab.contracts.paper import PaperFillSource
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.data_sync import DataSyncService
from etf_quant_lab.services.paper import PaperAccountService
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.quality import QualityReportRepository
from etf_quant_lab.storage.repositories import (
    DataBatchRepository,
    DuckDBTradingCalendarRepository,
)

FETCHED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
END = date(2026, 7, 15)


class _WindowProvider:
    """Emits one bar per requested day in the query window, close = 4.00 + i*0.01."""

    source = DataSource.TUSHARE

    def fetch_daily_bars(self, query: DailyBarsQuery) -> RawProviderBatch:
        records = []
        for symbol in query.symbols:
            day = query.start_date
            while day <= query.end_date:
                offset = (day - date(2026, 6, 1)).days
                close = f"{4.00 + offset * 0.01:.2f}"
                records.append(
                    {
                        "symbol": symbol,
                        "trade_date": day.strftime("%Y%m%d"),
                        "open": close,
                        "high": close,
                        "low": close,
                        "close": close,
                        "pre_close": close,
                        "volume": "1000",
                        "amount": "4000",
                    }
                )
                day += timedelta(days=1)
        return RawProviderBatch(
            batch_id=UlidGenerator().new(),
            source=DataSource.TUSHARE,
            dataset="fund_daily",
            records=tuple(records),
            fetched_at=FETCHED_AT,
        )

    def fetch_trade_calendar(self, query: TradeCalendarQuery) -> RawProviderBatch:
        raise NotImplementedError


def _environment(tmp_path: Path) -> tuple[DataSyncService, DataBatchRepository]:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    store = ParquetStore(tmp_path / "data")
    batches = DataBatchRepository(database, store)
    calendar = DuckDBTradingCalendarRepository(database)
    days = []
    day = date(2026, 6, 1)
    while day <= END:
        days.append(
            TradingCalendarDay(
                exchange=Exchange.SSE,
                cal_date=day,
                is_open=True,
                previous_open_date=None,
                next_open_date=None,
                source=DataSource.TUSHARE,
                batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NY9",
                updated_at=FETCHED_AT,
            )
        )
        day += timedelta(days=1)
    calendar.upsert_many(tuple(days))
    quality = QualityService(
        batch_repository=batches,
        calendar_repository=calendar,
        report_repository=QualityReportRepository(database, UlidGenerator()),
        id_generator=UlidGenerator(),
    )
    service = DataSyncService(
        provider=_WindowProvider(),
        parquet_store=store,
        batch_repository=batches,
        quality_service=quality,
        id_generator=UlidGenerator(),
        clock=lambda: FETCHED_AT,
    )
    return service, batches


def test_incremental_topup_keeps_full_history(tmp_path: Path) -> None:
    service, batches = _environment(tmp_path)

    full, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 10),
    )
    topup, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 7, 5),
        end_date=END,
        incremental=True,
    )

    # The full-history batch must survive an incremental top-up.
    refreshed_full = batches.get(full.batch_id)
    assert refreshed_full is not None
    assert refreshed_full.status is DataBatchStatus.ACTIVE
    assert topup.status is DataBatchStatus.ACTIVE
    bars = batches.query_daily_bars(symbols=("510300.SH",))
    dates = [bar.trade_date for bar in bars]
    # Full range is intact: history start through the new end, no gaps or dupes.
    assert dates[0] == date(2026, 6, 1)
    assert dates[-1] == END
    assert len(dates) == len(set(dates))


def test_second_incremental_supersedes_first_incremental_only(tmp_path: Path) -> None:
    service, batches = _environment(tmp_path)
    full, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 10),
    )
    first_topup, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 7, 5),
        end_date=date(2026, 7, 14),
        incremental=True,
    )

    second_topup, _ = service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 7, 6),
        end_date=END,
        incremental=True,
    )

    assert batches.get(full.batch_id).status is DataBatchStatus.ACTIVE  # type: ignore[union-attr]
    assert batches.get(first_topup.batch_id).status is DataBatchStatus.SUPERSEDED  # type: ignore[union-attr]
    assert second_topup.status is DataBatchStatus.ACTIVE


def test_active_adjustment_follows_standing_history(tmp_path: Path) -> None:
    from etf_quant_lab.contracts.enums import PriceAdjustment
    from etf_quant_lab.services.data_sync import active_adjustment

    service, batches = _environment(tmp_path)
    # Empty database prefers QFQ going forward.
    assert active_adjustment(batches) is PriceAdjustment.QFQ

    service.sync_daily_bars(
        symbols=("510300.SH",),
        start_date=date(2026, 6, 1),
        end_date=date(2026, 7, 10),
        adjustment=PriceAdjustment.RAW,
    )
    # Standing RAW history keeps top-ups on RAW.
    assert active_adjustment(batches) is PriceAdjustment.RAW


def test_nav_snapshot_records_and_is_idempotent(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    paper = PaperAccountService(database, UlidGenerator(), clock=lambda: FETCHED_AT)
    account = paper.create_account(name="PAPER_MAIN", initial_cash=Decimal("10000"))
    order = paper.propose_order(
        account_id=account.account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=1000,
    )
    paper.record_fill(
        order_id=order.order_id,
        trade_date=date(2026, 7, 14),
        price=Decimal("4.00"),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )

    equity = paper.record_nav_snapshot(
        account.account_id,
        trade_date=date(2026, 7, 15),
        close_prices={"510300.SH": Decimal("4.20")},
    )
    # cash 10000-4005=5995; positions 1000*4.20=4200 => 10195.
    assert equity == Decimal("10195.0000")

    # Re-recording the same date replaces, not duplicates.
    equity_again = paper.record_nav_snapshot(
        account.account_id,
        trade_date=date(2026, 7, 15),
        close_prices={"510300.SH": Decimal("4.30")},
    )
    assert equity_again == Decimal("10295.0000")
    navs = paper.list_nav_snapshots(account.account_id)
    assert len(navs) == 1
    assert navs[0][3] == Decimal("10295.0000")
