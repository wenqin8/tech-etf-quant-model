"""Unit tests for ETF universe models and application service."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from etf_quant_lab.contracts import DomainError, ErrorCode
from etf_quant_lab.contracts.enums import Exchange, InstrumentStatus, SortOrder
from etf_quant_lab.contracts.universe import ListInstrumentsRequest, ReloadUniverseRequest
from etf_quant_lab.domain.market import EtfInstrument
from etf_quant_lab.services.universe import UniverseService
from etf_quant_lab.storage.memory import InMemoryInstrumentRepository

FIXED_NOW = datetime(2026, 7, 13, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def _instrument(
    symbol: str = "510300.SH",
    *,
    name: str = "沪深300ETF",
    exchange: Exchange = Exchange.SSE,
    category: str = "BROAD_BASE",
    enabled: bool = True,
) -> EtfInstrument:
    status = InstrumentStatus.ACTIVE if enabled else InstrumentStatus.DISABLED
    return EtfInstrument(
        symbol=symbol,
        name=name,
        exchange=exchange,
        status=status,
        category=category,
        benchmark_symbol="000300.SH",
        enabled=enabled,
        metadata_version="test-v1",
    )


def _write_universe(path: Path, instruments_yaml: str) -> Path:
    path.write_text(
        "schema_version: 1\n"
        'metadata_version: "test-v2"\n'
        "instruments:\n"
        f"{instruments_yaml}",
        encoding="utf-8",
    )
    return path


def test_instrument_rejects_invalid_symbol_format() -> None:
    with pytest.raises(ValueError, match="invalid ETF symbol"):
        _instrument("510300")


def test_instrument_rejects_exchange_suffix_mismatch() -> None:
    with pytest.raises(ValueError, match="does not match exchange"):
        _instrument("159915.SZ", exchange=Exchange.SSE)


def test_instrument_rejects_invalid_lot_size() -> None:
    with pytest.raises(ValueError, match="lot_size"):
        EtfInstrument(
            symbol="510300.SH",
            name="沪深300ETF",
            exchange=Exchange.SSE,
            lot_size=0,
        )


def test_reload_adds_updates_and_disables_removed_instruments(tmp_path: Path) -> None:
    repository = InMemoryInstrumentRepository(
        (
            _instrument(name="旧名称"),
            _instrument("510500.SH", name="中证500ETF"),
        )
    )
    service = UniverseService(repository, clock=lambda: FIXED_NOW)
    config_path = _write_universe(
        tmp_path / "universe.yaml",
        "  - symbol: 510300.SH\n"
        "    name: 沪深300ETF\n"
        "    exchange: SSE\n"
        "    category: BROAD_BASE\n"
        "    benchmark_symbol: 000300.SH\n"
        "  - symbol: 159915.SZ\n"
        "    name: 创业板ETF\n"
        "    exchange: SZSE\n"
        "    category: GROWTH\n"
        "    benchmark_symbol: 399006.SZ\n",
    )

    result = service.reload_from_config(ReloadUniverseRequest(config_path=config_path))

    assert result.config_version.startswith("sha256:")
    assert result.added == ("159915.SZ",)
    assert result.updated == ("510300.SH",)
    assert result.disabled == ("510500.SH",)
    assert result.loaded_at == FIXED_NOW
    removed = repository.get("510500.SH")
    assert removed is not None
    assert removed.enabled is False
    assert removed.status is InstrumentStatus.DISABLED


def test_reload_dry_run_does_not_change_repository(tmp_path: Path) -> None:
    original = _instrument()
    repository = InMemoryInstrumentRepository((original,))
    service = UniverseService(repository, clock=lambda: FIXED_NOW)
    config_path = _write_universe(
        tmp_path / "universe.yaml",
        "  - symbol: 510300.SH\n"
        "    name: 新名称\n"
        "    exchange: SSE\n"
        "    category: BROAD_BASE\n",
    )

    result = service.reload_from_config(
        ReloadUniverseRequest(config_path=config_path, dry_run=True)
    )

    assert result.updated == ("510300.SH",)
    assert repository.get("510300.SH") == original


def test_reload_rejects_duplicate_symbols(tmp_path: Path) -> None:
    config_path = _write_universe(
        tmp_path / "universe.yaml",
        "  - symbol: 510300.SH\n"
        "    name: 沪深300ETF\n"
        "    exchange: SSE\n"
        "    category: BROAD_BASE\n"
        "  - symbol: 510300.SH\n"
        "    name: 重复ETF\n"
        "    exchange: SSE\n"
        "    category: BROAD_BASE\n",
    )
    service = UniverseService(InMemoryInstrumentRepository(), clock=lambda: FIXED_NOW)

    with pytest.raises(DomainError) as exc_info:
        service.reload_from_config(ReloadUniverseRequest(config_path=config_path))

    assert exc_info.value.code == ErrorCode.CONFIG_INVALID.value
    assert "duplicate ETF symbols" in str(exc_info.value.details["reason"])


def test_list_instruments_filters_sorts_and_paginates() -> None:
    repository = InMemoryInstrumentRepository(
        (
            _instrument(),
            _instrument(
                "159915.SZ",
                name="创业板ETF",
                exchange=Exchange.SZSE,
                category="GROWTH",
            ),
            _instrument("510500.SH", name="中证500ETF", enabled=False),
        )
    )
    service = UniverseService(repository)

    page = service.list_instruments(
        ListInstrumentsRequest(
            enabled_only=True,
            exchanges=(Exchange.SSE, Exchange.SZSE),
            keyword="ETF",
            page=1,
            page_size=1,
            sort_by="symbol",
            sort_order=SortOrder.DESC,
        )
    )

    assert page.total == 2
    assert page.total_pages == 2
    assert tuple(item.symbol for item in page.items) == ("510300.SH",)


def test_set_enabled_is_idempotent_and_reversible() -> None:
    repository = InMemoryInstrumentRepository((_instrument(),))
    service = UniverseService(repository)

    disabled = service.set_enabled("510300.SH", False, "暂停研究")
    repeated = service.set_enabled("510300.SH", False, "重复调用")
    enabled = service.set_enabled("510300.SH", True, "恢复研究")

    assert disabled.status is InstrumentStatus.DISABLED
    assert repeated == disabled
    assert enabled.status is InstrumentStatus.ACTIVE
    assert enabled.enabled is True


def test_default_universe_configuration_loads_all_etfs() -> None:
    repository = InMemoryInstrumentRepository()
    service = UniverseService(repository, clock=lambda: FIXED_NOW)

    result = service.reload_from_config(
        ReloadUniverseRequest(config_path=Path("config/universe.yaml"))
    )

    # 12 initial + 8 semis/tech + 6 robotics/sector + 3 focused additions.
    assert len(result.added) == 29
    assert len(repository.list_all()) == 29
    assert all(item.lot_size == 100 for item in repository.list_all())
    assert all(item.price_tick == Decimal("0.001") for item in repository.list_all())
    semiconductor = [
        item
        for item in repository.list_all()
        if item.metadata.get("role") == "semiconductor"
    ]
    assert {item.symbol for item in semiconductor} == {
        "512480.SH",
        "159995.SZ",
        "588200.SH",
        "512760.SH",
    }
    semiconductor_equipment = [
        item
        for item in repository.list_all()
        if item.metadata.get("role") == "semiconductor_equipment"
    ]
    assert {item.symbol for item in semiconductor_equipment} == {
        "159516.SZ",
        "588170.SH",
    }
    robotics = [
        item
        for item in repository.list_all()
        if item.metadata.get("role") == "robotics"
    ]
    assert {item.symbol for item in robotics} == {"562500.SH", "159770.SZ"}
    innovation_drug = [
        item
        for item in repository.list_all()
        if item.metadata.get("role") == "innovation_drug"
    ]
    assert {item.symbol for item in innovation_drug} == {"159992.SZ"}
