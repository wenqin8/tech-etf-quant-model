"""Immutable raw and canonical Parquet persistence primitives."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from etf_quant_lab.contracts.data import DailyBar, RawProviderBatch
from etf_quant_lab.contracts.enums import DataLayer

_SAFE_DATASET = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CANONICAL_DAILY_BAR_FIELDS: list[pa.Field[pa.DataType]] = [
    pa.field("symbol", pa.string(), nullable=False),
    pa.field("trade_date", pa.date32(), nullable=False),
    pa.field("exchange", pa.string(), nullable=False),
    pa.field("open", pa.decimal128(20, 6), nullable=False),
    pa.field("high", pa.decimal128(20, 6), nullable=False),
    pa.field("low", pa.decimal128(20, 6), nullable=False),
    pa.field("close", pa.decimal128(20, 6), nullable=False),
    pa.field("pre_close", pa.decimal128(20, 6)),
    pa.field("volume", pa.decimal128(24, 4), nullable=False),
    pa.field("amount", pa.decimal128(24, 4), nullable=False),
    pa.field("adj_factor", pa.decimal128(20, 8)),
    pa.field("is_suspended", pa.bool_(), nullable=False),
    pa.field("source", pa.string(), nullable=False),
    pa.field("batch_id", pa.string(), nullable=False),
    pa.field("ingested_at", pa.timestamp("us", tz="UTC"), nullable=False),
]
_CANONICAL_DAILY_BAR_SCHEMA = pa.schema(_CANONICAL_DAILY_BAR_FIELDS)


class ParquetStorageError(RuntimeError):
    """Raised when a Parquet artifact violates storage invariants."""


class ChecksumMismatchError(ParquetStorageError):
    """Raised when persisted bytes differ from their registered SHA-256."""


@dataclass(frozen=True, slots=True)
class ParquetArtifact:
    """A verified Parquet file ready to be registered in DuckDB."""

    batch_id: str
    layer: DataLayer
    dataset: str
    relative_path: str
    checksum: str
    row_count: int
    schema_version: str
    min_trade_date: date | None = None
    max_trade_date: date | None = None

    def __post_init__(self) -> None:
        if len(self.batch_id) != 26:
            raise ValueError("batch_id must contain exactly 26 characters")
        if len(self.checksum) != 64:
            raise ValueError("checksum must be a SHA-256 hexadecimal digest")
        if self.row_count < 0:
            raise ValueError("row_count must not be negative")
        if not self.schema_version.strip():
            raise ValueError("schema_version must not be blank")


def sha256_file(path: Path) -> str:
    """Return a lowercase SHA-256 digest for one file."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ParquetStore:
    """Write application-owned raw and canonical Parquet datasets."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = data_root.resolve()
        self.data_root.mkdir(parents=True, exist_ok=True)

    def resolve(self, relative_path: str) -> Path:
        """Resolve and confine one manifest path to the configured data root."""

        candidate = (self.data_root / Path(relative_path)).resolve()
        if not candidate.is_relative_to(self.data_root):
            raise ParquetStorageError("artifact path escapes the configured data root")
        return candidate

    def write_raw(
        self,
        batch: RawProviderBatch,
        *,
        schema_version: str = "raw_v1",
    ) -> ParquetArtifact:
        """Append one immutable provider response; an existing batch is never overwritten."""

        dataset = _normalize_dataset(batch.dataset)
        relative_path = Path(
            "raw",
            f"provider={batch.source.value.lower()}",
            f"dataset={dataset}",
            f"fetch_date={batch.fetched_at.date().isoformat()}",
            f"batch_id={batch.batch_id}",
            "part-000.parquet",
        )
        target = self.resolve(relative_path.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise FileExistsError(f"raw batch already exists: {relative_path.as_posix()}")

        records = [dict(record) for record in batch.records]
        table = pa.Table.from_pylist(records)
        table = table.replace_schema_metadata(
            _schema_metadata(
                batch_id=batch.batch_id,
                dataset=dataset,
                schema_version=schema_version,
                extra={
                    "layer": DataLayer.RAW.value,
                    "provider": batch.source.value,
                    "fetched_at": batch.fetched_at.isoformat(),
                    "request_metadata": batch.request_metadata,
                },
            )
        )
        temporary = self._write_temporary(table, target)
        try:
            self._publish_exclusive(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

        return ParquetArtifact(
            batch_id=batch.batch_id,
            layer=DataLayer.RAW,
            dataset=dataset,
            relative_path=relative_path.as_posix(),
            checksum=sha256_file(target),
            row_count=table.num_rows,
            schema_version=schema_version,
        )

    def write_canonical_daily_bars(
        self,
        bars: tuple[DailyBar, ...],
        *,
        schema_version: str = "daily_bar_v1",
    ) -> tuple[ParquetArtifact, ...]:
        """Atomically publish canonical daily bars partitioned by exchange and month."""

        if not bars:
            return ()
        batch_ids = {bar.batch_id for bar in bars}
        if len(batch_ids) != 1:
            raise ParquetStorageError("one canonical write must contain exactly one batch_id")
        batch_id = next(iter(batch_ids))

        partitions: dict[tuple[str, int, int], list[DailyBar]] = {}
        for bar in bars:
            key = (bar.exchange.value.lower(), bar.trade_date.year, bar.trade_date.month)
            partitions.setdefault(key, []).append(bar)

        staged: list[tuple[Path, Path, ParquetArtifact]] = []
        try:
            for (exchange, year, month), partition_bars in sorted(partitions.items()):
                ordered = sorted(partition_bars, key=lambda item: (item.trade_date, item.symbol))
                relative_path = Path(
                    "canonical",
                    "dataset=daily_bars",
                    f"exchange={exchange}",
                    f"year={year:04d}",
                    f"month={month:02d}",
                    f"part-{batch_id}.parquet",
                )
                target = self.resolve(relative_path.as_posix())
                target.parent.mkdir(parents=True, exist_ok=True)
                table = _daily_bars_table(ordered, schema_version=schema_version)
                temporary = self._write_temporary(table, target)
                temporary_checksum = sha256_file(temporary)

                if target.exists():
                    if sha256_file(target) != temporary_checksum:
                        raise FileExistsError(
                            "canonical batch path already contains different bytes: "
                            f"{relative_path.as_posix()}"
                        )
                    temporary.unlink(missing_ok=True)
                artifact = ParquetArtifact(
                    batch_id=batch_id,
                    layer=DataLayer.CANONICAL,
                    dataset="daily_bars",
                    relative_path=relative_path.as_posix(),
                    checksum=temporary_checksum,
                    row_count=table.num_rows,
                    schema_version=schema_version,
                    min_trade_date=ordered[0].trade_date,
                    max_trade_date=ordered[-1].trade_date,
                )
                staged.append((temporary, target, artifact))

            published: list[ParquetArtifact] = []
            for temporary, target, artifact in staged:
                if temporary.exists():
                    os.replace(temporary, target)
                published.append(artifact)
            return tuple(published)
        finally:
            for temporary, _, _ in staged:
                temporary.unlink(missing_ok=True)

    def verify(self, artifact: ParquetArtifact) -> None:
        """Verify that an artifact exists and still matches its registered digest."""

        path = self.resolve(artifact.relative_path)
        if not path.is_file():
            raise FileNotFoundError(path)
        actual = sha256_file(path)
        if actual != artifact.checksum:
            raise ChecksumMismatchError(
                f"checksum mismatch for {artifact.relative_path}: "
                f"expected {artifact.checksum}, got {actual}"
            )

    def read_table(self, artifact: ParquetArtifact, *, verify: bool = True) -> pa.Table:
        """Read one artifact without inferring Hive columns from its parent directories."""

        if verify:
            self.verify(artifact)
        return pq.ParquetFile(self.resolve(artifact.relative_path)).read()

    def recover_temporary_files(self) -> tuple[Path, ...]:
        """Remove orphaned files left before atomic publication completed."""

        recovered: list[Path] = []
        for temporary in sorted(self.data_root.rglob(".tmp-*.parquet")):
            temporary.unlink(missing_ok=True)
            recovered.append(temporary)
        return tuple(recovered)

    @staticmethod
    def _write_temporary(table: pa.Table, target: Path) -> Path:
        temporary = target.parent / f".tmp-{uuid4().hex}-{target.name}"
        pq.write_table(table, temporary, compression="zstd", write_statistics=True)
        return temporary

    @staticmethod
    def _publish_exclusive(temporary: Path, target: Path) -> None:
        try:
            os.link(temporary, target)
        except FileExistsError:
            raise
        except OSError:
            if target.exists():
                raise FileExistsError(target) from None
            try:
                with temporary.open("rb") as source, target.open("xb") as destination:
                    shutil.copyfileobj(source, destination)
            except BaseException:
                target.unlink(missing_ok=True)
                raise


def _normalize_dataset(dataset: str) -> str:
    normalized = dataset.strip().lower()
    if _SAFE_DATASET.fullmatch(normalized) is None:
        raise ParquetStorageError(f"unsafe dataset name: {dataset!r}")
    return normalized


def _daily_bars_table(bars: list[DailyBar], *, schema_version: str) -> pa.Table:
    batch_id = bars[0].batch_id
    rows = [
        {
            "symbol": bar.symbol,
            "trade_date": bar.trade_date,
            "exchange": bar.exchange.value,
            "open": bar.open,
            "high": bar.high,
            "low": bar.low,
            "close": bar.close,
            "pre_close": bar.pre_close,
            "volume": bar.volume,
            "amount": bar.amount,
            "adj_factor": bar.adj_factor,
            "is_suspended": bar.is_suspended,
            "source": bar.source.value,
            "batch_id": bar.batch_id,
            "ingested_at": bar.ingested_at,
        }
        for bar in bars
    ]
    table = pa.Table.from_pylist(rows, schema=_CANONICAL_DAILY_BAR_SCHEMA)
    return table.replace_schema_metadata(
        _schema_metadata(
            batch_id=batch_id,
            dataset="daily_bars",
            schema_version=schema_version,
            extra={"layer": DataLayer.CANONICAL.value},
        )
    )


def _schema_metadata(
    *,
    batch_id: str,
    dataset: str,
    schema_version: str,
    extra: Mapping[str, object],
) -> dict[bytes, bytes]:
    values: dict[str, object] = {
        "batch_id": batch_id,
        "dataset": dataset,
        "schema_version": schema_version,
        **extra,
    }
    return {
        f"eql.{key}".encode(): json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        ).encode()
        for key, value in values.items()
    }


def _json_default(value: object) -> str:
    if isinstance(value, (date, datetime, Decimal, Path, Enum)):
        return str(value)
    raise TypeError(f"unsupported metadata value: {type(value).__name__}")
