"""DuckDB repositories for the node-5 market-data storage boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import cast

import duckdb

from etf_quant_lab.contracts.data import DailyBar, DataBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataLayer,
    DataSource,
    Exchange,
    InstrumentStatus,
    InstrumentType,
)
from etf_quant_lab.domain.market import EtfInstrument, TradingCalendarDay
from etf_quant_lab.storage._json import decode_json as _decode_json
from etf_quant_lab.storage._json import encode_json as _encode_json
from etf_quant_lab.storage.duckdb import DuckDBDatabase
from etf_quant_lab.storage.parquet import ParquetArtifact, ParquetStore


class InvalidBatchTransitionError(RuntimeError):
    """Raised when a batch lifecycle transition is not allowed."""


@dataclass(frozen=True, slots=True)
class StoredDataFile:
    """One immutable file-manifest entry persisted in DuckDB."""

    batch_id: str
    layer: DataLayer
    dataset: str
    relative_path: str
    checksum: str
    row_count: int
    schema_version: str
    min_trade_date: date | None
    max_trade_date: date | None

    def as_artifact(self) -> ParquetArtifact:
        return ParquetArtifact(
            batch_id=self.batch_id,
            layer=self.layer,
            dataset=self.dataset,
            relative_path=self.relative_path,
            checksum=self.checksum,
            row_count=self.row_count,
            schema_version=self.schema_version,
            min_trade_date=self.min_trade_date,
            max_trade_date=self.max_trade_date,
        )


class DuckDBInstrumentRepository:
    """DuckDB implementation of the ETF instrument repository protocol."""

    def __init__(
        self,
        database: DuckDBDatabase,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, symbol: str) -> EtfInstrument | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    symbol, name, exchange, asset_class, status, category,
                    benchmark_symbol, lot_size, price_tick, list_date, delist_date,
                    active, metadata_version, metadata
                FROM instruments
                WHERE symbol = ?
                """,
                [symbol],
            ).fetchone()
        return None if row is None else _instrument_from_row(row)

    def list_all(self) -> tuple[EtfInstrument, ...]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    symbol, name, exchange, asset_class, status, category,
                    benchmark_symbol, lot_size, price_tick, list_date, delist_date,
                    active, metadata_version, metadata
                FROM instruments
                ORDER BY symbol
                """
            ).fetchall()
        return tuple(_instrument_from_row(row) for row in rows)

    def upsert_many(self, instruments: tuple[EtfInstrument, ...]) -> None:
        if not instruments:
            return
        now = self._clock()
        parameters = [
            [
                instrument.symbol,
                instrument.name,
                instrument.exchange.value,
                instrument.instrument_type.value,
                instrument.status.value,
                instrument.category,
                instrument.benchmark_symbol,
                instrument.lot_size,
                instrument.price_tick,
                instrument.list_date,
                instrument.delist_date,
                instrument.enabled,
                instrument.metadata_version,
                _encode_json(instrument.metadata),
                now,
                now,
            ]
            for instrument in instruments
        ]
        with self._database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO instruments (
                    symbol, name, exchange, asset_class, status, category,
                    benchmark_symbol, lot_size, price_tick, list_date, delist_date,
                    active, metadata_version, metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (symbol) DO UPDATE SET
                    name = EXCLUDED.name,
                    exchange = EXCLUDED.exchange,
                    asset_class = EXCLUDED.asset_class,
                    status = EXCLUDED.status,
                    category = EXCLUDED.category,
                    benchmark_symbol = EXCLUDED.benchmark_symbol,
                    lot_size = EXCLUDED.lot_size,
                    price_tick = EXCLUDED.price_tick,
                    list_date = EXCLUDED.list_date,
                    delist_date = EXCLUDED.delist_date,
                    active = EXCLUDED.active,
                    metadata_version = EXCLUDED.metadata_version,
                    metadata = EXCLUDED.metadata,
                    updated_at = EXCLUDED.updated_at
                """,
                parameters,
            )


class DuckDBTradingCalendarRepository:
    """DuckDB implementation of the exchange trading-calendar repository protocol."""

    def __init__(self, database: DuckDBDatabase) -> None:
        self._database = database

    def get_day(self, exchange: Exchange, cal_date: date) -> TradingCalendarDay | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    exchange, cal_date, is_open, previous_open_date, next_open_date,
                    source, batch_id, updated_at
                FROM trading_calendar
                WHERE exchange = ? AND cal_date = ?
                """,
                [exchange.value, cal_date],
            ).fetchone()
        return None if row is None else _calendar_day_from_row(row)

    def list_days(
        self,
        exchange: Exchange,
        start_date: date,
        end_date: date,
    ) -> tuple[TradingCalendarDay, ...]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    exchange, cal_date, is_open, previous_open_date, next_open_date,
                    source, batch_id, updated_at
                FROM trading_calendar
                WHERE exchange = ? AND cal_date BETWEEN ? AND ?
                ORDER BY cal_date
                """,
                [exchange.value, start_date, end_date],
            ).fetchall()
        return tuple(_calendar_day_from_row(row) for row in rows)

    def next_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        operator = ">=" if inclusive else ">"
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT min(cal_date)
                FROM trading_calendar
                WHERE exchange = ? AND is_open AND cal_date {operator} ?
                """,
                [exchange.value, reference_date],
            ).fetchone()
        return None if row is None or row[0] is None else cast(date, row[0])

    def previous_open_date(
        self,
        exchange: Exchange,
        reference_date: date,
        *,
        inclusive: bool,
    ) -> date | None:
        operator = "<=" if inclusive else "<"
        with self._database.read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT max(cal_date)
                FROM trading_calendar
                WHERE exchange = ? AND is_open AND cal_date {operator} ?
                """,
                [exchange.value, reference_date],
            ).fetchone()
        return None if row is None or row[0] is None else cast(date, row[0])

    def upsert_many(self, days: tuple[TradingCalendarDay, ...]) -> None:
        if not days:
            return
        parameters = [
            [
                day.exchange.value,
                day.cal_date,
                day.is_open,
                day.previous_open_date,
                day.next_open_date,
                day.source.value,
                day.batch_id,
                day.updated_at,
            ]
            for day in days
        ]
        with self._database.transaction() as connection:
            connection.executemany(
                """
                INSERT INTO trading_calendar (
                    exchange, cal_date, is_open, previous_open_date, next_open_date,
                    source, batch_id, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (exchange, cal_date) DO UPDATE SET
                    is_open = EXCLUDED.is_open,
                    previous_open_date = EXCLUDED.previous_open_date,
                    next_open_date = EXCLUDED.next_open_date,
                    source = EXCLUDED.source,
                    batch_id = EXCLUDED.batch_id,
                    updated_at = EXCLUDED.updated_at
                """,
                parameters,
            )


class DataBatchRepository:
    """Persist batch metadata, verified file manifests and the active bars view."""

    def __init__(self, database: DuckDBDatabase, parquet_store: ParquetStore) -> None:
        self._database = database
        self._parquet_store = parquet_store

    def create(
        self,
        batch: DataBatch,
        *,
        requested_start: date | None = None,
        requested_end: date | None = None,
    ) -> None:
        if batch.status != DataBatchStatus.FETCHING:
            raise InvalidBatchTransitionError("a new batch must start in FETCHING")
        with self._database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO data_batches (
                    batch_id, provider, dataset, requested_start, requested_end,
                    fetched_at, status, row_count, file_count, checksum,
                    schema_version, parent_batch_id, error_summary, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    batch.batch_id,
                    batch.provider.value,
                    batch.dataset,
                    requested_start,
                    requested_end,
                    batch.fetched_at,
                    batch.status.value,
                    batch.row_count,
                    batch.file_count,
                    batch.checksum,
                    batch.schema_version,
                    batch.parent_batch_id,
                    batch.error_summary,
                    _encode_json(batch.metadata),
                ],
            )

    def get(self, batch_id: str) -> DataBatch | None:
        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT
                    batch_id, provider, dataset, status, fetched_at, schema_version,
                    row_count, file_count, checksum, parent_batch_id,
                    error_summary, metadata
                FROM data_batches
                WHERE batch_id = ?
                """,
                [batch_id],
            ).fetchone()
        return None if row is None else _data_batch_from_row(row)

    def list_recent(self, *, limit: int = 20) -> tuple[DataBatch, ...]:
        """Return the most recently fetched batches for the data-center page."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    batch_id, provider, dataset, status, fetched_at, schema_version,
                    row_count, file_count, checksum, parent_batch_id,
                    error_summary, metadata
                FROM data_batches
                ORDER BY fetched_at DESC, batch_id DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()
        return tuple(_data_batch_from_row(row) for row in rows)

    def stage_files(
        self,
        batch_id: str,
        artifacts: tuple[ParquetArtifact, ...],
    ) -> DataBatch:
        """Verify and register files, then move the batch to VALIDATING atomically."""

        if not artifacts:
            raise ValueError("artifacts must not be empty")
        if any(artifact.batch_id != batch_id for artifact in artifacts):
            raise ValueError("all artifacts must belong to the staged batch")
        for artifact in artifacts:
            self._parquet_store.verify(artifact)

        with self._database.transaction() as connection:
            current = _required_batch_status(connection, batch_id)
            if current not in {DataBatchStatus.FETCHING, DataBatchStatus.VALIDATING}:
                raise InvalidBatchTransitionError(
                    f"cannot stage files while batch is {current.value}"
                )
            for artifact in artifacts:
                self._insert_or_verify_file(connection, artifact)

            stored_files = _stored_files_for_batch(connection, batch_id)
            canonical_rows = sum(
                item.row_count for item in stored_files if item.layer == DataLayer.CANONICAL
            )
            total_rows = canonical_rows or sum(
                item.row_count for item in stored_files if item.layer == DataLayer.RAW
            )
            manifest_checksum = _manifest_checksum(stored_files)
            connection.execute(
                """
                UPDATE data_batches
                SET status = ?, row_count = ?, file_count = ?, checksum = ?, error_summary = NULL
                WHERE batch_id = ?
                """,
                [
                    DataBatchStatus.VALIDATING.value,
                    total_rows,
                    len(stored_files),
                    manifest_checksum,
                    batch_id,
                ],
            )
        staged = self.get(batch_id)
        if staged is None:
            raise RuntimeError(f"staged batch disappeared: {batch_id}")
        return staged

    def activate(
        self,
        batch_id: str,
        *,
        supersede_batch_ids: tuple[str, ...] = (),
    ) -> DataBatch:
        """Activate one verified batch and refresh ``v_daily_bars`` in one transaction."""

        if batch_id in supersede_batch_ids:
            raise ValueError("a batch cannot supersede itself")
        if len(set(supersede_batch_ids)) != len(supersede_batch_ids):
            raise ValueError("supersede_batch_ids must not contain duplicates")

        for stored_file in self.list_files(batch_id):
            self._parquet_store.verify(stored_file.as_artifact())

        with self._database.transaction() as connection:
            target_row = connection.execute(
                "SELECT status, dataset FROM data_batches WHERE batch_id = ?",
                [batch_id],
            ).fetchone()
            if target_row is None:
                raise KeyError(f"data batch not found: {batch_id}")
            target_status = DataBatchStatus(cast(str, target_row[0]))
            target_dataset = cast(str, target_row[1])
            if target_status == DataBatchStatus.ACTIVE and not supersede_batch_ids:
                self._refresh_daily_bars_view(connection)
            else:
                if target_status != DataBatchStatus.VALIDATING:
                    raise InvalidBatchTransitionError(
                        f"cannot activate batch while it is {target_status.value}"
                    )
                canonical_count_row = connection.execute(
                    """
                    SELECT count(*)
                    FROM data_files
                    WHERE batch_id = ? AND layer = 'CANONICAL'
                    """,
                    [batch_id],
                ).fetchone()
                if canonical_count_row is None:
                    canonical_count = 0
                else:
                    canonical_count = cast(int, canonical_count_row[0])
                if target_dataset == "daily_bars" and canonical_count == 0:
                    raise InvalidBatchTransitionError(
                        "daily_bars batch cannot activate without canonical files"
                    )

                for old_batch_id in supersede_batch_ids:
                    old_row = connection.execute(
                        "SELECT status, dataset FROM data_batches WHERE batch_id = ?",
                        [old_batch_id],
                    ).fetchone()
                    if old_row is None:
                        raise KeyError(f"data batch not found: {old_batch_id}")
                    old_status = DataBatchStatus(cast(str, old_row[0]))
                    old_dataset = cast(str, old_row[1])
                    if old_status != DataBatchStatus.ACTIVE:
                        raise InvalidBatchTransitionError(
                            f"batch to supersede is not ACTIVE: {old_batch_id}"
                        )
                    if old_dataset != target_dataset:
                        raise InvalidBatchTransitionError(
                            "a batch can only supersede another batch from the same dataset"
                        )

                if supersede_batch_ids:
                    placeholders = ", ".join("?" for _ in supersede_batch_ids)
                    connection.execute(
                        f"""
                        UPDATE data_batches
                        SET status = ?
                        WHERE batch_id IN ({placeholders})
                        """,
                        [DataBatchStatus.SUPERSEDED.value, *supersede_batch_ids],
                    )
                connection.execute(
                    "UPDATE data_batches SET status = ? WHERE batch_id = ?",
                    [DataBatchStatus.ACTIVE.value, batch_id],
                )
                self._refresh_daily_bars_view(connection)

        active = self.get(batch_id)
        if active is None:
            raise RuntimeError(f"activated batch disappeared: {batch_id}")
        return active

    def reject(self, batch_id: str, error_summary: str) -> DataBatch:
        if not error_summary.strip():
            raise ValueError("error_summary must not be blank")
        with self._database.transaction() as connection:
            current = _required_batch_status(connection, batch_id)
            if current not in {DataBatchStatus.FETCHING, DataBatchStatus.VALIDATING}:
                raise InvalidBatchTransitionError(
                    f"cannot reject batch while it is {current.value}"
                )
            connection.execute(
                "UPDATE data_batches SET status = ?, error_summary = ? WHERE batch_id = ?",
                [DataBatchStatus.REJECTED.value, error_summary, batch_id],
            )
        rejected = self.get(batch_id)
        if rejected is None:
            raise RuntimeError(f"rejected batch disappeared: {batch_id}")
        return rejected

    def list_files(self, batch_id: str) -> tuple[StoredDataFile, ...]:
        with self._database.read_connection() as connection:
            return _stored_files_for_batch(connection, batch_id)

    def list_active_daily_bar_files(self) -> tuple[StoredDataFile, ...]:
        with self._database.read_connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    batch_id, 'CANONICAL' AS layer, 'daily_bars' AS dataset,
                    file_path, checksum, row_count, schema_version,
                    min_trade_date, max_trade_date
                FROM v_active_daily_bar_files
                ORDER BY batch_id, file_path
                """
            ).fetchall()
        return tuple(_stored_file_from_row(row) for row in rows)

    def active_daily_bar_manifest_checksum(self) -> str:
        return _manifest_checksum(self.list_active_daily_bar_files())

    def query_daily_bars(
        self,
        *,
        symbols: tuple[str, ...] = (),
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> tuple[DailyBar, ...]:
        conditions: list[str] = []
        parameters: list[object] = []
        if symbols:
            placeholders = ", ".join("?" for _ in symbols)
            conditions.append(f"symbol IN ({placeholders})")
            parameters.extend(symbols)
        if start_date is not None:
            conditions.append("trade_date >= ?")
            parameters.append(start_date)
        if end_date is not None:
            conditions.append("trade_date <= ?")
            parameters.append(end_date)
        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    symbol, trade_date, exchange, open, high, low, close,
                    volume, amount, source, batch_id, ingested_at,
                    pre_close, adj_factor, is_suspended
                FROM v_daily_bars
                {where_clause}
                ORDER BY trade_date, symbol
                """,
                parameters,
            ).fetchall()
        return tuple(_daily_bar_from_row(row) for row in rows)

    def query_daily_bars_for_batch(self, batch_id: str) -> tuple[DailyBar, ...]:
        """Read one batch's canonical bars directly, before it becomes ACTIVE.

        The quality gate runs while a batch is still VALIDATING, so the bars are
        read from that batch's own verified files rather than from
        ``v_daily_bars``, which only exposes the active snapshot.
        """

        stored_files = tuple(
            stored
            for stored in self.list_files(batch_id)
            if stored.layer == DataLayer.CANONICAL and stored.dataset == "daily_bars"
        )
        if not stored_files:
            return ()
        for stored in stored_files:
            self._parquet_store.verify(stored.as_artifact())
        paths = [
            self._parquet_store.resolve(stored.relative_path).as_posix()
            for stored in stored_files
        ]
        quoted_paths = ", ".join(f"'{path.replace("'", "''")}'" for path in paths)
        with self._database.read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    symbol, trade_date, exchange, open, high, low, close,
                    volume, amount, source, batch_id, ingested_at,
                    pre_close, adj_factor, is_suspended
                FROM read_parquet(
                    [{quoted_paths}],
                    hive_partitioning = false,
                    union_by_name = true
                )
                WHERE batch_id = ?
                ORDER BY trade_date, symbol
                """,
                [batch_id],
            ).fetchall()
        return tuple(_daily_bar_from_row(row) for row in rows)

    def refresh_daily_bars_view(self) -> None:
        with self._database.transaction() as connection:
            self._refresh_daily_bars_view(connection)

    @staticmethod
    def _insert_or_verify_file(
        connection: duckdb.DuckDBPyConnection,
        artifact: ParquetArtifact,
    ) -> None:
        existing = connection.execute(
            """
            SELECT checksum, row_count, schema_version, min_trade_date, max_trade_date, dataset
            FROM data_files
            WHERE batch_id = ? AND layer = ? AND file_path = ?
            """,
            [artifact.batch_id, artifact.layer.value, artifact.relative_path],
        ).fetchone()
        expected = (
            artifact.checksum,
            artifact.row_count,
            artifact.schema_version,
            artifact.min_trade_date,
            artifact.max_trade_date,
            artifact.dataset,
        )
        if existing is not None:
            if tuple(existing) != expected:
                raise RuntimeError(
                    f"manifest conflict for {artifact.batch_id}: {artifact.relative_path}"
                )
            return
        connection.execute(
            """
            INSERT INTO data_files (
                batch_id, layer, dataset, file_path, checksum, row_count,
                schema_version, min_trade_date, max_trade_date
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                artifact.batch_id,
                artifact.layer.value,
                artifact.dataset,
                artifact.relative_path,
                artifact.checksum,
                artifact.row_count,
                artifact.schema_version,
                artifact.min_trade_date,
                artifact.max_trade_date,
            ],
        )

    def _refresh_daily_bars_view(self, connection: duckdb.DuckDBPyConnection) -> None:
        rows = connection.execute(
            """
            SELECT file_path
            FROM v_active_daily_bar_files
            ORDER BY batch_id, file_path
            """
        ).fetchall()
        if not rows:
            connection.execute(_EMPTY_DAILY_BARS_VIEW_SQL)
            return

        paths = [self._parquet_store.resolve(cast(str, row[0])).as_posix() for row in rows]
        quoted_paths = ", ".join(f"'{path.replace("'", "''")}'" for path in paths)
        connection.execute(
            f"""
            CREATE OR REPLACE VIEW v_daily_bars AS
            WITH ranked AS (
                SELECT
                    bars.symbol,
                    bars.trade_date,
                    bars.exchange,
                    bars.open,
                    bars.high,
                    bars.low,
                    bars.close,
                    bars.pre_close,
                    bars.volume,
                    bars.amount,
                    bars.adj_factor,
                    bars.is_suspended,
                    bars.source,
                    bars.batch_id,
                    bars.ingested_at,
                    row_number() OVER (
                        PARTITION BY bars.symbol, bars.trade_date, bars.source
                        ORDER BY batches.fetched_at DESC, bars.batch_id DESC
                    ) AS active_rank
                FROM read_parquet(
                    [{quoted_paths}],
                    hive_partitioning = false,
                    union_by_name = true
                ) AS bars
                JOIN data_batches AS batches ON batches.batch_id = bars.batch_id
                WHERE batches.status = 'ACTIVE'
            )
            SELECT
                symbol,
                trade_date,
                exchange,
                open,
                high,
                low,
                close,
                pre_close,
                volume,
                amount,
                adj_factor,
                is_suspended,
                source,
                batch_id,
                ingested_at
            FROM ranked
            WHERE active_rank = 1
            """
        )


_EMPTY_DAILY_BARS_VIEW_SQL = """
CREATE OR REPLACE VIEW v_daily_bars AS
SELECT
    CAST(NULL AS VARCHAR) AS symbol,
    CAST(NULL AS DATE) AS trade_date,
    CAST(NULL AS VARCHAR) AS exchange,
    CAST(NULL AS DECIMAL(20, 6)) AS open,
    CAST(NULL AS DECIMAL(20, 6)) AS high,
    CAST(NULL AS DECIMAL(20, 6)) AS low,
    CAST(NULL AS DECIMAL(20, 6)) AS close,
    CAST(NULL AS DECIMAL(20, 6)) AS pre_close,
    CAST(NULL AS DECIMAL(24, 4)) AS volume,
    CAST(NULL AS DECIMAL(24, 4)) AS amount,
    CAST(NULL AS DECIMAL(20, 8)) AS adj_factor,
    CAST(NULL AS BOOLEAN) AS is_suspended,
    CAST(NULL AS VARCHAR) AS source,
    CAST(NULL AS VARCHAR) AS batch_id,
    CAST(NULL AS TIMESTAMPTZ) AS ingested_at
WHERE FALSE
"""


def _required_batch_status(
    connection: duckdb.DuckDBPyConnection,
    batch_id: str,
) -> DataBatchStatus:
    row = connection.execute(
        "SELECT status FROM data_batches WHERE batch_id = ?",
        [batch_id],
    ).fetchone()
    if row is None:
        raise KeyError(f"data batch not found: {batch_id}")
    return DataBatchStatus(cast(str, row[0]))


def _stored_files_for_batch(
    connection: duckdb.DuckDBPyConnection,
    batch_id: str,
) -> tuple[StoredDataFile, ...]:
    rows = connection.execute(
        """
        SELECT
            batch_id, layer, dataset, file_path, checksum, row_count,
            schema_version, min_trade_date, max_trade_date
        FROM data_files
        WHERE batch_id = ?
        ORDER BY layer, file_path
        """,
        [batch_id],
    ).fetchall()
    return tuple(_stored_file_from_row(row) for row in rows)


def _manifest_checksum(files: tuple[StoredDataFile, ...]) -> str:
    digest = hashlib.sha256()
    ordered = sorted(files, key=lambda item: (item.batch_id, item.layer.value, item.relative_path))
    for item in ordered:
        digest.update(
            (
                f"{item.batch_id}|{item.layer.value}|{item.dataset}|{item.relative_path}|"
                f"{item.checksum}|{item.row_count}|{item.schema_version}|"
                f"{item.min_trade_date}|{item.max_trade_date}\n"
            ).encode()
        )
    return digest.hexdigest()


def _instrument_from_row(row: tuple[object, ...]) -> EtfInstrument:
    return EtfInstrument(
        symbol=cast(str, row[0]),
        name=cast(str, row[1]),
        exchange=Exchange(cast(str, row[2])),
        instrument_type=InstrumentType(cast(str, row[3])),
        status=InstrumentStatus(cast(str, row[4])),
        category=cast(str, row[5]),
        benchmark_symbol=cast(str | None, row[6]),
        lot_size=cast(int, row[7]),
        price_tick=cast(Decimal, row[8]),
        list_date=cast(date | None, row[9]),
        delist_date=cast(date | None, row[10]),
        enabled=cast(bool, row[11]),
        metadata_version=cast(str, row[12]),
        metadata=_decode_json(row[13]),
    )


def _calendar_day_from_row(row: tuple[object, ...]) -> TradingCalendarDay:
    return TradingCalendarDay(
        exchange=Exchange(cast(str, row[0])),
        cal_date=cast(date, row[1]),
        is_open=cast(bool, row[2]),
        previous_open_date=cast(date | None, row[3]),
        next_open_date=cast(date | None, row[4]),
        source=DataSource(cast(str, row[5])),
        batch_id=cast(str, row[6]),
        updated_at=cast(datetime, row[7]),
    )


def _data_batch_from_row(row: tuple[object, ...]) -> DataBatch:
    return DataBatch(
        batch_id=cast(str, row[0]),
        provider=DataSource(cast(str, row[1])),
        dataset=cast(str, row[2]),
        status=DataBatchStatus(cast(str, row[3])),
        fetched_at=cast(datetime, row[4]),
        schema_version=cast(str, row[5]),
        row_count=cast(int, row[6]),
        file_count=cast(int, row[7]),
        checksum=cast(str | None, row[8]),
        parent_batch_id=cast(str | None, row[9]),
        error_summary=cast(str | None, row[10]),
        metadata=_decode_json(row[11]),
    )


def _stored_file_from_row(row: tuple[object, ...]) -> StoredDataFile:
    return StoredDataFile(
        batch_id=cast(str, row[0]),
        layer=DataLayer(cast(str, row[1])),
        dataset=cast(str, row[2]),
        relative_path=cast(str, row[3]),
        checksum=cast(str, row[4]),
        row_count=cast(int, row[5]),
        schema_version=cast(str, row[6]),
        min_trade_date=cast(date | None, row[7]),
        max_trade_date=cast(date | None, row[8]),
    )


def _daily_bar_from_row(row: tuple[object, ...]) -> DailyBar:
    return DailyBar(
        symbol=cast(str, row[0]),
        trade_date=cast(date, row[1]),
        exchange=Exchange(cast(str, row[2])),
        open=cast(Decimal, row[3]),
        high=cast(Decimal, row[4]),
        low=cast(Decimal, row[5]),
        close=cast(Decimal, row[6]),
        volume=cast(Decimal, row[7]),
        amount=cast(Decimal, row[8]),
        source=DataSource(cast(str, row[9])),
        batch_id=cast(str, row[10]),
        ingested_at=cast(datetime, row[11]),
        pre_close=cast(Decimal | None, row[12]),
        adj_factor=cast(Decimal | None, row[13]),
        is_suspended=cast(bool, row[14]),
    )
