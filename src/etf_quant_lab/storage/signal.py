"""DuckDB persistence for daily signals and their target positions."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import cast

from etf_quant_lab.contracts.enums import RiskState, SignalAction, StrategyId
from etf_quant_lab.contracts.signal import DailySignalBatch, SignalItem
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage.duckdb import DuckDBDatabase


class SignalRepository:
    """Persist signal batches; the idempotency key is unique at the schema level."""

    def __init__(self, database: DuckDBDatabase, id_generator: IdGenerator) -> None:
        self._database = database
        self._id_generator = id_generator

    def save(self, batch: DailySignalBatch) -> None:
        """Insert one batch and its items in a single transaction."""

        item_rows = [
            [
                self._id_generator.new(),
                batch.signal_id,
                item.symbol,
                item.action.value,
                item.current_weight,
                item.target_weight,
                item.weight_delta,
                item.reference_close,
                item.score,
                json.dumps(list(item.reason_codes), ensure_ascii=False),
            ]
            for item in batch.items
        ]
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO signals (
                    signal_id, trade_date, generated_at, strategy_id, strategy_version,
                    parameter_hash, universe_hash, idempotency_key, status, risk_state,
                    target_cash_weight, data_as_of, dataset_id, quality_report_id,
                    blocked_reason, warnings
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    batch.signal_id,
                    batch.trade_date,
                    batch.generated_at,
                    batch.strategy_id.value,
                    batch.strategy_version,
                    batch.parameter_hash,
                    batch.universe_hash,
                    batch.idempotency_key,
                    batch.status,
                    batch.risk_state.value,
                    batch.target_cash_weight,
                    batch.data_as_of,
                    batch.dataset_id,
                    batch.quality_report_id,
                    batch.blocked_reason,
                    json.dumps(list(batch.warnings), ensure_ascii=False),
                ],
            )
            if item_rows:
                connection.executemany(
                    """
                    INSERT INTO target_positions (
                        target_id, signal_id, symbol, action, current_weight,
                        target_weight, weight_delta, reference_close, score, reason_codes
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    item_rows,
                )

    def get(self, signal_id: str) -> DailySignalBatch | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM signals WHERE signal_id = ?",
                [signal_id],
            ).fetchone()
            if row is None:
                return None
            items = self._items_for(connection, signal_id)
        return _batch_from_row(row, items)

    def find_by_idempotency_key(self, idempotency_key: str) -> DailySignalBatch | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"SELECT {_SIGNAL_COLUMNS} FROM signals WHERE idempotency_key = ?",
                [idempotency_key],
            ).fetchone()
            if row is None:
                return None
            items = self._items_for(connection, cast(str, row[0]))
        return _batch_from_row(row, items)

    def find_latest_for_date(self, trade_date: date) -> DailySignalBatch | None:
        """Return the most recently generated batch for one trade date."""

        with self._database.read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_SIGNAL_COLUMNS} FROM signals
                WHERE trade_date = ?
                ORDER BY generated_at DESC, signal_id DESC
                LIMIT 1
                """,
                [trade_date],
            ).fetchone()
            if row is None:
                return None
            items = self._items_for(connection, cast(str, row[0]))
        return _batch_from_row(row, items)

    @staticmethod
    def _items_for(connection: object, signal_id: str) -> tuple[SignalItem, ...]:
        rows = connection.execute(  # type: ignore[attr-defined]
            """
            SELECT symbol, action, current_weight, target_weight, weight_delta,
                   reference_close, score, reason_codes
            FROM target_positions
            WHERE signal_id = ?
            ORDER BY symbol
            """,
            [signal_id],
        ).fetchall()
        return tuple(_item_from_row(row) for row in rows)


_SIGNAL_COLUMNS = (
    "signal_id, trade_date, generated_at, strategy_id, strategy_version, "
    "parameter_hash, universe_hash, idempotency_key, status, risk_state, "
    "target_cash_weight, data_as_of, dataset_id, quality_report_id, "
    "blocked_reason, warnings"
)


def _item_from_row(row: tuple[object, ...]) -> SignalItem:
    return SignalItem(
        symbol=cast(str, row[0]),
        action=SignalAction(cast(str, row[1])),
        current_weight=cast(Decimal, row[2]),
        target_weight=cast(Decimal, row[3]),
        weight_delta=cast(Decimal, row[4]),
        reference_close=cast(Decimal | None, row[5]),
        score=cast(Decimal | None, row[6]),
        reason_codes=tuple(json.loads(cast(str, row[7]))),
    )


def _batch_from_row(
    row: tuple[object, ...],
    items: tuple[SignalItem, ...],
) -> DailySignalBatch:
    return DailySignalBatch(
        signal_id=cast(str, row[0]),
        trade_date=cast(date, row[1]),
        generated_at=cast(datetime, row[2]),
        strategy_id=StrategyId(cast(str, row[3])),
        strategy_version=cast(str, row[4]),
        parameter_hash=cast(str, row[5]),
        universe_hash=cast(str, row[6]),
        idempotency_key=cast(str, row[7]),
        status=cast(str, row[8]),
        risk_state=RiskState(cast(str, row[9])),
        items=items,
        target_cash_weight=cast(Decimal, row[10]),
        data_as_of=cast(date, row[11]),
        dataset_id=cast(str | None, row[12]),
        quality_report_id=cast(str | None, row[13]),
        blocked_reason=cast(str | None, row[14]),
        warnings=tuple(json.loads(cast(str, row[15]))),
    )
