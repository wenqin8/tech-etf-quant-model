from pathlib import Path

import pytest

from etf_quant_lab.app_context import build_application_context
from etf_quant_lab.config import AppSettings
from etf_quant_lab.ids import FixedIdGenerator


def test_context_registers_and_resolves_services(tmp_path: Path) -> None:
    settings = AppSettings(
        env="test",
        data_dir=tmp_path / "data",
        db_path=tmp_path / "data" / "lab.duckdb",
        log_dir=tmp_path / "logs",
        config_dir=tmp_path / "config",
        backup_dir=tmp_path / "backup",
    )
    service = object()
    context = build_application_context(
        settings=settings,
        ids=FixedIdGenerator(["01K0D7F7P6XQ4M2Z8H9B3C5N12"]),
        services={"universe": service},
        initialize_paths=False,
        setup_logging=False,
    )

    assert context.universe is service
    assert context.ids.new() == "01K0D7F7P6XQ4M2Z8H9B3C5N12"
    with pytest.raises(AttributeError, match="unavailable"):
        _ = context.data
