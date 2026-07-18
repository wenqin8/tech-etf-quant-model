"""Data synchronization orchestration and the real Tushare client factory.

``sync_daily_bars`` closes the loop documented in §4.1: fetch via a provider →
persist the raw snapshot → normalize → stage canonical files → run the quality
gate → activate (superseding prior active batches) or reject.  The Tushare
client factory is the only place the token is read, and it never logs or
re-raises the secret.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

from etf_quant_lab.config import AppSettings
from etf_quant_lab.contracts.data import DailyBarsQuery, DataBatch, RawProviderBatch
from etf_quant_lab.contracts.enums import (
    DataBatchStatus,
    DataSource,
    Exchange,
    PriceAdjustment,
    QualityGateStatus,
)
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.quality import QualityReport, RunQualityChecksRequest
from etf_quant_lab.data.normalize import (
    NormalizedBatch,
    normalize_akshare_daily_bars,
    normalize_tushare_daily_bars,
)
from etf_quant_lab.data.providers.base import MarketDataProvider
from etf_quant_lab.data.providers.tushare import TushareClient, TushareProvider
from etf_quant_lab.ids import IdGenerator
from etf_quant_lab.services.quality import QualityService
from etf_quant_lab.storage.parquet import ParquetStore
from etf_quant_lab.storage.repositories import DataBatchRepository

DATA_AUTH_MISSING = "DATA_AUTH_MISSING"
DATA_NO_ROWS = "DATA_NO_ROWS"
DATA_SOURCE_UNSUPPORTED = "DATA_SOURCE_UNSUPPORTED"


def _symbol_key(symbols: tuple[str, ...]) -> str:
    """Stable identity for a batch's symbol set, used to scope supersession.

    A per-symbol sync must only retire the prior batch for that same symbol; a
    whole-universe sync only retires the prior whole-universe batch.  Sorting
    makes the key order-independent.
    """

    return ",".join(sorted(symbols))


def active_adjustment(batch_repository: DataBatchRepository) -> PriceAdjustment:
    """Return the price adjustment of the current ACTIVE daily-bars history.

    Incremental top-ups must match the standing history's adjustment or the
    series would mix adjusted and unadjusted prices.  Batches created before
    adjustment tracking carry no ``adjustment`` metadata and were RAW.
    """

    for batch in batch_repository.list_recent(limit=200):
        if batch.status == DataBatchStatus.ACTIVE and batch.dataset == "daily_bars":
            value = str(batch.metadata.get("adjustment", PriceAdjustment.RAW.value))
            try:
                return PriceAdjustment(value)
            except ValueError:
                return PriceAdjustment.RAW
    return PriceAdjustment.QFQ  # empty database: prefer adjusted going forward


def _normalize(
    raw: RawProviderBatch,
    *,
    batch_id: str,
    ingested_at: datetime,
) -> NormalizedBatch:
    """Dispatch raw-record normalization by source; unknown sources fail loudly."""

    if raw.source == DataSource.TUSHARE:
        return normalize_tushare_daily_bars(raw, batch_id=batch_id, ingested_at=ingested_at)
    if raw.source == DataSource.AKSHARE:
        return normalize_akshare_daily_bars(raw, batch_id=batch_id, ingested_at=ingested_at)
    raise DomainError(
        DATA_SOURCE_UNSUPPORTED,
        "该数据源没有对应的归一化器",
        details={"source": raw.source.value},
    )


def build_tushare_client(settings: AppSettings) -> TushareClient:
    """Create an authenticated Tushare Pro HTTP client from settings.

    Raises loudly when the token is absent; the secret itself never leaves this
    function except inside the client object.  The bundled tushare SDK is not
    used because it targets a different endpoint and swallows API errors into
    empty DataFrames.
    """

    if not settings.tushare_configured:
        raise DomainError(
            DATA_AUTH_MISSING,
            "缺少 Tushare Token, 无法创建正式数据客户端",
            details={"hint": "设置 EQL_TUSHARE_TOKEN 环境变量"},
        )
    from etf_quant_lab.data.providers.tushare_http import HttpTushareClient

    token = settings.tushare_token.get_secret_value()  # type: ignore[union-attr]
    return HttpTushareClient(token, timeout_seconds=settings.http_timeout_seconds)


def build_tushare_provider(
    settings: AppSettings,
    id_generator: IdGenerator,
    *,
    clock: Callable[[], datetime] | None = None,
) -> TushareProvider:
    """Assemble the retrying provider around a real authenticated client."""

    return TushareProvider(
        build_tushare_client(settings),
        id_generator,
        clock or (lambda: datetime.now(UTC)),
        max_retries=settings.http_max_retries,
    )


class DataSyncService:
    """Orchestrate fetch → raw → normalize → gate → activate for daily bars."""

    def __init__(
        self,
        *,
        provider: MarketDataProvider,
        parquet_store: ParquetStore,
        batch_repository: DataBatchRepository,
        quality_service: QualityService,
        id_generator: IdGenerator,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._provider = provider
        self._store = parquet_store
        self._batches = batch_repository
        self._quality = quality_service
        self._id_generator = id_generator
        self._clock = clock or (lambda: datetime.now(UTC))

    def sync_daily_bars(
        self,
        *,
        symbols: tuple[str, ...],
        start_date: date,
        end_date: date,
        exchange: Exchange = Exchange.SSE,
        incremental: bool = False,
        adjustment: PriceAdjustment = PriceAdjustment.QFQ,
    ) -> tuple[DataBatch, QualityReport]:
        """Run one full sync; the batch activates only when the gate passes.

        ``adjustment`` defaults to QFQ (前复权): fund share splits and large
        distributions would otherwise appear as ~50% single-day jumps in raw
        prices and be rejected by the extreme-return gate (seen live on
        512800/512480/512760/159995).  Adjusted prices keep momentum and
        return calculations continuous across corporate actions.

        ``incremental=True`` marks a short catch-up window: it supersedes only
        earlier *incremental* batches for the same symbols (whose window it
        covers, given a fixed lookback), never the full-history batch — daily
        top-ups therefore keep years of history intact while ``v_daily_bars``
        resolves overlapping days to the freshest fetch.  A full sync
        (default) supersedes both kinds for its symbols.

        Returns the final batch (ACTIVE or REJECTED) and the gate report, so the
        caller can surface both without re-querying.
        """

        raw = self._provider.fetch_daily_bars(
            DailyBarsQuery(
                symbols=symbols,
                start_date=start_date,
                end_date=end_date,
                adjustment=adjustment,
            )
        )
        raw_artifact = self._store.write_raw(raw)

        canonical_batch_id = self._id_generator.new()
        normalized = _normalize(raw, batch_id=canonical_batch_id, ingested_at=self._clock())
        symbol_key = _symbol_key(symbols)
        sync_mode = "incremental" if incremental else "full"
        self._batches.create(
            DataBatch(
                batch_id=canonical_batch_id,
                provider=raw.source,
                dataset="daily_bars",
                status=DataBatchStatus.FETCHING,
                fetched_at=raw.fetched_at,
                schema_version="daily_bar_v1",
                parent_batch_id=None,
                metadata={
                    "raw_file": raw_artifact.relative_path,
                    "symbol_key": symbol_key,
                    "sync_mode": sync_mode,
                    "adjustment": adjustment.value,
                },
            ),
            requested_start=start_date,
            requested_end=end_date,
        )

        if not normalized.bars:
            rejected = self._batches.reject(canonical_batch_id, "没有可发布的标准化行情")
            raise DomainError(
                DATA_NO_ROWS,
                "同步结果为空或全部记录非法",
                details={
                    "batch_id": rejected.batch_id,
                    "findings": len(normalized.findings),
                },
            )

        artifacts = self._store.write_canonical_daily_bars(normalized.bars)
        self._batches.stage_files(canonical_batch_id, artifacts)

        report = self._quality.run_checks(
            RunQualityChecksRequest(
                batch_id=canonical_batch_id,
                exchange=exchange,
                as_of_date=end_date,
            )
        )
        if report.gate_status == QualityGateStatus.FAILED or normalized.findings:
            batch = self._batches.reject(canonical_batch_id, "质量门禁未通过")
            return batch, report

        previous_active = tuple(
            batch.batch_id
            for batch in self._batches.list_recent(limit=500)
            if batch.status == DataBatchStatus.ACTIVE
            and batch.dataset == "daily_bars"
            and batch.metadata.get("symbol_key") == symbol_key
            # An incremental top-up must never retire the full-history batch;
            # a full sync retires everything for these symbols.
            and (not incremental or batch.metadata.get("sync_mode") == "incremental")
        )
        batch = self._batches.activate(
            canonical_batch_id,
            supersede_batch_ids=previous_active,
        )
        return batch, report
