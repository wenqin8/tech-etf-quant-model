from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

from etf_quant_lab.config import AppSettings
from etf_quant_lab.logging import configure_logging, get_logger


def _settings(tmp_path: Path) -> AppSettings:
    return AppSettings(
        env="test",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "lab.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
        log_retention_days=7,
    )


def test_logging_writes_json_and_configures_daily_rotation(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    configure_logging(settings)

    get_logger("test").info(
        "foundation.test",
        message="unit-test message",
        run_id="01K0D7F7P6XQ4M2Z8H9B3C5N12",
    )
    for handler in logging.getLogger().handlers:
        handler.flush()

    record = json.loads((settings.log_dir / "etf_quant_lab.jsonl").read_text(encoding="utf-8"))
    rotating = [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler, logging.handlers.TimedRotatingFileHandler)
    ]

    assert record["event"] == "foundation.test"
    assert record["message"] == "unit-test message"
    assert record["run_id"] == "01K0D7F7P6XQ4M2Z8H9B3C5N12"
    assert rotating[0].backupCount == 7
