"""Bootstrap the research database with real market data.

Steps (idempotent, safe to re-run):

1. Load the ETF universe from ``config/universe.yaml`` into DuckDB.
2. Sync the SSE trading calendar (AKShare sina feed) into ``trading_calendar``.
3. Sync daily bars for every enabled ETF over the requested window
   (AKShare, publication-eligible), one batch per symbol so a single bad
   symbol cannot poison the rest.

Usage:

    uv run python scripts/bootstrap_market_data.py --years 3
"""

from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, timedelta

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.data import TradeCalendarQuery
from etf_quant_lab.contracts.enums import Exchange, PriceAdjustment
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.universe import ReloadUniverseRequest
from etf_quant_lab.data.providers.akshare import AkshareProvider
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.services.data_sync import DataSyncService


def _sync_calendar(context: object, provider: AkshareProvider, start: date, end: date) -> int:
    """Fetch the A-share calendar and upsert open days plus gap days."""

    raw = provider.fetch_trade_calendar(
        TradeCalendarQuery(exchange=Exchange.SSE, start_date=start, end_date=end)
    )
    open_dates: set[date] = set()
    for record in raw.records:
        value = record.get("trade_date")
        parsed = value if isinstance(value, date) else None
        if parsed is None and value is not None:
            try:
                parsed = date.fromisoformat(str(value)[:10])
            except ValueError:
                continue
        if parsed is not None and start <= parsed <= end:
            open_dates.add(parsed)

    now = datetime.now(UTC)
    days: list[TradingCalendarDay] = []
    cursor = start
    while cursor <= end:
        days.append(
            TradingCalendarDay(
                exchange=Exchange.SSE,
                cal_date=cursor,
                is_open=cursor in open_dates,
                previous_open_date=None,
                next_open_date=None,
                source=raw.source,
                batch_id=raw.batch_id,
                updated_at=now,
            )
        )
        cursor += timedelta(days=1)
    context.calendar.upsert_many(tuple(days))  # type: ignore[attr-defined]
    return len(open_dates)


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化真实行情数据库")
    parser.add_argument("--years", type=int, default=5, help="回溯年数")
    parser.add_argument("--pause", type=float, default=1.0, help="每标的之间的间隔秒数")
    parser.add_argument(
        "--adjust",
        choices=("qfq", "raw"),
        default="qfq",
        help="价格口径: qfq 前复权(推荐, 需东财端点可用) / raw 未复权(新浪备用源可回退)",
    )
    parser.add_argument("--retries", type=int, default=3, help="网络类失败的每标的重试次数")
    args = parser.parse_args()

    adjustment = PriceAdjustment.QFQ if args.adjust == "qfq" else PriceAdjustment.RAW
    context = build_full_context()
    end = date.today()
    start = end - timedelta(days=args.years * 365 + 30)

    print("1) 载入标的池配置 ...")
    result = context.universe.reload_from_config(ReloadUniverseRequest())  # type: ignore[attr-defined]
    print(
        f"   新增 {len(result.added)} 更新 {len(result.updated)} "
        f"停用 {len(result.disabled)} 不变 {result.unchanged_count}"
    )

    provider = AkshareProvider(id_generator=context.ids, publication_eligible=True)  # type: ignore[attr-defined]
    print(f"2) 同步交易日历 {start} → {end} ...")
    open_count = _sync_calendar(context, provider, start, end)
    print(f"   开市日 {open_count} 天")

    service = DataSyncService(
        provider=provider,
        parquet_store=context.parquet_store,  # type: ignore[attr-defined]
        batch_repository=context.batches,  # type: ignore[attr-defined]
        quality_service=context.quality,  # type: ignore[attr-defined]
        id_generator=context.ids,  # type: ignore[attr-defined]
    )
    symbols = tuple(
        instrument.symbol
        for instrument in context.instruments.list_all()  # type: ignore[attr-defined]
        if instrument.enabled
    )
    print(f"3) 同步 {len(symbols)} 只 ETF 日线 {start} → {end} ...")
    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for symbol in symbols:
        batch = report = None
        last_error: DomainError | None = None
        for attempt in range(args.retries + 1):
            try:
                batch, report = service.sync_daily_bars(
                    symbols=(symbol,),
                    start_date=start,
                    end_date=end,
                    adjustment=adjustment,
                )
                break
            except DomainError as error:
                last_error = error
                if not error.retryable or attempt == args.retries:
                    break
                time.sleep(args.pause * (2 ** (attempt + 1)))
        if batch is None or report is None:
            assert last_error is not None
            failed.append((symbol, f"{last_error.code}: {last_error.message}"))
            print(f"   {symbol}  失败  {last_error.code}")
            time.sleep(args.pause)
            continue
        print(
            f"   {symbol}  {batch.status.value}  rows={batch.row_count}  "
            f"gate={report.gate_status.value}"
            + (
                f"  findings={len(report.findings)}"
                if report.findings
                else ""
            )
        )
        succeeded.append(symbol)
        time.sleep(args.pause)

    print(f"完成: 成功 {len(succeeded)} / 失败 {len(failed)}")
    for symbol, reason in failed:
        print(f"   FAILED {symbol}: {reason}")
    return 0 if succeeded and not failed else (0 if succeeded else 1)


if __name__ == "__main__":
    raise SystemExit(main())
