"""Unit tests for the resumable real-market-data bootstrap helpers."""

from __future__ import annotations

import argparse
from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from etf_quant_lab.contracts.enums import DataBatchStatus, PriceAdjustment
from scripts.bootstrap_market_data import (
    _complete_existing_symbols,
    _diagnostic_message,
    _parse_symbols,
    _select_symbols,
)


def test_parse_symbols_accepts_common_separators_and_normalizes_case() -> None:
    assert _parse_symbols("510300.sh\N{FULLWIDTH COMMA} 159915.sz") == (
        "510300.SH",
        "159915.SZ",
    )


@pytest.mark.parametrize(
    "value",
    ["", "510300", "510300.SH,510300.SH", "not-a-symbol"],
)
def test_parse_symbols_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_symbols(value)


def test_select_symbols_rejects_disabled_or_unknown_symbol() -> None:
    with pytest.raises(ValueError, match="不在已启用标的池"):
        _select_symbols(("510300.SH",), ("159915.SZ",))


def test_diagnostic_message_keeps_cause_and_redacts_credentials() -> None:
    upstream = requests.exceptions.ProxyError(
        "https://alice:private@example.test/path?token=top-secret disconnected"
    )
    wrapped = RuntimeError("provider unavailable")
    wrapped.__cause__ = upstream

    diagnostic = _diagnostic_message(wrapped)

    assert "RuntimeError" in diagnostic
    assert "ProxyError" in diagnostic
    assert "alice" not in diagnostic
    assert "private" not in diagnostic
    assert "top-secret" not in diagnostic


def test_complete_existing_symbols_requires_active_matching_complete_history() -> None:
    expected_dates = frozenset(
        {
            date(2026, 7, 22),
            date(2026, 7, 23),
            date(2026, 7, 24),
        }
    )
    batches = (
        SimpleNamespace(
            status=DataBatchStatus.ACTIVE,
            dataset="daily_bars",
            metadata={"symbol_key": "510300.SH", "adjustment": "QFQ"},
        ),
        SimpleNamespace(
            status=DataBatchStatus.ACTIVE,
            dataset="daily_bars",
            metadata={"symbol_key": "159915.SZ", "adjustment": "RAW"},
        ),
    )

    class _Repository:
        @staticmethod
        def list_recent(*, limit: int) -> tuple[Any, ...]:
            assert limit == 2_000
            return batches

        @staticmethod
        def query_daily_bars(
            *,
            symbols: tuple[str, ...],
            start_date: date | None,
            end_date: date | None,
        ) -> tuple[Any, ...]:
            assert symbols == ("510300.SH",)
            assert start_date == date(2026, 7, 22)
            assert end_date == date(2026, 7, 24)
            return tuple(SimpleNamespace(trade_date=value) for value in expected_dates)

    context = SimpleNamespace(batches=_Repository())

    complete = _complete_existing_symbols(
        context,
        symbols=("510300.SH", "159915.SZ"),
        expected_dates=expected_dates,
        adjustment=PriceAdjustment.QFQ,
    )

    assert complete == frozenset({"510300.SH"})
