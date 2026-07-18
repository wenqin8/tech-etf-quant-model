"""Framework-independent page presenters (node 19).

Each presenter reads application services and returns plain dataclasses the
Streamlit pages render verbatim.  Keeping them out of the ``ui`` layer makes the
"can I trade today?" logic testable without a browser: empty states, stale-data
banners and gate summaries are all decided here.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import Exchange, QualityGateStatus
from etf_quant_lab.contracts.signal import DailySignalBatch, SignalStatus
from etf_quant_lab.domain.repositories import TradingCalendarRepository
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.services.tasks import TaskRunResult
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.repositories import DataBatchRepository
from etf_quant_lab.storage.signal import SignalRepository

BANNER_NO_DATA = "尚未导入任何行情数据, 请先在数据中心同步或导入。"
BANNER_STALE = "行情数据落后于最近交易日, 今日信号不可用。"
BANNER_QUALITY_FAILED = "最近数据批次未通过质量门禁, 今日信号不可用。"
BANNER_READY = "数据就绪, 可以生成或查看今日信号。"
BANNER_READY_PREVIOUS_CLOSE = "数据截至上一交易日收盘, 可查看该日信号; 今日收盘后请再同步。"


@dataclass(frozen=True, slots=True)
class DashboardView:
    """Everything the overview page needs to answer '今天能不能用信号'."""

    as_of: date
    data_ready: bool
    banner: str
    latest_bar_date: date | None
    latest_batch: DataBatch | None
    gate_status: QualityGateStatus | None
    open_signal: DailySignalBatch | None


@dataclass(frozen=True, slots=True)
class DataCenterView:
    """Recent batches plus their gate outcomes for the data-center page."""

    batches: tuple[DataBatch, ...]
    gate_by_batch: dict[str, QualityGateStatus]
    latest_bar_date: date | None


@dataclass(frozen=True, slots=True)
class TaskHistoryView:
    """Recent task runs for the run-status page."""

    runs: tuple[TaskRunResult, ...]


@dataclass(frozen=True, slots=True)
class PaperPositionRow:
    """One holding row for the paper-account page."""

    symbol: str
    quantity: int
    available_quantity: int
    pending_quantity: int
    average_cost: str


@dataclass(frozen=True, slots=True)
class PaperOrderRow:
    """One order row with its lifecycle state."""

    order_id: str
    symbol: str
    side: str
    quantity: int
    status: str
    order_type: str


@dataclass(frozen=True, slots=True)
class PaperAccountView:
    """Paper-account page state: balances, holdings, orders, ledger health."""

    account_found: bool
    account_status: str | None
    cash_balance: str | None
    initial_cash: str | None
    positions: tuple[PaperPositionRow, ...]
    orders: tuple[PaperOrderRow, ...]
    ledger_consistent: bool | None
    ledger_message: str


@dataclass(frozen=True, slots=True)
class AuditTrailView:
    """Recent audit events for the run-log page."""

    events: tuple[dict[str, object], ...]


def build_dashboard(
    *,
    as_of: date,
    batch_repository: DataBatchRepository,
    calendar_repository: TradingCalendarRepository,
    quality_service: QualityService,
    signal_repository: SignalRepository,
    exchange: Exchange = Exchange.SSE,
    bars: tuple[DailyBar, ...] | None = None,
) -> DashboardView:
    """Assemble the overview state: data freshness, gate outcome, open signal.

    ``bars`` lets callers pass pre-loaded (cached) bars; when omitted the
    repository is queried directly, which scans every active Parquet file.
    """

    if bars is None:
        bars = batch_repository.query_daily_bars(end_date=as_of)
    else:
        bars = tuple(bar for bar in bars if bar.trade_date <= as_of)
    latest_bar_date = max((bar.trade_date for bar in bars), default=None)
    recent = batch_repository.list_recent(limit=1)
    latest_batch = recent[0] if recent else None
    gate_status: QualityGateStatus | None = None
    if latest_batch is not None:
        report = quality_service.get_report(latest_batch.batch_id)
        gate_status = None if report is None else report.gate_status

    # Today's bar only exists after the close, so intraday freshness is judged
    # against the previous open day; a bar dated today means a post-close sync
    # already ran and the view is fully current.
    expected_last_open = calendar_repository.previous_open_date(
        exchange, as_of, inclusive=False
    )

    if latest_bar_date is None:
        banner, ready = BANNER_NO_DATA, False
    elif gate_status == QualityGateStatus.FAILED:
        banner, ready = BANNER_QUALITY_FAILED, False
    elif expected_last_open is not None and latest_bar_date < expected_last_open:
        banner, ready = BANNER_STALE, False
    elif latest_bar_date < as_of:
        banner, ready = BANNER_READY_PREVIOUS_CLOSE, True
    else:
        banner, ready = BANNER_READY, True

    # Signals are decided on the latest closed bar, so surface that day's batch
    # (falling back keeps yesterday's signal visible during today's session).
    open_signal = _find_signal_for(signal_repository, as_of)
    if open_signal is None and latest_bar_date is not None and latest_bar_date < as_of:
        open_signal = _find_signal_for(signal_repository, latest_bar_date)
    return DashboardView(
        as_of=as_of,
        data_ready=ready,
        banner=banner,
        latest_bar_date=latest_bar_date,
        latest_batch=latest_batch,
        gate_status=gate_status,
        open_signal=open_signal,
    )


def build_data_center(
    *,
    batch_repository: DataBatchRepository,
    quality_service: QualityService,
    limit: int = 20,
    bars: tuple[DailyBar, ...] | None = None,
) -> DataCenterView:
    """List recent batches with their quality outcomes."""

    batches = batch_repository.list_recent(limit=limit)
    gate_by_batch: dict[str, QualityGateStatus] = {}
    for batch in batches:
        report = quality_service.get_report(batch.batch_id)
        if report is not None:
            gate_by_batch[batch.batch_id] = report.gate_status
    if bars is None:
        bars = batch_repository.query_daily_bars()
    latest_bar_date = max((bar.trade_date for bar in bars), default=None)
    return DataCenterView(
        batches=batches,
        gate_by_batch=gate_by_batch,
        latest_bar_date=latest_bar_date,
    )


def build_task_history(
    database: DuckDBDatabase,
    *,
    limit: int = 50,
) -> TaskHistoryView:
    """Return recent task runs, newest first, for the run-status page."""

    from typing import cast

    from etf_quant_lab.storage._json import decode_json

    with database.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT task_run_id, task_name, status, retry_count,
                   result_summary, error_code, error_message
            FROM task_runs
            ORDER BY started_at DESC, task_run_id DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    runs = tuple(
        TaskRunResult(
            task_run_id=cast(str, row[0]),
            task_name=cast(str, row[1]),
            status=cast(str, row[2]),
            retry_count=cast(int, row[3]),
            result_summary=decode_json(row[4]),
            error_code=cast(str | None, row[5]),
            error_message=cast(str | None, row[6]),
        )
        for row in rows
    )
    return TaskHistoryView(runs=runs)


def _find_signal_for(
    signal_repository: SignalRepository,
    trade_date: date,
) -> DailySignalBatch | None:
    """Return the newest signal batch stored for one trade date, if any."""

    return signal_repository.find_latest_for_date(trade_date)


def describe_signal_state(signal: DailySignalBatch | None) -> str:
    """One-line, user-facing summary of today's signal availability."""

    if signal is None:
        return "今日尚未生成信号。"
    if signal.status == SignalStatus.BLOCKED:
        return f"今日信号被阻断: {signal.blocked_reason}"
    holdings = len(signal.items)
    return f"今日信号有效, 共 {holdings} 条目标仓位, 现金比例 {signal.target_cash_weight}。"


LEDGER_OK = "账本一致: 现金与持仓可由成交流水独立重算。"
LEDGER_BROKEN = "账本不平: 账户已冻结, 请检查成交流水。"
LEDGER_NO_ACCOUNT = "尚未创建模拟账户。"


def build_paper_account_view(
    paper_service: object,
    database: DuckDBDatabase,
    account_id: str | None,
) -> PaperAccountView:
    """Assemble the paper-account page, including a live ledger health check."""

    from typing import cast

    from etf_quant_lab.contracts.errors import DomainError
    from etf_quant_lab.services.paper import PaperAccountService

    service = cast(PaperAccountService, paper_service)
    if account_id is None:
        return PaperAccountView(
            account_found=False,
            account_status=None,
            cash_balance=None,
            initial_cash=None,
            positions=(),
            orders=(),
            ledger_consistent=None,
            ledger_message=LEDGER_NO_ACCOUNT,
        )
    account = service.get_account(account_id)
    if account is None:
        return PaperAccountView(
            account_found=False,
            account_status=None,
            cash_balance=None,
            initial_cash=None,
            positions=(),
            orders=(),
            ledger_consistent=None,
            ledger_message=LEDGER_NO_ACCOUNT,
        )

    with database.read_connection() as connection:
        position_rows = connection.execute(
            """
            SELECT symbol, quantity, available_quantity, pending_quantity, average_cost
            FROM paper_positions
            WHERE account_id = ? AND quantity > 0
            ORDER BY symbol
            """,
            [account_id],
        ).fetchall()
        order_rows = connection.execute(
            """
            SELECT order_id, symbol, side, quantity, status, order_type
            FROM paper_orders
            WHERE account_id = ?
            ORDER BY created_at DESC, order_id DESC
            LIMIT 50
            """,
            [account_id],
        ).fetchall()

    try:
        service.verify_ledger(account_id)
        consistent, message = True, LEDGER_OK
    except DomainError:
        consistent, message = False, LEDGER_BROKEN

    latest = service.get_account(account_id)
    assert latest is not None
    return PaperAccountView(
        account_found=True,
        account_status=latest.status.value,
        cash_balance=str(latest.cash_balance),
        initial_cash=str(latest.initial_cash),
        positions=tuple(
            PaperPositionRow(
                symbol=str(row[0]),
                quantity=int(cast(int, row[1])),
                available_quantity=int(cast(int, row[2])),
                pending_quantity=int(cast(int, row[3])),
                average_cost=str(row[4]),
            )
            for row in position_rows
        ),
        orders=tuple(
            PaperOrderRow(
                order_id=str(row[0]),
                symbol=str(row[1]),
                side=str(row[2]),
                quantity=int(cast(int, row[3])),
                status=str(row[4]),
                order_type=str(row[5]),
            )
            for row in order_rows
        ),
        ledger_consistent=consistent,
        ledger_message=message,
    )


def build_audit_trail(
    database: DuckDBDatabase,
    *,
    limit: int = 100,
) -> AuditTrailView:
    """Return recent audit events, newest first, for the run-log page."""

    from etf_quant_lab.storage._json import decode_json

    with database.read_connection() as connection:
        rows = connection.execute(
            """
            SELECT event_id, task_run_id, event_type, entity_type, entity_id,
                   severity, payload, created_at
            FROM audit_events
            ORDER BY created_at DESC, event_id DESC
            LIMIT ?
            """,
            [limit],
        ).fetchall()
    events = tuple(
        {
            "event_id": row[0],
            "task_run_id": row[1],
            "event_type": row[2],
            "entity_type": row[3],
            "entity_id": row[4],
            "severity": row[5],
            "payload": decode_json(row[6]),
            "created_at": row[7],
        }
        for row in rows
    )
    return AuditTrailView(events=events)
