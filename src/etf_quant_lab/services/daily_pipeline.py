"""One-call daily pipeline: sync → gate → signal → NAV snapshot (post-close).

``run_daily_pipeline`` is what both the scheduler and the UI's one-click button
invoke.  It is idempotent end to end: incremental sync supersedes only earlier
top-ups, signal generation is idempotency-keyed, and the NAV snapshot upserts
per date.  Wrap it in ``TaskRunService.run`` for locking and audit.

Prices sync as QFQ (前复权) so fund splits/distributions do not fracture
momentum series.  QFQ history rescales after each new corporate action; the
14-day incremental overlap re-fetches recent days with fresh adjustment, and a
full re-sync (bootstrap script) realigns the whole history — run one after any
gate rejection that mentions extreme returns on a distribution date.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from etf_quant_lab.app_context import ApplicationContext
from etf_quant_lab.contracts.enums import Exchange, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.signal import (
    DailySignalBatch,
    GenerateDailySignalRequest,
)
from etf_quant_lab.data.providers.akshare import AkshareProvider
from etf_quant_lab.services.data_sync import DataSyncService, active_adjustment

# Weekly-cadence rotation preset, aligned with config/strategy_presets.yaml.
DEFAULT_STRATEGY_ID = StrategyId.ETF_ROTATION
DEFAULT_STRATEGY_VERSION = "1.0.0"
DEFAULT_PARAMETERS: dict[str, object] = {
    "momentum_window_short": 20,
    "momentum_window_medium": 60,
    "momentum_window_long": 120,
    "volatility_window": 20,
    "trend_filter_days": 120,
    "holdings_count": 3,
    "maximum_position_weight": Decimal("0.35"),
    "minimum_cash_weight": Decimal("0.10"),
    "cash_when_all_filtered": True,
}
_INCREMENTAL_LOOKBACK_DAYS = 14


@dataclass(frozen=True, slots=True)
class DailyPipelineResult:
    """Summary of one pipeline run, shaped for task summaries and the UI."""

    trade_date: date
    synced_symbols: int
    rejected_symbols: tuple[str, ...]
    signal: DailySignalBatch | None
    nav_equity: Decimal | None
    skipped_reason: str | None = None

    def as_summary(self) -> dict[str, object]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "synced_symbols": self.synced_symbols,
            "rejected_symbols": list(self.rejected_symbols),
            "signal_status": None if self.signal is None else self.signal.status,
            "nav_equity": None if self.nav_equity is None else str(self.nav_equity),
            "skipped_reason": self.skipped_reason,
        }


def run_daily_pipeline(
    context: ApplicationContext,
    *,
    trade_date: date,
    strategy_id: StrategyId = DEFAULT_STRATEGY_ID,
    strategy_version: str = DEFAULT_STRATEGY_VERSION,
    parameters: dict[str, object] | None = None,
    account_name: str = "PAPER_MAIN",
) -> DailyPipelineResult:
    """Run the post-close routine for one trade date.

    Non-trading days return a skipped result instead of raising, so a scheduler
    can fire every calendar day safely.
    """

    calendar = context.resolve("calendar")
    day = calendar.get_day(Exchange.SSE, trade_date)  # type: ignore[attr-defined]
    if day is None or not day.is_open:
        return DailyPipelineResult(
            trade_date=trade_date,
            synced_symbols=0,
            rejected_symbols=(),
            signal=None,
            nav_equity=None,
            skipped_reason="非交易日",
        )

    # 1. Incremental sync per enabled symbol (short window keeps requests light).
    sync_service = DataSyncService(
        provider=AkshareProvider(
            id_generator=context.ids,
            publication_eligible=True,
        ),
        parquet_store=context.resolve("parquet_store"),  # type: ignore[arg-type]
        batch_repository=context.resolve("batches"),  # type: ignore[arg-type]
        quality_service=context.resolve("quality"),  # type: ignore[arg-type]
        id_generator=context.ids,
    )
    instruments = context.resolve("instruments")
    symbols = tuple(
        instrument.symbol
        for instrument in instruments.list_all()  # type: ignore[attr-defined]
        if instrument.enabled
    )
    start = trade_date - timedelta(days=_INCREMENTAL_LOOKBACK_DAYS)
    # Top-ups must match the standing history's price basis (RAW vs QFQ), or
    # the series would mix adjusted and unadjusted prices at the seam.
    adjustment = active_adjustment(context.resolve("batches"))  # type: ignore[arg-type]
    synced = 0
    rejected: list[str] = []
    for symbol in symbols:
        try:
            batch, _report = sync_service.sync_daily_bars(
                symbols=(symbol,),
                start_date=start,
                end_date=trade_date,
                incremental=True,
                adjustment=adjustment,
            )
        except DomainError:
            rejected.append(symbol)
            continue
        if batch.status.value == "ACTIVE":
            synced += 1
        else:
            rejected.append(symbol)

    # 2. Daily signal (idempotent by construction).
    signal_service = context.resolve("signal")
    signal: DailySignalBatch | None
    try:
        signal = signal_service.generate_daily(  # type: ignore[attr-defined]
            GenerateDailySignalRequest(
                trade_date=trade_date,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                parameters=parameters or DEFAULT_PARAMETERS,
            )
        )
    except DomainError:
        signal = None

    # 3. NAV snapshot for the tracked paper account, valued at today's closes.
    paper = context.resolve("paper")
    nav_equity: Decimal | None = None
    account = paper.get_account_by_name(account_name)  # type: ignore[attr-defined]
    if account is not None:
        paper.settle_pending(account.account_id, as_of=trade_date)  # type: ignore[attr-defined]
        batches = context.resolve("batches")
        bars = batches.query_daily_bars(end_date=trade_date)  # type: ignore[attr-defined]
        closes: dict[str, Decimal] = {}
        for bar in bars:  # last write per symbol wins: bars are date-ascending
            closes[bar.symbol] = bar.close
        try:
            nav_equity = paper.record_nav_snapshot(  # type: ignore[attr-defined]
                account.account_id,
                trade_date=trade_date,
                close_prices=closes,
            )
        except DomainError:
            nav_equity = None

    return DailyPipelineResult(
        trade_date=trade_date,
        synced_symbols=synced,
        rejected_symbols=tuple(rejected),
        signal=signal,
        nav_equity=nav_equity,
    )
