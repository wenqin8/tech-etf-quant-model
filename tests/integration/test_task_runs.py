"""Integration tests for supervised task runs: locks, retries and recovery."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.tasks import FileLock, TaskRunService
from etf_quant_lab.storage.duckdb import DuckDBDatabase

NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


def _service(tmp_path: Path, *, clock: object = None) -> TaskRunService:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    return TaskRunService(
        database,
        UlidGenerator(),
        lock_dir=tmp_path / "locks",
        lock_timeout_seconds=900,
        clock=clock or (lambda: NOW),  # type: ignore[arg-type]
    )


def test_successful_run_records_summary_and_audit(tmp_path: Path) -> None:
    service = _service(tmp_path)

    result = service.run("daily_signal", lambda: {"signals": 1})

    assert result.status == "SUCCEEDED"
    assert result.result_summary == {"signals": 1}
    assert result.retry_count == 0


def test_held_lock_skips_duplicate_execution(tmp_path: Path) -> None:
    service = _service(tmp_path)
    lock = FileLock(tmp_path / "locks" / "daily_signal.lock", timeout_seconds=900)
    assert lock.acquire()  # another process holds the lock

    try:
        result = service.run("daily_signal", lambda: {"signals": 1})
    finally:
        lock.release()

    assert result.status == "SKIPPED"
    assert result.error_code == "TASK_LOCK_HELD"


def test_stale_lock_is_broken_and_task_runs(tmp_path: Path) -> None:
    lock_path = tmp_path / "locks" / "daily_signal.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_text("pid=0\n", encoding="utf-8")
    import os

    ancient = datetime.now(UTC).timestamp() - 3600  # older than the 900s timeout
    os.utime(lock_path, (ancient, ancient))
    service = _service(tmp_path)

    result = service.run("daily_signal", lambda: {"ok": True})

    assert result.status == "SUCCEEDED"


def test_retryable_failure_retries_within_budget(tmp_path: Path) -> None:
    service = _service(tmp_path)
    attempts: list[int] = []

    def flaky() -> dict[str, object]:
        attempts.append(1)
        if len(attempts) < 3:
            raise TimeoutError("transient")
        return {"attempts": len(attempts)}

    result = service.run(
        "data_sync",
        flaky,
        max_retries=3,
        retryable=lambda exc: isinstance(exc, TimeoutError),
    )

    assert result.status == "SUCCEEDED"
    assert result.retry_count == 2
    assert result.result_summary == {"attempts": 3}


def test_non_retryable_failure_records_error(tmp_path: Path) -> None:
    service = _service(tmp_path)

    def broken() -> dict[str, object]:
        raise PermissionError("token rejected")

    result = service.run(
        "data_sync",
        broken,
        max_retries=3,
        retryable=lambda exc: isinstance(exc, TimeoutError),
    )

    assert result.status == "FAILED"
    assert result.retry_count == 0
    assert result.error_message == "token rejected"


def test_lock_released_after_failure_allows_next_run(tmp_path: Path) -> None:
    service = _service(tmp_path)

    def broken() -> dict[str, object]:
        raise RuntimeError("boom")

    first = service.run("daily_signal", broken)
    second = service.run("daily_signal", lambda: {"ok": True})

    assert first.status == "FAILED"
    assert second.status == "SUCCEEDED"


def test_startup_recovery_fails_timed_out_running_rows(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    ids = UlidGenerator()
    stale_id = ids.new()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_runs (
                task_run_id, task_name, status, started_at, retry_count, result_summary
            )
            VALUES (?, 'daily_signal', 'RUNNING', ?, 0, '{}')
            """,
            [stale_id, NOW - timedelta(hours=2)],
        )
    service = TaskRunService(
        database, ids, lock_dir=tmp_path / "locks", clock=lambda: NOW
    )

    recovered = service.recover_stale_runs()

    assert recovered == (stale_id,)
    with database.read_connection() as connection:
        row = connection.execute(
            "SELECT status, error_code FROM task_runs WHERE task_run_id = ?",
            [stale_id],
        ).fetchone()
    assert row == ("FAILED", "TASK_TIMED_OUT")


def test_recent_running_row_is_not_recovered(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    ids = UlidGenerator()
    fresh_id = ids.new()
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO task_runs (
                task_run_id, task_name, status, started_at, retry_count, result_summary
            )
            VALUES (?, 'daily_signal', 'RUNNING', ?, 0, '{}')
            """,
            [fresh_id, NOW - timedelta(minutes=5)],
        )
    service = TaskRunService(
        database, ids, lock_dir=tmp_path / "locks", clock=lambda: NOW
    )

    assert service.recover_stale_runs() == ()
