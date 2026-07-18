"""Integration tests for paper-account and audit-trail presenters."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.paper import PaperFillSource
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.paper import PaperAccountService
from etf_quant_lab.services.tasks import TaskRunService
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.ui.presenters import (
    LEDGER_BROKEN,
    LEDGER_NO_ACCOUNT,
    LEDGER_OK,
    build_audit_trail,
    build_paper_account_view,
)

NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 15)


def _setup(tmp_path: Path) -> tuple[DuckDBDatabase, PaperAccountService, str]:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    service = PaperAccountService(database, UlidGenerator(), clock=lambda: NOW)
    account = service.create_account(name="PAPER_MAIN", initial_cash=Decimal("10000"))
    return database, service, account.account_id


def test_no_account_shows_empty_state(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    service = PaperAccountService(database, UlidGenerator())

    view = build_paper_account_view(service, database, None)

    assert not view.account_found
    assert view.ledger_message == LEDGER_NO_ACCOUNT


def test_account_view_lists_positions_orders_and_ledger_ok(tmp_path: Path) -> None:
    database, service, account_id = _setup(tmp_path)
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=1000,
    )
    service.record_fill(
        order_id=order.order_id,
        trade_date=TRADE_DATE,
        price=Decimal("4.00"),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )

    view = build_paper_account_view(service, database, account_id)

    assert view.account_found
    assert view.account_status == "ACTIVE"
    assert view.cash_balance == "5995.0000"
    assert len(view.positions) == 1
    assert view.positions[0].symbol == "510300.SH"
    assert view.positions[0].pending_quantity == 1000  # T+1: bought today
    assert len(view.orders) == 1
    assert view.orders[0].status == "FILLED"
    assert view.ledger_consistent is True
    assert view.ledger_message == LEDGER_OK


def test_tampered_ledger_reports_broken_and_frozen(tmp_path: Path) -> None:
    database, service, account_id = _setup(tmp_path)
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=100,
    )
    service.record_fill(
        order_id=order.order_id,
        trade_date=TRADE_DATE,
        price=Decimal("4.00"),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE paper_accounts SET cash_balance = cash_balance + 50 WHERE account_id = ?",
            [account_id],
        )

    view = build_paper_account_view(service, database, account_id)

    assert view.ledger_consistent is False
    assert view.ledger_message == LEDGER_BROKEN
    assert view.account_status == "FROZEN"


def test_audit_trail_lists_task_events(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    tasks = TaskRunService(database, UlidGenerator(), lock_dir=tmp_path / "locks")
    tasks.run("daily_signal", lambda: {"ok": True})

    view = build_audit_trail(database)

    assert view.events
    assert view.events[0]["event_type"] == "TASK_SUCCEEDED"
    assert view.events[0]["severity"] == "INFO"
