"""Tushare market-data adapter with deterministic retry and safe errors."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from datetime import date, datetime
from functools import partial
from typing import Protocol, cast

import pandas as pd

from etf_quant_lab.contracts import (
    DailyBarsQuery,
    DomainError,
    ErrorCode,
    RawProviderBatch,
    TradeCalendarQuery,
)
from etf_quant_lab.contracts.enums import DataSource, PriceAdjustment
from etf_quant_lab.ids import IdGenerator

_FUND_DAILY_DATASET = "fund_daily"
_TRADE_CALENDAR_DATASET = "trade_cal"

_FUND_DAILY_FIELDS = (
    "ts_code",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "vol",
    "amount",
)
_FUND_DAILY_OUTPUT_FIELDS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "pre_close",
    "volume",
    "amount",
)
_TRADE_CALENDAR_FIELDS = ("exchange", "cal_date", "is_open", "pretrade_date")
_TRADE_CALENDAR_OUTPUT_FIELDS = (
    "exchange",
    "cal_date",
    "is_open",
    "previous_open_date",
)

_RATE_LIMIT_MARKERS = (
    "rate limit",
    "too many requests",
    "访问频率",
    "访问次数",
    "每分钟",
    "频率限制",
    "频率超限",
)
_AUTH_MARKERS = (
    "token",
    "unauthorized",
    "forbidden",
    "permission",
    "鉴权",
    "权限",
    "积分不足",
)
_TIMEOUT_MARKERS = ("timeout", "timed out", "超时")


class TushareClient(Protocol):
    """Small client surface used by the adapter and its test doubles."""

    def fund_daily(
        self,
        *,
        ts_code: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame: ...

    def trade_cal(
        self,
        *,
        exchange: str,
        start_date: str,
        end_date: str,
        fields: str,
    ) -> pd.DataFrame: ...


class TushareProvider:
    """Fetch Tushare responses without persisting or canonicalizing them."""

    def __init__(
        self,
        client: TushareClient,
        id_generator: IdGenerator,
        clock: Callable[[], datetime],
        *,
        max_retries: int = 3,
        retry_backoff_seconds: float = 0.5,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        self._client = client
        self._id_generator = id_generator
        self._clock = clock
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._sleeper = sleeper

    @property
    def source(self) -> DataSource:
        """Return the stable source identifier for this adapter."""

        return DataSource.TUSHARE

    def fetch_daily_bars(self, query: DailyBarsQuery) -> RawProviderBatch:
        """Fetch raw ETF daily bars and map Tushare field names safely."""

        if query.adjustment != PriceAdjustment.RAW:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "Tushare fund_daily 适配器仅支持未复权行情",
                details={"adjustment": query.adjustment.value},
            )

        records: list[Mapping[str, object]] = []
        for symbol in query.symbols:
            frame = self._request_frame(
                partial(
                    self._client.fund_daily,
                    ts_code=symbol,
                    start_date=_format_date(query.start_date),
                    end_date=_format_date(query.end_date),
                    fields=",".join(_FUND_DAILY_FIELDS),
                ),
                dataset=_FUND_DAILY_DATASET,
                operation="fetch_daily_bars",
                request_context={"symbol": symbol},
            )
            records.extend(
                self._map_records(
                    frame,
                    source_fields=_FUND_DAILY_FIELDS,
                    output_fields=_FUND_DAILY_OUTPUT_FIELDS,
                    dataset=_FUND_DAILY_DATASET,
                    operation="fetch_daily_bars",
                )
            )

        return RawProviderBatch(
            batch_id=self._id_generator.new(),
            source=self.source,
            dataset=_FUND_DAILY_DATASET,
            records=tuple(records),
            fetched_at=self._clock(),
            request_metadata={
                "symbols": query.symbols,
                "start_date": query.start_date.isoformat(),
                "end_date": query.end_date.isoformat(),
                "adjustment": query.adjustment.value,
                "request_count": len(query.symbols),
            },
        )

    def fetch_trade_calendar(self, query: TradeCalendarQuery) -> RawProviderBatch:
        """Fetch an exchange calendar and map Tushare calendar field names."""

        frame = self._request_frame(
            lambda: self._client.trade_cal(
                exchange=query.exchange.value,
                start_date=_format_date(query.start_date),
                end_date=_format_date(query.end_date),
                fields=",".join(_TRADE_CALENDAR_FIELDS),
            ),
            dataset=_TRADE_CALENDAR_DATASET,
            operation="fetch_trade_calendar",
            request_context={"exchange": query.exchange.value},
        )
        records = self._map_records(
            frame,
            source_fields=_TRADE_CALENDAR_FIELDS,
            output_fields=_TRADE_CALENDAR_OUTPUT_FIELDS,
            dataset=_TRADE_CALENDAR_DATASET,
            operation="fetch_trade_calendar",
        )

        return RawProviderBatch(
            batch_id=self._id_generator.new(),
            source=self.source,
            dataset=_TRADE_CALENDAR_DATASET,
            records=records,
            fetched_at=self._clock(),
            request_metadata={
                "exchange": query.exchange.value,
                "start_date": query.start_date.isoformat(),
                "end_date": query.end_date.isoformat(),
                "request_count": 1,
            },
        )

    def _request_frame(
        self,
        request: Callable[[], pd.DataFrame],
        *,
        dataset: str,
        operation: str,
        request_context: Mapping[str, object],
    ) -> pd.DataFrame:
        terminal_error: DomainError | None = None
        for attempt in range(self._max_retries + 1):
            try:
                frame = request()
            except Exception as exc:  # External SDK boundary: normalize all provider failures.
                safe_error = self._translate_provider_error(
                    exc,
                    dataset=dataset,
                    operation=operation,
                    request_context=request_context,
                )
                if not safe_error.retryable or attempt == self._max_retries:
                    terminal_error = safe_error
                    break
                delay = self._retry_backoff_seconds * (2**attempt)
                self._sleeper(delay)
            else:
                if not isinstance(frame, pd.DataFrame):
                    raise self._schema_error(
                        dataset=dataset,
                        operation=operation,
                        missing_fields=(),
                        reason="response_not_dataframe",
                    )
                return frame

        if terminal_error is None:  # Defensive guard for future retry-loop changes.
            raise RuntimeError("Tushare retry loop ended without a response or error")
        # Raised outside the SDK exception handler so secret-bearing causes are not retained.
        raise terminal_error

    def _map_records(
        self,
        frame: pd.DataFrame,
        *,
        source_fields: tuple[str, ...],
        output_fields: tuple[str, ...],
        dataset: str,
        operation: str,
    ) -> tuple[Mapping[str, object], ...]:
        available_fields = {str(column) for column in frame.columns}
        missing_fields = tuple(
            field for field in source_fields if field not in available_fields
        )
        if missing_fields:
            raise self._schema_error(
                dataset=dataset,
                operation=operation,
                missing_fields=missing_fields,
                reason="required_fields_missing",
            )

        selected = frame.loc[:, list(source_fields)]
        rows = cast(
            Iterable[tuple[object, ...]],
            selected.itertuples(index=False, name=None),
        )
        return tuple(dict(zip(output_fields, row, strict=True)) for row in rows)

    def _translate_provider_error(
        self,
        exc: Exception,
        *,
        dataset: str,
        operation: str,
        request_context: Mapping[str, object],
    ) -> DomainError:
        normalized_message = str(exc).casefold()
        exception_name = type(exc).__name__.casefold()
        details = {
            "provider": self.source.value,
            "dataset": dataset,
            "operation": operation,
            **request_context,
        }

        if any(marker in normalized_message for marker in _RATE_LIMIT_MARKERS):
            return DomainError(
                "DATA_SOURCE_RATE_LIMITED",
                "Tushare 请求触发频率限制",
                details=details,
                retryable=True,
            )
        if isinstance(exc, PermissionError) or any(
            marker in normalized_message for marker in _AUTH_MARKERS
        ):
            return DomainError(
                "DATA_AUTH_FAILED",
                "Tushare 鉴权失败或接口权限不足",
                details=details,
                retryable=False,
            )
        if (
            isinstance(exc, TimeoutError)
            or "timeout" in exception_name
            or any(marker in normalized_message for marker in _TIMEOUT_MARKERS)
        ):
            return DomainError(
                "DATA_SOURCE_UNAVAILABLE",
                "Tushare 请求超时",
                details={**details, "reason": "timeout"},
                retryable=True,
            )
        return DomainError(
            "DATA_SOURCE_UNAVAILABLE",
            "Tushare 数据源暂时不可用",
            details=details,
            retryable=True,
        )

    def _schema_error(
        self,
        *,
        dataset: str,
        operation: str,
        missing_fields: tuple[str, ...],
        reason: str,
    ) -> DomainError:
        return DomainError(
            "DATA_SOURCE_SCHEMA_CHANGED",
            "Tushare 返回字段结构不符合预期",
            details={
                "provider": self.source.value,
                "dataset": dataset,
                "operation": operation,
                "missing_fields": missing_fields,
                "reason": reason,
            },
            retryable=False,
        )


def _format_date(value: date) -> str:
    return value.strftime("%Y%m%d")
