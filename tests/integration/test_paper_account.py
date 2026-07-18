"""Integration tests for the transactional paper account service."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.paper import (
    PaperFillSource,
    PaperOrderStatus,
    PaperOrderType,
)
from etf_quant_lab.ids import UlidGenerator
from etf_quant_lab.services.paper import PaperAccountService
from etf_quant_lab.storage.duckdb import DuckDBDatabase

NOW = datetime(2026, 7, 13, 16, 40, tzinfo=UTC)
TRADE_DATE = date(2026, 7, 13)


def _service(tmp_path: Path) -> PaperAccountService:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    return PaperAccountService(database, UlidGenerator(), clock=lambda: NOW)


def _funded_account(service: PaperAccountService, cash: str = "10000") -> str:
    return service.create_account(name="PAPER_MAIN", initial_cash=Decimal(cash)).account_id


def _buy(
    service: PaperAccountService,
    account_id: str,
    *,
    quantity: int = 1000,
    price: str = "4.00",
    signal_id: str | None = None,
    trade_date: date = TRADE_DATE,
) -> None:
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=quantity,
        signal_id=signal_id,
    )
    service.record_fill(
        order_id=order.order_id,
        trade_date=trade_date,
        price=Decimal(price),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )


def test_buy_fill_updates_cash_position_and_order_atomically(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service)

    _buy(service, account_id, quantity=1000, price="4.00")

    account = service.get_account(account_id)
    assert account is not None
    # 10000 - (4000 gross + 5 commission) = 5995.
    assert account.cash_balance == Decimal("5995.0000")
    cash, positions = service.recompute_from_ledger(account_id)
    assert cash == account.cash_balance
    assert positions == {"510300.SH": 1000}


def test_insufficient_cash_rejects_fill_and_rolls_back(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service, cash="1000")
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=1000,
    )

    with pytest.raises(DomainError) as excinfo:
        service.record_fill(
            order_id=order.order_id,
            trade_date=TRADE_DATE,
            price=Decimal("4.00"),
            commission=Decimal("5"),
            source=PaperFillSource.NEXT_OPEN,
        )

    assert excinfo.value.code == "PAPER_INSUFFICIENT_CASH"
    account = service.get_account(account_id)
    assert account is not None
    assert account.cash_balance == Decimal("1000")
    # Order stays unfilled and the ledger is empty.
    stored = service.get_order(order.order_id)
    assert stored is not None
    assert stored.status is PaperOrderStatus.PROPOSED
    _, positions = service.recompute_from_ledger(account_id)
    assert positions == {}


def test_t_plus_one_blocks_same_day_sell_then_allows_next_day(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service)
    _buy(service, account_id, quantity=1000, price="4.00", trade_date=TRADE_DATE)

    sell = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.SELL,
        quantity=1000,
    )
    with pytest.raises(DomainError) as excinfo:
        service.record_fill(
            order_id=sell.order_id,
            trade_date=TRADE_DATE,  # same day as the buy: T+1 forbids selling
            price=Decimal("4.10"),
            commission=Decimal("5"),
            source=PaperFillSource.NEXT_OPEN,
        )
    assert excinfo.value.code == "PAPER_INSUFFICIENT_QUANTITY"

    # Next trading day the pending quantity becomes sellable.
    next_day = TRADE_DATE + timedelta(days=1)
    fill = service.record_fill(
        order_id=sell.order_id,
        trade_date=next_day,
        price=Decimal("4.10"),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )

    assert fill.quantity == 1000
    cash, positions = service.recompute_from_ledger(account_id)
    assert positions == {}
    # 10000 - 4005 + (4100 - 5) = 10090.
    assert cash == Decimal("10090.0000")


def test_duplicate_fill_is_idempotent(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service)
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=500,
    )

    first = service.record_fill(
        order_id=order.order_id,
        trade_date=TRADE_DATE,
        price=Decimal("4.00"),
        commission=Decimal("5"),
        source=PaperFillSource.NEXT_OPEN,
    )
    second = service.record_fill(
        order_id=order.order_id,
        trade_date=TRADE_DATE,
        price=Decimal("9.99"),  # different inputs must not create a second fill
        commission=Decimal("0"),
        source=PaperFillSource.MANUAL,
    )

    assert first.fill_id == second.fill_id
    account = service.get_account(account_id)
    assert account is not None
    assert account.cash_balance == Decimal("7995.0000")  # charged exactly once


def test_order_proposal_is_idempotent_per_signal_symbol_side(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service)

    first = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=100,
        signal_id="01K0D7F7P6XQ4M2Z8H9B3C5NS1",
    )
    second = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=999,  # repeat proposal returns the original order untouched
        signal_id="01K0D7F7P6XQ4M2Z8H9B3C5NS1",
    )

    assert first.order_id == second.order_id
    assert second.quantity == 100


def test_manual_fill_records_source_and_price(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service)
    order = service.propose_order(
        account_id=account_id,
        symbol="510300.SH",
        side=OrderSide.BUY,
        quantity=100,
        order_type=PaperOrderType.MANUAL,
    )

    fill = service.record_fill(
        order_id=order.order_id,
        trade_date=TRADE_DATE,
        price=Decimal("4.123"),
        commission=Decimal("5"),
        source=PaperFillSource.MANUAL,
    )

    assert fill.source is PaperFillSource.MANUAL
    assert fill.price == Decimal("4.123")


def test_ledger_verification_freezes_tampered_account(tmp_path: Path) -> None:
    database = DuckDBDatabase(tmp_path / "eql.duckdb")
    database.migrate()
    service = PaperAccountService(database, UlidGenerator(), clock=lambda: NOW)
    account_id = _funded_account(service)
    _buy(service, account_id)

    service.verify_ledger(account_id)  # consistent: no exception

    # Tamper with the cached balance behind the service's back.
    with database.transaction() as connection:
        connection.execute(
            "UPDATE paper_accounts SET cash_balance = cash_balance + 100 WHERE account_id = ?",
            [account_id],
        )

    with pytest.raises(DomainError) as excinfo:
        service.verify_ledger(account_id)

    assert excinfo.value.code == "PAPER_LEDGER_MISMATCH"
    account = service.get_account(account_id)
    assert account is not None
    assert account.status.value == "FROZEN"


def test_average_cost_tracks_multiple_buys(tmp_path: Path) -> None:
    service = _service(tmp_path)
    account_id = _funded_account(service, cash="100000")
    _buy(service, account_id, quantity=1000, price="4.00", trade_date=TRADE_DATE)
    _buy(
        service,
        account_id,
        quantity=1000,
        price="5.00",
        signal_id="01K0D7F7P6XQ4M2Z8H9B3C5NS2",
        trade_date=TRADE_DATE,
    )

    with DuckDBDatabase(tmp_path / "eql.duckdb").read_connection() as connection:
        row = connection.execute(
            "SELECT average_cost, quantity FROM paper_positions WHERE account_id = ?",
            [account_id],
        ).fetchone()

    assert row is not None
    assert row[0] == Decimal("4.500000")
    assert row[1] == 2000
