"""One-shot verification: sync a small real slice and print the audit trail.

Usage:

    uv run python scripts/verify_tushare_sync.py --source akshare --symbol 510300.SH --days 5
    uv run python scripts/verify_tushare_sync.py --source tushare ...  # needs EQL_TUSHARE_TOKEN

The Tushare token is read only inside the client factory and is never printed.
Retryable provider failures (flaky proxy, transient network) are retried with
backoff because this script exists to prove the happy path end to end.
"""

from __future__ import annotations

import argparse
import time
from datetime import date, timedelta

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.data.providers.akshare import AkshareProvider
from etf_quant_lab.services.data_sync import DataSyncService, build_tushare_provider


def main() -> int:
    parser = argparse.ArgumentParser(description="小范围真实数据同步验证")
    parser.add_argument("--symbol", default="510300.SH", help="标的代码, 默认沪深300ETF")
    parser.add_argument("--days", type=int, default=5, help="展示最近 N 个交易日")
    parser.add_argument(
        "--source",
        choices=("tushare", "akshare"),
        default="akshare",
        help="正式数据源, akshare 无需 Token",
    )
    parser.add_argument("--retries", type=int, default=5, help="可重试错误的最大重试次数")
    args = parser.parse_args()

    context = build_full_context()
    end = date.today()
    # A ~3-week natural window safely contains at least `days` trading days.
    start = end - timedelta(days=args.days * 2 + 10)

    try:
        if args.source == "tushare":
            provider = build_tushare_provider(context.settings, context.ids)
        else:
            provider = AkshareProvider(
                id_generator=context.ids,
                publication_eligible=True,
            )
        service = DataSyncService(
            provider=provider,
            parquet_store=context.parquet_store,
            batch_repository=context.batches,
            quality_service=context.quality,
            id_generator=context.ids,
        )
        print(f"[{args.source}] 同步 {args.symbol} {start} → {end} ...")
        batch = report = None
        # Tushare fund_daily only serves raw prices; AKShare uses QFQ default.
        from etf_quant_lab.contracts.enums import PriceAdjustment

        adjustment = (
            PriceAdjustment.RAW if args.source == "tushare" else PriceAdjustment.QFQ
        )
        for attempt in range(args.retries + 1):
            try:
                batch, report = service.sync_daily_bars(
                    symbols=(args.symbol,),
                    start_date=start,
                    end_date=end,
                    adjustment=adjustment,
                )
                break
            except DomainError as error:
                if not error.retryable or attempt == args.retries:
                    raise
                delay = 2.0 * (attempt + 1)
                print(f"  可重试错误({error.code}), {delay:.0f}s 后第 {attempt + 1} 次重试 ...")
                time.sleep(delay)
        assert batch is not None and report is not None
    except DomainError as error:
        print(f"同步失败: {error.code} - {error.message}")
        if error.details:
            print(f"详情: {error.details}")
        return 1

    print(f"批次: {batch.batch_id}  状态: {batch.status.value}")
    print(f"行数: {batch.row_count}  文件数: {batch.file_count}")
    print(f"质量门禁: {report.gate_status.value}  检查行数: {report.checked_rows}")
    for finding in report.findings:
        print(
            f"  [{finding.severity.value}] {finding.rule_code} "
            f"{finding.symbol} {finding.trade_date}: {finding.message}"
        )

    bars = context.batches.query_daily_bars(symbols=(args.symbol,))
    tail = bars[-args.days :]
    print(f"激活视图最近 {len(tail)} 个交易日:")
    for bar in tail:
        print(
            f"  {bar.trade_date}  open={bar.open}  close={bar.close}  "
            f"volume={bar.volume}  amount={bar.amount}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
