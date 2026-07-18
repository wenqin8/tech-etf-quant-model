"""DuckDB connection, migration and transaction management."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, cast

import duckdb

if TYPE_CHECKING:
    from etf_quant_lab.config import AppSettings

DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "migrations"


class MigrationChangedError(RuntimeError):
    """Raised when an already-applied migration file is edited in place."""


class DuckDBDatabase:
    """Own DuckDB connections and serialize all local write transactions."""

    def __init__(self, db_path: Path, *, migrations_dir: Path | None = None) -> None:
        self.db_path = db_path.resolve()
        self.migrations_dir = (migrations_dir or DEFAULT_MIGRATIONS_DIR).resolve()
        self._write_lock = RLock()

    def connect(self, *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
        """Open a configured short-lived connection."""

        if not read_only:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(self.db_path), read_only=read_only)
        connection.execute("SET TimeZone = 'UTC'")
        return connection

    @contextmanager
    def read_connection(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Yield a read-only connection and always close it."""

        connection = self.connect(read_only=True)
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        """Run one serialized transaction with guaranteed rollback on failure."""

        with self._write_lock:
            connection = self.connect()
            connection.execute("BEGIN TRANSACTION")
            try:
                yield connection
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            else:
                connection.execute("COMMIT")
            finally:
                connection.close()

    def migrate(self) -> tuple[str, ...]:
        """Apply pending SQL migrations and reject mutated applied migrations."""

        migration_paths = tuple(sorted(self.migrations_dir.glob("[0-9][0-9][0-9]_*.sql")))
        if not migration_paths:
            raise FileNotFoundError(f"no DuckDB migrations found in {self.migrations_dir}")

        with self._write_lock:
            bootstrap = self.connect()
            try:
                bootstrap.execute(
                    """
                    CREATE TABLE IF NOT EXISTS schema_migrations (
                        version VARCHAR PRIMARY KEY,
                        checksum VARCHAR NOT NULL,
                        applied_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            finally:
                bootstrap.close()

            applied_now: list[str] = []
            for migration_path in migration_paths:
                version = migration_path.stem
                script = migration_path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(script.encode()).hexdigest()
                with self.transaction() as connection:
                    row = connection.execute(
                        "SELECT checksum FROM schema_migrations WHERE version = ?",
                        [version],
                    ).fetchone()
                    if row is not None:
                        recorded_checksum = cast(str, row[0])
                        if recorded_checksum != checksum:
                            raise MigrationChangedError(
                                f"applied migration changed: {migration_path.name}"
                            )
                        continue
                    connection.execute(script)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (?, ?)",
                        [version, checksum],
                    )
                    applied_now.append(version)
            return tuple(applied_now)


def build_database(settings: AppSettings) -> DuckDBDatabase:
    """Create a DuckDB database handle bound to the configured local file path."""

    return DuckDBDatabase(settings.db_path)
