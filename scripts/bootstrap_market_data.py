"""Bootstrap the research database with real market data.

Steps (idempotent, safe to re-run):

1. Load the ETF universe from ``config/universe.yaml`` into DuckDB.
2. Sync the SSE trading calendar (AKShare sina feed) into ``trading_calendar``.
3. Sync daily bars for every enabled ETF over the requested window
   (AKShare, publication-eligible), one batch per symbol.

The downloader is deliberately conservative with the Eastmoney history endpoint:

* complete ACTIVE histories are skipped by default;
* requests are spaced by a base delay plus random jitter;
* retryable errors expose a redacted exception-chain summary;
* repeated symbol failures open a circuit breaker instead of hammering the source.

Usage:

    uv run python scripts/bootstrap_market_data.py --years 3
    uv run python scripts/bootstrap_market_data.py --symbols 510300.SH,510500.SH
"""

from __future__ import annotations

import argparse
import random
import re
import time
from datetime import UTC, date, datetime, timedelta

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.data import TradeCalendarQuery
from etf_quant_lab.contracts.enums import DataBatchStatus, Exchange, PriceAdjustment
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.universe import ReloadUniverseRequest
from etf_quant_lab.data.providers.akshare import AkshareProvider
from etf_quant_lab.domain.market import TradingCalendarDay
from etf_quant_lab.services.data_sync import DataSyncService

_SECRET_PATTERN = re.compile(
    r"(?i)(token|api[_-]?key|secret|password|passwd)=([^&\s'\"]+)"
)
_URL_CREDENTIAL_PATTERN = re.compile(r"(https?://)[^/@\s]+@", flags=re.IGNORECASE)


def _parse_symbols(value: str) -> tuple[str, ...]:
    """Parse a comma/Chinese-comma/whitespace separated symbol list."""

    symbols = tuple(
        item.upper()
        for item in re.split(r"[,\N{FULLWIDTH COMMA}\s]+", value.strip())
        if item.strip()
    )
    if not symbols:
        raise argparse.ArgumentTypeError("标的列表不能为空")
    if len(set(symbols)) != len(symbols):
        raise argparse.ArgumentTypeError("标的列表不能包含重复项")
    invalid = tuple(
        symbol
        for symbol in symbols
        if not re.fullmatch(r"\d{6}\.(?:SH|SZ)", symbol)
    )
    if invalid:
        raise argparse.ArgumentTypeError(
            "标的格式应为 510300.SH 或 159915.SZ: " + ", ".join(invalid)
        )
    return symbols


def _redact_error_text(value: str) -> str:
    """Keep diagnostics useful without printing tokens or proxy credentials."""

    one_line = " ".join(value.split())
    one_line = _URL_CREDENTIAL_PATTERN.sub(r"\1***@", one_line)
    one_line = _SECRET_PATTERN.sub(r"\1=***", one_line)
    return one_line[:500]


def _diagnostic_message(error: BaseException) -> str:
    """Return a compact, redacted exception chain for terminal diagnostics."""

    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(parts) < 4:
        seen.add(id(current))
        message = _redact_error_text(str(current))
        part = type(current).__name__ + (f": {message}" if message else "")
        if part not in parts:
            parts.append(part)
        current = current.__cause__ or (
            None if current.__suppress_context__ else current.__context__
        )
    return " <- ".join(parts)


def _sync_calendar(
    context: object,
    provider: AkshareProvider,
    start: date,
    end: date,
) -> frozenset[date]:
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
    return frozenset(open_dates)


def _select_symbols(
    enabled_symbols: tuple[str, ...],
    requested_symbols: tuple[str, ...],
) -> tuple[str, ...]:
    if not requested_symbols:
        return enabled_symbols
    enabled = set(enabled_symbols)
    unknown = tuple(symbol for symbol in requested_symbols if symbol not in enabled)
    if unknown:
        raise ValueError("标的不在已启用标的池中: " + ", ".join(unknown))
    return requested_symbols


def _complete_existing_symbols(
    context: object,
    *,
    symbols: tuple[str, ...],
    expected_dates: frozenset[date],
    adjustment: PriceAdjustment,
) -> frozenset[str]:
    """Find ACTIVE per-symbol histories covering every expected trading day."""

    active_adjustments: dict[str, str] = {}
    requested = set(symbols)
    for batch in context.batches.list_recent(limit=2_000):  # type: ignore[attr-defined]
        if batch.status != DataBatchStatus.ACTIVE or batch.dataset != "daily_bars":
            continue
        symbol_key = str(batch.metadata.get("symbol_key", ""))
        batch_symbols = tuple(item for item in symbol_key.split(",") if item)
        for symbol in batch_symbols:
            if symbol in requested:
                active_adjustments.setdefault(
                    symbol,
                    str(batch.metadata.get("adjustment", PriceAdjustment.RAW.value)),
                )

    complete: set[str] = set()
    for symbol in symbols:
        if active_adjustments.get(symbol) != adjustment.value:
            continue
        bars = context.batches.query_daily_bars(  # type: ignore[attr-defined]
            symbols=(symbol,),
            start_date=min(expected_dates) if expected_dates else None,
            end_date=max(expected_dates) if expected_dates else None,
        )
        stored_dates = {bar.trade_date for bar in bars}
        if expected_dates and expected_dates.issubset(stored_dates):
            complete.add(symbol)
    return frozenset(complete)


def _delay_seconds(base: float, jitter: float, *, multiplier: float = 1.0) -> float:
    return base * multiplier + (random.uniform(0.0, jitter) if jitter else 0.0)


def _sleep_with_notice(delay: float, reason: str) -> None:
    if delay <= 0:
        return
    print(f"      等待 {delay:.1f}s ({reason})")
    time.sleep(delay)


def main() -> int:
    parser = argparse.ArgumentParser(description="初始化真实行情数据库")
    parser.add_argument("--years", type=int, default=5, help="回溯年数")
    parser.add_argument(
        "--symbols",
        type=_parse_symbols,
        default=(),
        metavar="CODE.SH,CODE.SZ",
        help="只同步指定标的, 使用逗号分隔",
    )
    parser.add_argument(
        "--skip-existing",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="跳过已覆盖整个窗口且复权口径一致的 ACTIVE 数据 (默认开启)",
    )
    parser.add_argument("--pause", type=float, default=8.0, help="标的间最短间隔秒数")
    parser.add_argument("--jitter", type=float, default=7.0, help="标的间随机附加等待秒数")
    parser.add_argument(
        "--adjust",
        choices=("qfq", "raw"),
        default="qfq",
        help="价格口径: qfq 前复权(推荐, 东财失败时由新浪复权记录合成) / raw 未复权",
    )
    parser.add_argument("--retries", type=int, default=1, help="网络类失败的每标的重试次数")
    parser.add_argument(
        "--max-consecutive-failures",
        type=int,
        default=2,
        help="连续失败达到该数量后停止, 保护数据源和当前 IP",
    )
    args = parser.parse_args()
    if args.years <= 0:
        parser.error("--years 必须大于 0")
    if args.pause < 0 or args.jitter < 0:
        parser.error("--pause 和 --jitter 不能为负数")
    if args.retries < 0:
        parser.error("--retries 不能为负数")
    if args.max_consecutive_failures <= 0:
        parser.error("--max-consecutive-failures 必须大于 0")

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
    open_dates = _sync_calendar(context, provider, start, end)
    print(f"   开市日 {len(open_dates)} 天")

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
    try:
        symbols = _select_symbols(symbols, args.symbols)
    except ValueError as error:
        parser.error(str(error))

    skipped: frozenset[str] = frozenset()
    if args.skip_existing:
        skipped = _complete_existing_symbols(
            context,
            symbols=symbols,
            expected_dates=open_dates,
            adjustment=adjustment,
        )
    pending = tuple(symbol for symbol in symbols if symbol not in skipped)
    print(
        f"3) 同步 {len(pending)} 只 ETF 日线 {start} → {end} ..."
        f" (已跳过完整数据 {len(skipped)} 只)"
    )
    for symbol in symbols:
        if symbol in skipped:
            print(f"   {symbol}  SKIPPED  已有完整 {adjustment.value} 数据")

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    not_attempted: list[str] = []
    consecutive_failures = 0
    for index, symbol in enumerate(pending):
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
                diagnostic = _diagnostic_message(error)
                retry_delay = _delay_seconds(
                    args.pause,
                    args.jitter,
                    multiplier=2 ** (attempt + 1),
                )
                print(
                    f"   {symbol}  第 {attempt + 1} 次失败, 将重试"
                    f"  detail={diagnostic}"
                )
                _sleep_with_notice(retry_delay, "指数退避")
        if batch is None or report is None:
            assert last_error is not None
            diagnostic = _diagnostic_message(last_error)
            failed.append(
                (
                    symbol,
                    f"{last_error.code}: {last_error.message}; {diagnostic}",
                )
            )
            consecutive_failures += 1
            print(
                f"   {symbol}  FAILED  {last_error.code}"
                f"  consecutive={consecutive_failures}"
            )
            print(f"      detail={diagnostic}")
        elif batch.status != DataBatchStatus.ACTIVE:
            reason = (
                f"batch={batch.status.value}; gate={report.gate_status.value}; "
                f"findings={len(report.findings)}"
            )
            failed.append((symbol, reason))
            consecutive_failures = 0
            print(f"   {symbol}  FAILED  {reason}")
        else:
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
            consecutive_failures = 0

        if consecutive_failures >= args.max_consecutive_failures:
            not_attempted.extend(pending[index + 1 :])
            print(
                "   CIRCUIT_OPEN  "
                f"连续失败 {consecutive_failures} 只, 停止请求; "
                f"剩余 {len(not_attempted)} 只未尝试"
            )
            break

        if index < len(pending) - 1:
            _sleep_with_notice(
                _delay_seconds(args.pause, args.jitter),
                "标的间限速",
            )

    print(
        f"完成: 跳过 {len(skipped)} / 新增成功 {len(succeeded)} / "
        f"失败 {len(failed)} / 未尝试 {len(not_attempted)}"
    )
    for symbol, reason in failed:
        print(f"   FAILED {symbol}: {reason}")
    if not_attempted:
        print("   NOT_ATTEMPTED " + ", ".join(not_attempted))
    return 0 if not failed and not not_attempted else 1


if __name__ == "__main__":
    raise SystemExit(main())
