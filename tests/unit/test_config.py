from __future__ import annotations

from pathlib import Path

import pytest

from etf_quant_lab.config import (
    AppSettings,
    initialize_directories,
    load_settings,
    redact_settings,
)
from etf_quant_lab.contracts.enums import DataSource
from etf_quant_lab.contracts.errors import DomainError


def test_environment_overrides_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text(
        "log_level: WARNING\nprimary_provider: AKSHARE\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("EQL_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("EQL_DATA_DIR", str(tmp_path / "env-data"))

    settings = load_settings(config_dir=config_dir)

    assert settings.log_level == "DEBUG"
    assert settings.primary_provider is DataSource.AKSHARE
    assert settings.data_dir == tmp_path / "env-data"


def test_invalid_or_missing_shape_is_reported_as_domain_error(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "app.yaml").write_text("- not-an-object\n", encoding="utf-8")

    with pytest.raises(DomainError) as raised:
        load_settings(config_dir=config_dir)

    assert raised.value.code == "CONFIG_INVALID"


def test_redacted_settings_never_expose_secret(tmp_path: Path) -> None:
    settings = AppSettings(
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "lab.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
        tushare_token="super-secret-token",
    )

    dumped = redact_settings(settings).model_dump_json()

    assert "super-secret-token" not in dumped
    assert '"tushare_configured":true' in dumped


def test_directory_initialization_creates_required_layout(tmp_path: Path) -> None:
    settings = AppSettings(
        env="test",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "lab.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
    )

    created = initialize_directories(settings)

    assert settings.data_dir / "raw" in created
    assert (settings.data_dir / "checkpoints").is_dir()
    assert settings.log_dir.is_dir()
    assert not list(tmp_path.rglob(".eql-write-test"))
