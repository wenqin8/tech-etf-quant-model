"""Transactional paper-account service (node 15).

Every fill mutates order status, cash, position and the fill ledger inside one
DuckDB transaction, so a crash can never leave the account half-updated.  The
account never goes negative in cash or oversells the available (T+1-adjusted)
quantity, and the whole account state can be recomputed from fills alone
(``recompute_from_ledger``) to audit the cached balances.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import duckdb

from etf_quant_lab.contracts.enums import OrderSide
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.paper import (
    PaperAccount,
    PaperAccountStatus,
    PaperFill,
    PaperFillSource,
    PaperOrder,
    PaperOrderStatus,
    PaperOrderType,
    order_idempotency_key,
)
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage.duckdb import DuckDBDatabase

PAPER_INSUFFICIENT_CASH = "PAPER_INSUFFICIENT_CASH"
PAPER_INSUFFICIENT_QUANTITY = "PAPER_INSUFFICIENT_QUANTITY"
PAPER_LEDGER_MISMATCH = "PAPER_LEDGER_MISMATCH"
PAPER_INVALID_STATE = "PAPER_INVALID_STATE"

_CASH_QUANTIZE = Decimal("0.0001")


class PaperAccountService:
    """Own the simulated account, order and fill lifecycle."""

    def __init__(
        self,
        database: DuckDBDatabase,
        id_generator: IdGenerator,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._id_generator = id_generator
        self._clock = clock or (lambda: datetime.now(UTC))

    # ---------------------------------------------------------------- account

    def create_account(self, *, name: str, initial_cash: Decimal) -> PaperAccount:
        """Create one ACTIVE account with its opening cash balance."""

        if initial_cash <= 0:
            raise DomainError(
                PAPER_INVALID_STATE,
                "初始资金必须为正",
                details={"initial_cash": str(initial_cash)},
            )
        now = self._clock()
        account_id = self._id_generator.new()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO paper_accounts (
                    account_id, name, base_currency, initial_cash, cash_balance,
                    status, version, created_at, updated_at
                )
                VALUES (?, ?, 'CNY', ?, ?, 'ACTIVE', 0, ?, ?)
                """,
                [account_id, name, initial_cash, initial_cash, now, now],
            )
        account = self.get_account(account_id)
        assert account is not None
        return account

    def get_account(self, account_id: str) -> PaperAccount | None:
        return self._account_from_row(self._fetch_account_row("account_id", account_id))

    def get_account_by_name(self, name: str) -> PaperAccount | None:
        """Return one account by its unique name, for idempotent reuse."""

        return self._account_from_row(self._fetch_account_row("name", name))

    def _fetch_account_row(self, column: str, value: str) -> tuple[object, ...] | None:
        with self._database.read_connection() as connection:
            return connection.execute(
                f"""
                SELECT account_id, name, base_currency, initial_cash, cash_balance,
                       status, version, created_at, updated_at
                FROM paper_accounts WHERE {column} = ?
                """,
                [value],
            ).fetchone()

    @staticmethod
    def _account_from_row(row: tuple[object, ...] | None) -> PaperAccount | None:
        if row is None:
            return None
        return PaperAccount(
            account_id=cast(str, row[0]),
            name=cast(str, row[1]),
            base_currency=cast(str, row[2]),
            initial_cash=cast(Decimal, row[3]),
            cash_balance=cast(Decimal, row[4]),
            status=PaperAccountStatus(cast(str, row[5])),
            version=cast(int, row[6]),
            created_at=cast(datetime, row[7]),
            updated_at=cast(datetime, row[8]),
        )

    # ----------------------------------------------------------------- orders

    def propose_order(
        self,
        *,
        account_id: str,
        symbol: str,
        side: OrderSide,
        quantity: int,
        order_type: PaperOrderType = PaperOrderType.MARKET_AT_NEXT_OPEN,
        signal_id: str | None = None,
        proposed_price: Decimal | None = None,
    ) -> PaperOrder:
        """Create a PROPOSED order; the same natural key returns the existing one."""

        if quantity <= 0:
            raise DomainError(
                PAPER_INVALID_STATE,
                "订单数量必须为正",
                details={"quantity": quantity},
            )
        key = order_idempotency_key(
            account_id=account_id, signal_id=signal_id, symbol=symbol, side=side
        )
        existing = self._find_order_by_key(key)
        if existing is not None:
            return existing
        now = self._clock()
        order_id = self._id_generator.new()
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO paper_orders (
                    order_id, account_id, signal_id, symbol, side, quantity,
                    order_type, status, idempotency_key, proposed_price,
                    created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'PROPOSED', ?, ?, ?, ?)
                """,
                [
                    order_id,
                    account_id,
                    signal_id,
                    symbol,
                    side.value,
                    quantity,
                    order_type.value,
                    key,
                    proposed_price,
                    now,
                    now,
                ],
            )
        order = self.get_order(order_id)
        assert order is not None
        return order

    def get_order(self, order_id: str) -> PaperOrder | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"SELECT {_ORDER_COLUMNS} FROM paper_orders WHERE order_id = ?",
                [order_id],
            ).fetchone()
        return None if row is None else _order_from_row(row)

    def _find_order_by_key(self, idempotency_key: str) -> PaperOrder | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"SELECT {_ORDER_COLUMNS} FROM paper_orders WHERE idempotency_key = ?",
                [idempotency_key],
            ).fetchone()
        return None if row is None else _order_from_row(row)

    # ------------------------------------------------------------------ fills

    def record_fill(
        self,
        *,
        order_id: str,
        trade_date: date,
        price: Decimal,
        commission: Decimal,
        source: PaperFillSource,
        slippage_cost: Decimal = Decimal(0),
        other_cost: Decimal = Decimal(0),
    ) -> PaperFill:
        """Fill one PROPOSED/CONFIRMED order atomically; repeat calls are no-ops.

        Cash, position, order status, and the fill row commit in a single
        transaction.  Buys become available T+1: the bought quantity stays in
        ``pending_quantity`` until ``settle_pending`` runs for a later date.
        """

        if price <= 0:
            raise DomainError(
                PAPER_INVALID_STATE, "成交价必须为正", details={"price": str(price)}
            )
        existing_fill = self._find_fill_by_order(order_id)
        if existing_fill is not None:
            return existing_fill

        now = self._clock()
        fill_id = self._id_generator.new()
        with self._database.transaction() as connection:
            order_row = connection.execute(
                f"SELECT {_ORDER_COLUMNS} FROM paper_orders WHERE order_id = ?",
                [order_id],
            ).fetchone()
            if order_row is None:
                raise DomainError(
                    PAPER_INVALID_STATE, "订单不存在", details={"order_id": order_id}
                )
            order = _order_from_row(order_row)
            if order.status not in {PaperOrderStatus.PROPOSED, PaperOrderStatus.CONFIRMED}:
                raise DomainError(
                    PAPER_INVALID_STATE,
                    "订单状态不允许成交",
                    details={"order_id": order_id, "status": order.status.value},
                )

            gross = (price * Decimal(order.quantity)).quantize(_CASH_QUANTIZE)
            fees = (commission + slippage_cost + other_cost).quantize(_CASH_QUANTIZE)
            cash_delta = -(gross + fees) if order.side == OrderSide.BUY else gross - fees

            cash_row = connection.execute(
                "SELECT cash_balance FROM paper_accounts WHERE account_id = ?",
                [order.account_id],
            ).fetchone()
            if cash_row is None:
                raise DomainError(
                    PAPER_INVALID_STATE,
                    "账户不存在",
                    details={"account_id": order.account_id},
                )
            cash_balance = cast(Decimal, cash_row[0])
            new_cash = (cash_balance + cash_delta).quantize(_CASH_QUANTIZE)
            if new_cash < 0:
                raise DomainError(
                    PAPER_INSUFFICIENT_CASH,
                    "模拟现金不足",
                    details={
                        "account_id": order.account_id,
                        "cash_balance": str(cash_balance),
                        "required": str(-cash_delta),
                    },
                )

            self._apply_position_change(
                connection,
                order,
                trade_date=trade_date,
                price=price,
                now=now,
            )
            connection.execute(
                """
                UPDATE paper_accounts
                SET cash_balance = ?, version = version + 1, updated_at = ?
                WHERE account_id = ?
                """,
                [new_cash, now, order.account_id],
            )
            connection.execute(
                "UPDATE paper_orders SET status = 'FILLED', updated_at = ? WHERE order_id = ?",
                [now, order_id],
            )
            connection.execute(
                """
                INSERT INTO paper_fills (
                    fill_id, order_id, trade_date, fill_time, quantity, price,
                    commission, slippage_cost, other_cost, cash_delta, source, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    fill_id,
                    order_id,
                    trade_date,
                    now,
                    order.quantity,
                    price,
                    commission,
                    slippage_cost,
                    other_cost,
                    cash_delta,
                    source.value,
                    now,
                ],
            )

        fill = self._find_fill_by_order(order_id)
        assert fill is not None
        return fill

    def _apply_position_change(
        self,
        connection: duckdb.DuckDBPyConnection,
        order: PaperOrder,
        *,
        trade_date: date,
        price: Decimal,
        now: datetime,
    ) -> None:
        row = connection.execute(
            """
            SELECT quantity, available_quantity, pending_quantity, pending_date, average_cost
            FROM paper_positions WHERE account_id = ? AND symbol = ?
            """,
            [order.account_id, order.symbol],
        ).fetchone()
        quantity = 0 if row is None else cast(int, row[0])
        available = 0 if row is None else cast(int, row[1])
        pending = 0 if row is None else cast(int, row[2])
        pending_date = None if row is None else cast(date | None, row[3])
        average_cost = Decimal(0) if row is None else cast(Decimal, row[4])

        # Pending shares bought before today become sellable first (T+1).
        if pending > 0 and pending_date is not None and pending_date < trade_date:
            available += pending
            pending = 0
            pending_date = None

        if order.side == OrderSide.BUY:
            total_cost = average_cost * Decimal(quantity) + price * Decimal(order.quantity)
            quantity += order.quantity
            pending += order.quantity
            pending_date = trade_date
            average_cost = total_cost / Decimal(quantity)
        else:
            if order.quantity > available:
                raise DomainError(
                    PAPER_INSUFFICIENT_QUANTITY,
                    "可卖数量不足(含 T+1 限制)",
                    details={
                        "symbol": order.symbol,
                        "available": available,
                        "requested": order.quantity,
                    },
                )
            quantity -= order.quantity
            available -= order.quantity
            if quantity == 0:
                average_cost = Decimal(0)

        connection.execute(
            """
            INSERT INTO paper_positions (
                account_id, symbol, quantity, available_quantity, pending_quantity,
                pending_date, average_cost, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (account_id, symbol) DO UPDATE SET
                quantity = EXCLUDED.quantity,
                available_quantity = EXCLUDED.available_quantity,
                pending_quantity = EXCLUDED.pending_quantity,
                pending_date = EXCLUDED.pending_date,
                average_cost = EXCLUDED.average_cost,
                updated_at = EXCLUDED.updated_at
            """,
            [
                order.account_id,
                order.symbol,
                quantity,
                available,
                pending,
                pending_date,
                average_cost,
                now,
            ],
        )

    def settle_pending(self, account_id: str, *, as_of: date) -> None:
        """Convert T+1 pending quantities bought before ``as_of`` into sellable."""

        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                """
                UPDATE paper_positions
                SET available_quantity = available_quantity + pending_quantity,
                    pending_quantity = 0,
                    pending_date = NULL,
                    updated_at = ?
                WHERE account_id = ? AND pending_quantity > 0 AND pending_date < ?
                """,
                [now, account_id, as_of],
            )

    def record_nav_snapshot(
        self,
        account_id: str,
        *,
        trade_date: date,
        close_prices: Mapping[str, Decimal],
    ) -> Decimal:
        """Value the account at the given closes and upsert one NAV snapshot.

        Held symbols missing a close raise loudly (no silent zero valuation).
        Re-running for the same date replaces the earlier snapshot, so the
        daily pipeline stays idempotent.  Returns the total equity.
        """

        account = self.get_account(account_id)
        if account is None:
            raise DomainError(
                PAPER_INVALID_STATE, "账户不存在", details={"account_id": account_id}
            )
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT symbol, quantity FROM paper_positions
                WHERE account_id = ? AND quantity > 0
                """,
                [account_id],
            ).fetchall()
        market_value = Decimal(0)
        for row in rows:
            symbol = cast(str, row[0])
            quantity = cast(int, row[1])
            price = close_prices.get(symbol)
            if price is None or price <= 0:
                raise DomainError(
                    PAPER_INVALID_STATE,
                    "持仓标的缺少有效收盘价, 无法估值",
                    details={"symbol": symbol, "trade_date": trade_date.isoformat()},
                )
            market_value += price * Decimal(quantity)
        market_value = market_value.quantize(_CASH_QUANTIZE)
        total_equity = (account.cash_balance + market_value).quantize(_CASH_QUANTIZE)

        now = self._clock()
        with self._database.transaction() as connection:
            connection.execute(
                "DELETE FROM nav_snapshots WHERE account_id = ? AND trade_date = ?",
                [account_id, trade_date],
            )
            connection.execute(
                """
                INSERT INTO nav_snapshots (
                    snapshot_id, account_id, trade_date, cash, market_value,
                    total_equity, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    self._id_generator.new(),
                    account_id,
                    trade_date,
                    account.cash_balance,
                    market_value,
                    total_equity,
                    now,
                ],
            )
        return total_equity

    def list_nav_snapshots(
        self,
        account_id: str,
    ) -> tuple[tuple[date, Decimal, Decimal, Decimal], ...]:
        """Return (trade_date, cash, market_value, total_equity) ascending."""

        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT trade_date, cash, market_value, total_equity
                FROM nav_snapshots WHERE account_id = ?
                ORDER BY trade_date
                """,
                [account_id],
            ).fetchall()
        return tuple(
            (
                cast(date, row[0]),
                cast(Decimal, row[1]),
                cast(Decimal, row[2]),
                cast(Decimal, row[3]),
            )
            for row in rows
        )

    def _find_fill_by_order(self, order_id: str) -> PaperFill | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT fill_id, order_id, trade_date, fill_time, quantity, price,
                       commission, slippage_cost, other_cost, cash_delta, source
                FROM paper_fills WHERE order_id = ?
                """,
                [order_id],
            ).fetchone()
        if row is None:
            return None
        return PaperFill(
            fill_id=cast(str, row[0]),
            order_id=cast(str, row[1]),
            trade_date=cast(date, row[2]),
            fill_time=cast(datetime, row[3]),
            quantity=cast(int, row[4]),
            price=cast(Decimal, row[5]),
            commission=cast(Decimal, row[6]),
            slippage_cost=cast(Decimal, row[7]),
            other_cost=cast(Decimal, row[8]),
            cash_delta=cast(Decimal, row[9]),
            source=PaperFillSource(cast(str, row[10])),
        )

    # ------------------------------------------------------------------ audit

    def recompute_from_ledger(self, account_id: str) -> tuple[Decimal, dict[str, int]]:
        """Recompute cash and positions purely from fills, for reconciliation."""

        account = self.get_account(account_id)
        if account is None:
            raise DomainError(
                PAPER_INVALID_STATE, "账户不存在", details={"account_id": account_id}
            )
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT orders.symbol, orders.side, fills.quantity, fills.cash_delta
                FROM paper_fills AS fills
                JOIN paper_orders AS orders ON orders.order_id = fills.order_id
                WHERE orders.account_id = ?
                ORDER BY fills.fill_time, fills.fill_id
                """,
                [account_id],
            ).fetchall()
        cash = account.initial_cash
        positions: dict[str, int] = {}
        for row in rows:
            symbol = cast(str, row[0])
            side = OrderSide(cast(str, row[1]))
            quantity = cast(int, row[2])
            cash = (cash + cast(Decimal, row[3])).quantize(_CASH_QUANTIZE)
            delta = quantity if side == OrderSide.BUY else -quantity
            positions[symbol] = positions.get(symbol, 0) + delta
        positions = {symbol: qty for symbol, qty in positions.items() if qty != 0}
        return cash, positions

    def verify_ledger(self, account_id: str) -> None:
        """Freeze the account when cached balances disagree with the ledger."""

        recomputed_cash, recomputed_positions = self.recompute_from_ledger(account_id)
        account = self.get_account(account_id)
        assert account is not None
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT symbol, quantity
                FROM paper_positions
                WHERE account_id = ? AND quantity > 0
                """,
                [account_id],
            ).fetchall()
        cached_positions = {cast(str, row[0]): cast(int, row[1]) for row in rows}

        if recomputed_cash != account.cash_balance or recomputed_positions != cached_positions:
            now = self._clock()
            with self._database.transaction() as connection:
                connection.execute(
                    """
                    UPDATE paper_accounts
                    SET status = 'FROZEN', updated_at = ?
                    WHERE account_id = ?
                    """,
                    [now, account_id],
                )
            raise DomainError(
                PAPER_LEDGER_MISMATCH,
                "模拟账本不平, 账户已冻结",
                details={
                    "account_id": account_id,
                    "cached_cash": str(account.cash_balance),
                    "ledger_cash": str(recomputed_cash),
                },
            )


_ORDER_COLUMNS = (
    "order_id, account_id, signal_id, symbol, side, quantity, order_type, "
    "status, idempotency_key, proposed_price, reject_reason, created_at, updated_at"
)


def _order_from_row(row: tuple[object, ...]) -> PaperOrder:
    return PaperOrder(
        order_id=cast(str, row[0]),
        account_id=cast(str, row[1]),
        signal_id=cast(str | None, row[2]),
        symbol=cast(str, row[3]),
        side=OrderSide(cast(str, row[4])),
        quantity=cast(int, row[5]),
        order_type=PaperOrderType(cast(str, row[6])),
        status=PaperOrderStatus(cast(str, row[7])),
        idempotency_key=cast(str, row[8]),
        proposed_price=cast(Decimal | None, row[9]),
        reject_reason=cast(str | None, row[10]),
        created_at=cast(datetime, row[11]),
        updated_at=cast(datetime, row[12]),
    )
