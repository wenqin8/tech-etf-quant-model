"""Integration tests for the DuckDB strategy definition repository."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.strategy import StrategyDefinitionRepository

REGISTERED_AT = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def _repository(tmp_path: Path) -> StrategyDefinitionRepository:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    return StrategyDefinitionRepository(
        database,
        UlidGenerator(),
        clock=lambda: REGISTERED_AT,
    )


def test_register_is_idempotent_for_identical_definition(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    schema = {"top_n": {"type": "int", "default": 3}}

    first = repository.register(
        strategy_key="etf_rotation",
        version="1.0.0",
        name="ETF 动量轮动策略",
        parameter_schema=schema,
        code_hash="abc123",
    )
    second = repository.register(
        strategy_key="etf_rotation",
        version="1.0.0",
        name="ETF 动量轮动策略",
        parameter_schema=schema,
        code_hash="abc123",
    )

    assert first == second
    assert repository.get_id("etf_rotation", "1.0.0") == first


def test_register_rejects_changed_code_under_same_version(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    repository.register(
        strategy_key="etf_rotation",
        version="1.0.0",
        name="ETF 动量轮动策略",
        parameter_schema={"top_n": {"type": "int"}},
        code_hash="abc123",
    )

    with pytest.raises(RuntimeError, match="changed under a released version"):
        repository.register(
            strategy_key="etf_rotation",
            version="1.0.0",
            name="ETF 动量轮动策略",
            parameter_schema={"top_n": {"type": "int"}},
            code_hash="def456",
        )


def test_distinct_versions_coexist(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    first = repository.register(
        strategy_key="etf_rotation",
        version="1.0.0",
        name="v1",
        parameter_schema={},
        code_hash="hash-1",
    )
    second = repository.register(
        strategy_key="etf_rotation",
        version="1.1.0",
        name="v1.1",
        parameter_schema={},
        code_hash="hash-2",
    )

    assert first != second
    assert repository.get_id("etf_rotation", "1.1.0") == second
    assert repository.get_id("etf_rotation", "9.9.9") is None
