"""Integration tests for DuckDB connection, migration and transaction management."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from etf_quant_lab.storage.duckdb import DuckDBDatabase, MigrationChangedError

BATCH_ID = "01K0D7F7P6XQ4M2Z8H9B3C5NAA"


def test_migrate_applies_initial_schema_and_is_idempotent(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")

    applied = database.migrate()

    assert applied == (
        "001_initial",
        "002_quality",
        "003_strategy",
        "004_signal",
        "005_paper",
        "006_tasks",
    )
    with database.read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM instruments").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM data_batches").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM trading_calendar").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM data_files").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM quality_reports").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM quality_issues").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM strategy_definitions"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM signals").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM target_positions").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM v_daily_bars").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM v_active_daily_bar_files"
        ).fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM v_quality_gate").fetchone() == (0,)

    assert database.migrate() == ()


def test_migrate_rejects_edited_applied_migration(tmp_path: Path) -> None:
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    migration = migrations_dir / "001_demo.sql"
    migration.write_text("CREATE TABLE demo_table (id INTEGER);\n", encoding="utf-8")
    database = DuckDBDatabase(tmp_path / "eql.duckdb", migrations_dir=migrations_dir)

    assert database.migrate() == ("001_demo",)
    migration.write_text(
        "CREATE TABLE demo_table (id INTEGER, note VARCHAR);\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationChangedError, match="001_demo"):
        database.migrate()


def test_migrate_requires_at_least_one_migration(tmp_path: Path) -> None:
    empty_dir = tmp_path / "migrations"
    empty_dir.mkdir()
    database = DuckDBDatabase(tmp_path / "eql.duckdb", migrations_dir=empty_dir)

    with pytest.raises(FileNotFoundError):
        database.migrate()


def test_transaction_rolls_back_on_error(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()

    with pytest.raises(RuntimeError, match="boom"), database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO data_batches (
                batch_id, provider, dataset, fetched_at, status, schema_version
            )
            VALUES (?, 'TUSHARE', 'daily_bars', ?, 'FETCHING', 'daily_bar_v1')
            """,
            [BATCH_ID, datetime(2026, 7, 13, 8, 0, tzinfo=UTC)],
        )
        raise RuntimeError("boom")

    with database.read_connection() as connection:
        assert connection.execute("SELECT count(*) FROM data_batches").fetchone() == (0,)
