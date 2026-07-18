"""Long-running daily scheduler: post-close pipeline every trading day.

Runs the daily pipeline (sync → gate → signal → NAV) at the configured signal
time (default 16:30 Asia/Shanghai) under TaskRunService supervision — file lock
against double instances, task_runs records, audit events, startup recovery.

    uv run python scripts/run_scheduler.py            # keep running
    uv run python scripts/run_scheduler.py --once     # run once now and exit
"""

from __future__ import annotations

import argparse
from datetime import datetime
from zoneinfo import ZoneInfo

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.services.daily_pipeline import run_daily_pipeline

_MARKET_TZ = ZoneInfo("Asia/Shanghai")
_TASK_NAME = "daily_pipeline"


def _run_once(context: object) -> None:
    tasks = context.resolve("tasks")  # type: ignore[attr-defined]
    trade_date = datetime.now(_MARKET_TZ).date()

    def job() -> dict[str, object]:
        result = run_daily_pipeline(context, trade_date=trade_date)  # type: ignore[arg-type]
        return result.as_summary()

    outcome = tasks.run(
        _TASK_NAME,
        job,
        max_retries=2,
        retryable=lambda exc: "UNAVAILABLE" in getattr(exc, "code", "")
        or isinstance(exc, TimeoutError),
    )
    print(
        f"[{datetime.now(_MARKET_TZ):%Y-%m-%d %H:%M:%S}] {_TASK_NAME} "
        f"{outcome.status} retry={outcome.retry_count} {outcome.result_summary}"
        + (f" error={outcome.error_message}" if outcome.error_message else "")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="每日收盘流水线调度器")
    parser.add_argument("--once", action="store_true", help="立即运行一次后退出")
    args = parser.parse_args()

    context = build_full_context()
    tasks = context.resolve("tasks")
    recovered = tasks.recover_stale_runs()  # type: ignore[attr-defined]
    if recovered:
        print(f"启动恢复: {len(recovered)} 个超时任务已标记失败")

    if args.once:
        _run_once(context)
        return 0

    from apscheduler.schedulers.blocking import BlockingScheduler

    settings = context.settings  # type: ignore[attr-defined]
    scheduler = BlockingScheduler(timezone=str(_MARKET_TZ))
    scheduler.add_job(
        lambda: _run_once(context),
        trigger="cron",
        day_of_week="mon-fri",
        hour=settings.signal_hour,
        minute=settings.signal_minute,
        id=_TASK_NAME,
        misfire_grace_time=3600,
        coalesce=True,
    )
    print(
        f"调度器已启动: 周一至周五 {settings.signal_hour:02d}:{settings.signal_minute:02d} "
        f"(Asia/Shanghai) 运行每日流水线。Ctrl+C 退出。"
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        print("调度器已停止。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
