"""Smoke test: the composition root wires every real service."""

from __future__ import annotations

from pathlib import Path

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.config import AppSettings


def test_full_context_registers_all_services(tmp_path: Path) -> None:
    settings = AppSettings(
        env="test",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "eql.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
    )

    context = build_full_context(settings=settings, setup_logging=False)

    for name in (
        "database",
        "parquet_store",
        "instruments",
        "calendar",
        "batches",
        "signals",
        "universe",
        "strategy",
        "quality",
        "signal",
        "paper",
        "tasks",
    ):
        assert context.resolve(name) is not None

    # Both released strategies are registered and listable.
    descriptors = context.resolve("strategy").list_strategies()  # type: ignore[attr-defined]
    keys = {(d.strategy_id.value, d.version) for d in descriptors}
    assert keys == {
        ("TREND_BASELINE", "1.0.0"),
        ("ETF_ROTATION", "1.0.0"),
        ("THREE_DAY_TECH", "1.0.0"),
    }

    # Migrations applied: core tables exist and are queryable.
    database = context.resolve("database")
    with database.read_connection() as connection:  # type: ignore[attr-defined]
        assert connection.execute("SELECT count(*) FROM signals").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM paper_accounts").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM task_runs").fetchone() == (0,)
