"""DuckDB persistence for versioned strategy definitions."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.storage._json import decode_json, encode_json
from etf_quant_lab.storage.duckdb import DuckDBDatabase


class StrategyDefinitionRepository:
    """Persist immutable ``(strategy_key, version)`` rows with their code hash.

    Registration is idempotent: re-registering an identical definition returns
    the stored id, while a changed ``code_hash`` or schema under the same version
    is rejected so history stays reproducible.
    """

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

    def register(
        self,
        *,
        strategy_key: str,
        version: str,
        name: str,
        parameter_schema: dict[str, object],
        code_hash: str,
    ) -> str:
        """Insert one definition or return the existing identical row's id."""

        if not code_hash.strip():
            raise ValueError("code_hash must not be blank")
        encoded_schema = encode_json(parameter_schema)
        with self._database.transaction() as connection:
            row = connection.execute(
                """
                SELECT strategy_id, parameter_schema, code_hash
                FROM strategy_definitions
                WHERE strategy_key = ? AND version = ?
                """,
                [strategy_key, version],
            ).fetchone()
            if row is not None:
                stored_schema = decode_json(row[1])
                if cast(str, row[2]) != code_hash or stored_schema != parameter_schema:
                    raise RuntimeError(
                        "strategy definition changed under a released version: "
                        f"{strategy_key} {version}"
                    )
                return cast(str, row[0])
            strategy_id = self._id_generator.new()
            connection.execute(
                """
                INSERT INTO strategy_definitions (
                    strategy_id, strategy_key, version, name,
                    parameter_schema, code_hash, active, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, TRUE, ?)
                """,
                [
                    strategy_id,
                    strategy_key,
                    version,
                    name,
                    encoded_schema,
                    code_hash,
                    self._clock(),
                ],
            )
            return strategy_id

    def get_id(self, strategy_key: str, version: str) -> str | None:
        """Return the ULID for one released version when present."""

        with self._database.read_connection() as connection:
            row = connection.execute(
                """
                SELECT strategy_id
                FROM strategy_definitions
                WHERE strategy_key = ? AND version = ? AND active
                """,
                [strategy_key, version],
            ).fetchone()
        return None if row is None else cast(str, row[0])
