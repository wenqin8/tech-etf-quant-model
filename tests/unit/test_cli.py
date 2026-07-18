"""Unit tests for the maintenance CLI init command."""

from __future__ import annotations

from pathlib import Path

import pytest

from etf_quant_lab.cli import main


def _isolate_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EQL_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("EQL_DB_PATH", str(tmp_path / "data" / "etf_quant_lab.duckdb"))
    monkeypatch.setenv("EQL_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("EQL_BACKUP_DIR", str(tmp_path / "backup"))
    monkeypatch.setenv("EQL_CONFIG_DIR", str(tmp_path / "config"))


def test_init_applies_migrations_and_reports_safe_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_environment(tmp_path, monkeypatch)

    assert main(["init"]) == 0

    output = capsys.readouterr().out
    assert "migrations_applied=6" in output
    assert "tushare_configured=False" in output
    assert (tmp_path / "data" / "etf_quant_lab.duckdb").exists()


def test_init_is_idempotent_on_second_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_environment(tmp_path, monkeypatch)

    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["init"]) == 0

    assert "migrations_applied=0" in capsys.readouterr().out
