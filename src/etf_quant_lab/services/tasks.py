"""Task-run tracking with single-instance file locks and recovery (node 18).

``TaskRunService`` wraps any callable job: it acquires an exclusive file lock so
two processes cannot run the same task, records the run in ``task_runs``, emits
audit events, retries retryable failures with a bounded budget, and on startup
marks timed-out RUNNING rows as FAILED so a crashed process never wedges the
scheduler.  Notification here is local-only (log + audit event) per the MVP.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage._json import decode_json, encode_json
from etf_quant_lab.storage.duckdb import DuckDBDatabase

TASK_LOCK_HELD = "TASK_LOCK_HELD"
TASK_ALREADY_TIMED_OUT = "TASK_TIMED_OUT"

_STATUS_RUNNING = "RUNNING"
_STATUS_SUCCEEDED = "SUCCEEDED"
_STATUS_FAILED = "FAILED"
_STATUS_SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class TaskRunResult:
    """Outcome of one supervised task execution."""

    task_run_id: str
    task_name: str
    status: str
    retry_count: int
    result_summary: Mapping[str, object]
    error_code: str | None = None
    error_message: str | None = None


class FileLock:
    """Exclusive lock via atomic create; stale locks expire by mtime."""

    def __init__(self, path: Path, *, timeout_seconds: int) -> None:
        self._path = path
        self._timeout_seconds = timeout_seconds
        self._held = False

    def acquire(self) -> bool:
        """Try to take the lock, breaking it first when the holder timed out."""

        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            age = datetime.now(UTC).timestamp() - self._path.stat().st_mtime
            if age <= self._timeout_seconds:
                return False
            self._path.unlink(missing_ok=True)  # stale holder: break the lock
        try:
            handle = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(f"pid={os.getpid()}\n")
        self._held = True
        return True

    def release(self) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False


class TaskRunService:
    """Supervise idempotent job execution with locking, retries and auditing."""

    def __init__(
        self,
        database: DuckDBDatabase,
        id_generator: IdGenerator,
        *,
        lock_dir: Path,
        lock_timeout_seconds: int = 900,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._id_generator = id_generator
        self._lock_dir = lock_dir
        self._lock_timeout_seconds = lock_timeout_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    def run(
        self,
        task_name: str,
        job: Callable[[], Mapping[str, object]],
        *,
        max_retries: int = 0,
        retryable: Callable[[Exception], bool] | None = None,
    ) -> TaskRunResult:
        """Run one job under the task lock, recording every attempt.

        A held lock yields a SKIPPED record (TASK-001 semantics) instead of a
        second concurrent execution.
        """

        lock = FileLock(
            self._lock_dir / f"{task_name}.lock",
            timeout_seconds=self._lock_timeout_seconds,
        )
        if not lock.acquire():
            return self._record_skipped(task_name)

        task_run_id = self._id_generator.new()
        started_at = self._clock()
        self._insert_running(task_run_id, task_name, started_at)
        retry_count = 0
        try:
            while True:
                try:
                    summary = dict(job())
                except Exception as exc:
                    can_retry = (
                        retry_count < max_retries
                        and retryable is not None
                        and retryable(exc)
                    )
                    if can_retry:
                        retry_count += 1
                        continue
                    self._finish(
                        task_run_id,
                        status=_STATUS_FAILED,
                        retry_count=retry_count,
                        summary={},
                        error_code=getattr(exc, "code", type(exc).__name__),
                        error_message=str(exc),
                    )
                    self._emit_event(
                        task_run_id,
                        event_type="TASK_FAILED",
                        severity="ERROR",
                        payload={"task_name": task_name, "error": str(exc)},
                    )
                    return self._get(task_run_id)
                self._finish(
                    task_run_id,
                    status=_STATUS_SUCCEEDED,
                    retry_count=retry_count,
                    summary=summary,
                )
                self._emit_event(
                    task_run_id,
                    event_type="TASK_SUCCEEDED",
                    severity="INFO",
                    payload={"task_name": task_name},
                )
                return self._get(task_run_id)
        finally:
            lock.release()

    def recover_stale_runs(self, *, timeout: timedelta | None = None) -> tuple[str, ...]:
        """Mark RUNNING rows older than the timeout as FAILED (startup recovery)."""

        limit = timeout or timedelta(seconds=self._lock_timeout_seconds)
        cutoff = self._clock() - limit
        with self._database.transaction() as connection:
            rows = connection.execute(
                """
                SELECT task_run_id FROM task_runs
                WHERE status = 'RUNNING' AND started_at < ?
                """,
                [cutoff],
            ).fetchall()
            stale_ids = tuple(cast(str, row[0]) for row in rows)
            for task_run_id in stale_ids:
                connection.execute(
                    """
                    UPDATE task_runs
                    SET status = 'FAILED', finished_at = ?, error_code = ?,
                        error_message = '任务超时, 启动恢复时标记失败'
                    WHERE task_run_id = ?
                    """,
                    [self._clock(), TASK_ALREADY_TIMED_OUT, task_run_id],
                )
        for task_run_id in stale_ids:
            self._emit_event(
                task_run_id,
                event_type="TASK_RECOVERED",
                severity="WARN",
                payload={"reason": "timeout"},
            )
        return stale_ids

    # ------------------------------------------------------------- persistence

    def _insert_running(
        self,
        task_run_id: str,
        task_name: str,
        started_at: datetime,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_run_id, task_name, status, lock_key, started_at,
                    retry_count, result_summary
                )
                VALUES (?, ?, 'RUNNING', ?, ?, 0, '{}')
                """,
                [task_run_id, task_name, f"{task_name}.lock", started_at],
            )

    def _finish(
        self,
        task_run_id: str,
        *,
        status: str,
        retry_count: int,
        summary: Mapping[str, object],
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, finished_at = ?, retry_count = ?,
                    result_summary = ?, error_code = ?, error_message = ?
                WHERE task_run_id = ?
                """,
                [
                    status,
                    self._clock(),
                    retry_count,
                    encode_json(summary),
                    error_code,
                    error_message,
                    task_run_id,
                ],
            )

    def _record_skipped(self, task_name: str) -> TaskRunResult:
        task_run_id = self._id_generator.new()
        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO task_runs (
                    task_run_id, task_name, status, lock_key, started_at,
                    finished_at, retry_count, result_summary, error_code
                )
                VALUES (?, ?, 'SKIPPED', ?, ?, ?, 0, '{}', ?)
                """,
                [
                    task_run_id,
                    task_name,
                    f"{task_name}.lock",
                    now,
                    now,
                    TASK_LOCK_HELD,
                ],
            )
        return self._get(task_run_id)

    def _emit_event(
        self,
        task_run_id: str,
        *,
        event_type: str,
        severity: str,
        payload: Mapping[str, object],
    ) -> None:
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, task_run_id, event_type, entity_type, entity_id,
                    severity, payload, created_at
                )
                VALUES (?, ?, ?, 'TASK_RUN', ?, ?, ?, ?)
                """,
                [
                    self._id_generator.new(),
                    task_run_id,
                    event_type,
                    task_run_id,
                    severity,
                    encode_json(payload),
                    self._clock(),
                ],
            )

    def _get(self, task_run_id: str) -> TaskRunResult:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT task_run_id, task_name, status, retry_count,
                       result_summary, error_code, error_message
                FROM task_runs WHERE task_run_id = ?
                """,
                [task_run_id],
            ).fetchone()
        if row is None:
            raise DomainError(
                "TASK_NOT_FOUND", "任务运行记录不存在", details={"task_run_id": task_run_id}
            )
        return TaskRunResult(
            task_run_id=cast(str, row[0]),
            task_name=cast(str, row[1]),
            status=cast(str, row[2]),
            retry_count=cast(int, row[3]),
            result_summary=decode_json(row[4]),
            error_code=cast(str | None, row[5]),
            error_message=cast(str | None, row[6]),
        )
