"""AKShare adapter for cross-source validation and optional primary publication.

The adapter returns :class:`RawProviderBatch` objects with the provider's
original column names and units; it never canonicalizes or persists data
itself.  Unit translation (lots to shares) lives in
``data/normalize.normalize_akshare_daily_bars``.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from importlib import import_module
from typing import cast

import pandas as pd
import requests

from etf_quant_lab.contracts.data import DailyBarsQuery, RawProviderBatch, TradeCalendarQuery
from etf_quant_lab.contracts.enums import DataSource, PriceAdjustment
from etf_quant_lab.contracts.errors import DomainError, ErrorCode
from etf_quant_lab.ids import IdGenerator, UlidGenerator

DailyBarsFetcher = Callable[..., pd.DataFrame]
TradeCalendarFetcher = Callable[[], pd.DataFrame]
Clock = Callable[[], datetime]

_DAILY_DATASET = "fund_daily"
_CALENDAR_DATASET = "trade_cal"
_DAILY_REQUIRED_COLUMNS = frozenset(
    {"日期", "开盘", "收盘", "最高", "最低", "成交量", "成交额"}
)
_SINA_DAILY_REQUIRED_COLUMNS = frozenset(
    {"date", "open", "high", "low", "close", "volume", "amount"}
)
_SINA_QFQ_FACTOR_COLUMNS = frozenset({"d", "f", "s", "u"})
_CANONICAL_PRICE_DECIMAL_PLACES = 6
_CALENDAR_REQUIRED_COLUMNS = frozenset({"trade_date"})
_ADJUSTMENT_ARGUMENT = {
    PriceAdjustment.RAW: "",
    PriceAdjustment.QFQ: "qfq",
    PriceAdjustment.HFQ: "hfq",
}
# Eastmoney's quote API drops connections from the python-requests default
# User-Agent (observed live 2026-07-16: bare requests -> RemoteDisconnected,
# browser UA -> 200).  akshare calls requests without setting one, so we patch
# the library-wide default UA once at import time.
_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _install_browser_user_agent() -> None:
    import requests.utils

    requests.utils.default_user_agent = lambda *args: _BROWSER_USER_AGENT


_install_browser_user_agent()


def _utc_now() -> datetime:
    return datetime.now(UTC)


@contextmanager
def _proxy_disabled() -> Iterator[None]:
    """Temporarily route all requests-based calls directly, bypassing any proxy.

    requests consults ``NO_PROXY`` before the Windows registry proxy, so setting
    it to ``*`` for the duration of one attempt neutralizes an unreachable local
    proxy without permanently changing the process environment.
    """

    saved = {name: os.environ.get(name) for name in ("NO_PROXY", "no_proxy")}
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _call_with_proxy_fallback(call: Callable[[], pd.DataFrame]) -> pd.DataFrame:
    """Run one network call, retrying once with the system proxy bypassed.

    Network policy (user directive 2026-07-15): on a network failure, retry the
    same call with the opposite proxy setting before failing over to another
    data source or surfacing an error.  The first attempt uses the ambient
    configuration (proxy if the OS has one); the second forces a direct
    connection.  Non-network errors propagate immediately.
    """

    try:
        return call()
    except Exception as first_error:
        if not _looks_like_network_error(first_error):
            raise
        with _proxy_disabled():
            return call()


def _looks_like_network_error(error: Exception) -> bool:
    name = type(error).__name__.lower()
    text = str(error).lower()
    markers = ("proxy", "connection", "timeout", "timed out", "remote", "ssl", "eof")
    return any(marker in name for marker in markers) or any(
        marker in text for marker in markers
    )


def _adjust_sina_etf_qfq(
    frame: pd.DataFrame,
    factors: pd.DataFrame,
    *,
    symbol: str,
) -> pd.DataFrame:
    """Apply Sina's ETF front-adjustment parameters to raw OHLC prices.

    Sina publishes ETF factor records as ``d/f/s/u``.  ``s`` is the reverse
    cumulative share-split scale and ``u`` is the future cash-distribution
    offset.  For the published ETF records ``f`` is the fund face value and is
    currently always one.  The latest price remains unchanged:

    ``qfq_price = raw_price / s - u``.

    Unknown face-value transformations fail closed rather than silently
    fabricating an adjusted series.
    """

    missing = _SINA_QFQ_FACTOR_COLUMNS - {str(column) for column in factors.columns}
    if missing:
        raise DomainError(
            "DATA_SOURCE_SCHEMA_CHANGED",
            "Sina ETF adjustment response is missing required fields",
            details={"symbol": symbol, "missing_columns": tuple(sorted(missing))},
        )
    if "date" not in frame.columns:
        return frame

    working = frame.copy()
    factor_frame = factors.loc[:, ["d", "f", "s", "u"]].copy()
    working["date"] = pd.to_datetime(working["date"], errors="coerce")
    factor_frame["d"] = pd.to_datetime(factor_frame["d"], errors="coerce")
    for column in ("f", "s", "u"):
        factor_frame[column] = pd.to_numeric(factor_frame[column], errors="coerce")

    if (
        bool(working["date"].isna().any())
        or bool(factor_frame[["d", "f", "s", "u"]].isna().any().any())
        or bool((factor_frame["s"] <= 0).any())
    ):
        raise DomainError(
            "DATA_SOURCE_SCHEMA_CHANGED",
            "Sina ETF adjustment response contains invalid values",
            details={"symbol": symbol},
        )
    if not bool(factor_frame["f"].eq(1).all()):
        raise DomainError(
            "DATA_SOURCE_SCHEMA_CHANGED",
            "Sina ETF adjustment uses an unsupported face-value transformation",
            details={"symbol": symbol},
        )

    factor_frame = factor_frame.sort_values("d").rename(
        columns={"d": "factor_date", "s": "qfq_scale", "u": "qfq_cash"}
    )
    adjusted = pd.merge_asof(
        working.sort_values("date"),
        factor_frame,
        left_on="date",
        right_on="factor_date",
        direction="backward",
    )
    if bool(adjusted[["qfq_scale", "qfq_cash"]].isna().any().any()):
        raise DomainError(
            "DATA_SOURCE_SCHEMA_CHANGED",
            "Sina ETF adjustment does not cover the requested history",
            details={"symbol": symbol},
        )

    for column in ("open", "high", "low", "close"):
        numeric = pd.to_numeric(adjusted[column], errors="coerce")
        adjusted[column] = (
            numeric / adjusted["qfq_scale"] - adjusted["qfq_cash"]
        ).round(_CANONICAL_PRICE_DECIMAL_PLACES)
    return adjusted.drop(columns=["factor_date", "f"])


class AkshareProvider:
    """Fetch AKShare snapshots for cross-source validation or primary publication.

    The default role stays ``cross_validation`` (node-4 semantics).  Passing
    ``publication_eligible=True`` promotes the provider to a primary channel:
    the batches it emits may then flow through normalization, the quality gate
    and activation exactly like Tushare batches (decision recorded 2026-07-15).
    """

    def __init__(
        self,
        *,
        daily_bars_fetcher: DailyBarsFetcher | None = None,
        trade_calendar_fetcher: TradeCalendarFetcher | None = None,
        id_generator: IdGenerator | None = None,
        clock: Clock | None = None,
        publication_eligible: bool = False,
    ) -> None:
        self._daily_bars_fetcher = daily_bars_fetcher
        self._trade_calendar_fetcher = trade_calendar_fetcher
        self._id_generator = id_generator or UlidGenerator()
        self._clock = clock or _utc_now
        self._publication_eligible = publication_eligible
        self._provider_role = "primary" if publication_eligible else "cross_validation"

    @property
    def source(self) -> DataSource:
        return DataSource.AKSHARE

    def fetch_daily_bars(self, query: DailyBarsQuery) -> RawProviderBatch:
        """Fetch raw ETF rows without converting dates, volume, or amount units.

        The Eastmoney feed is tried first; when it is unreachable (a common
        failure behind local proxies) and no explicit fetcher was injected, the
        sina daily feed serves as a fallback for RAW queries.  QFQ queries also
        use Sina's split/cash adjustment records to construct front-adjusted
        OHLC locally.  The two feeds' rows keep their native column names — the
        normalizer auto-detects the dialect per record.
        """

        fetcher = self._daily_bars_fetcher or self._load_daily_bars_fetcher()
        records: list[Mapping[str, object]] = []
        empty_symbols: list[str] = []
        operations_by_symbol: dict[str, str] = {}

        for symbol in query.symbols:
            provider_symbol = self._provider_symbol(symbol)
            frame, operation = self._fetch_symbol_frame(
                fetcher, query, symbol, provider_symbol
            )
            operations_by_symbol[symbol] = operation
            self._validate_frame(
                frame,
                required_columns=(
                    _SINA_DAILY_REQUIRED_COLUMNS
                    if operation.startswith("fund_etf_hist_sina")
                    else _DAILY_REQUIRED_COLUMNS
                ),
                dataset=_DAILY_DATASET,
                operation=operation,
            )
            if frame.empty:
                empty_symbols.append(symbol)
                continue

            for raw_record in frame.to_dict(orient="records"):
                record: dict[str, object] = {
                    "query_symbol": symbol,
                    "provider_symbol": provider_symbol,
                }
                record.update(cast(Mapping[str, object], raw_record))
                records.append(record)

        return RawProviderBatch(
            batch_id=self._id_generator.new(),
            source=self.source,
            dataset=_DAILY_DATASET,
            records=tuple(records),
            fetched_at=self._clock(),
            request_metadata={
                "symbols": query.symbols,
                "start_date": query.start_date.isoformat(),
                "end_date": query.end_date.isoformat(),
                "adjustment": query.adjustment.value,
                "provider_role": self._provider_role,
                "publication_eligible": self._publication_eligible,
                "empty_symbols": tuple(empty_symbols),
                "operations_by_symbol": operations_by_symbol,
            },
        )

    def _fetch_symbol_frame(
        self,
        fetcher: DailyBarsFetcher,
        query: DailyBarsQuery,
        symbol: str,
        provider_symbol: str,
    ) -> tuple[pd.DataFrame, str]:
        try:
            frame = _call_with_proxy_fallback(
                lambda: fetcher(
                    symbol=provider_symbol,
                    period="daily",
                    start_date=query.start_date.strftime("%Y%m%d"),
                    end_date=query.end_date.strftime("%Y%m%d"),
                    adjust=_ADJUSTMENT_ARGUMENT[query.adjustment],
                )
            )
        except DomainError:
            raise
        except Exception as primary_error:
            fallback_allowed = self._daily_bars_fetcher is None and query.adjustment in {
                PriceAdjustment.RAW,
                PriceAdjustment.QFQ,
            }
            if not fallback_allowed:
                raise self._unavailable_error(
                    dataset=_DAILY_DATASET,
                    operation="fund_etf_hist_em",
                    symbol=symbol,
                ) from primary_error
            frame = self._fetch_sina_daily(query, symbol)
            operation = (
                "fund_etf_hist_sina_qfq"
                if query.adjustment == PriceAdjustment.QFQ
                else "fund_etf_hist_sina"
            )
            return frame, operation
        return frame, "fund_etf_hist_em"

    def _fetch_sina_daily(self, query: DailyBarsQuery, symbol: str) -> pd.DataFrame:
        """Fallback fetch via the sina feed, range-filtered to the query window."""

        code, _, suffix = symbol.partition(".")
        sina_symbol = f"{suffix.lower()}{code}"
        sina_fetcher = self._load_function("fund_etf_hist_sina", dataset=_DAILY_DATASET)
        try:
            frame = _call_with_proxy_fallback(
                lambda: cast(pd.DataFrame, sina_fetcher(symbol=sina_symbol))
            )
        except DomainError:
            raise
        except Exception as exc:
            raise self._unavailable_error(
                dataset=_DAILY_DATASET,
                operation="fund_etf_hist_sina",
                symbol=symbol,
            ) from exc
        if not isinstance(frame, pd.DataFrame) or "date" not in frame.columns:
            return frame
        parsed = pd.to_datetime(frame["date"], errors="coerce")
        mask = (parsed >= pd.Timestamp(query.start_date)) & (
            parsed <= pd.Timestamp(query.end_date)
        )
        selected = frame.loc[mask].copy()
        if query.adjustment != PriceAdjustment.QFQ:
            return selected
        factors = self._fetch_sina_qfq_factors(sina_symbol, symbol)
        return _adjust_sina_etf_qfq(selected, factors, symbol=symbol)

    def _fetch_sina_qfq_factors(
        self,
        sina_symbol: str,
        symbol: str,
    ) -> pd.DataFrame:
        """Fetch and safely parse Sina's public ETF qfq.js adjustment records."""

        url = (
            "https://finance.sina.com.cn/realstock/company/"
            f"{sina_symbol}/qfq.js"
        )

        def fetch() -> pd.DataFrame:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            separator = response.text.find("=")
            payload_end = response.text.rfind("}")
            if separator < 0 or payload_end <= separator:
                raise ValueError("Sina ETF qfq response is not valid JavaScript data")
            payload = json.loads(response.text[separator + 1 : payload_end + 1])
            records = payload.get("data")
            if not isinstance(records, list):
                raise ValueError("Sina ETF qfq response has no data list")
            return pd.DataFrame(records)

        try:
            return _call_with_proxy_fallback(fetch)
        except DomainError:
            raise
        except Exception as exc:
            raise self._unavailable_error(
                dataset=_DAILY_DATASET,
                operation="fund_etf_hist_sina_qfq",
                symbol=symbol,
            ) from exc

    def fetch_trade_calendar(self, query: TradeCalendarQuery) -> RawProviderBatch:
        """Fetch and range-filter the shared A-share calendar without rewriting raw dates."""

        fetcher = self._trade_calendar_fetcher or self._load_trade_calendar_fetcher()
        try:
            frame = _call_with_proxy_fallback(fetcher)
        except DomainError:
            raise
        except Exception as exc:
            raise self._unavailable_error(
                dataset=_CALENDAR_DATASET,
                operation="tool_trade_date_hist_sina",
            ) from exc

        self._validate_frame(
            frame,
            required_columns=_CALENDAR_REQUIRED_COLUMNS,
            dataset=_CALENDAR_DATASET,
            operation="tool_trade_date_hist_sina",
        )
        selected = self._select_calendar_range(frame, query.start_date, query.end_date)

        return RawProviderBatch(
            batch_id=self._id_generator.new(),
            source=self.source,
            dataset=_CALENDAR_DATASET,
            records=tuple(
                cast(Mapping[str, object], record)
                for record in selected.to_dict(orient="records")
            ),
            fetched_at=self._clock(),
            request_metadata={
                "exchange": query.exchange.value,
                "start_date": query.start_date.isoformat(),
                "end_date": query.end_date.isoformat(),
                "provider_role": self._provider_role,
                "publication_eligible": self._publication_eligible,
            },
        )

    @staticmethod
    def _provider_symbol(symbol: str) -> str:
        code, separator, exchange = symbol.partition(".")
        if separator != "." or exchange not in {"SH", "SZ"} or not code.isdigit() or len(code) != 6:
            raise DomainError(
                ErrorCode.VALIDATION_ERROR,
                "AKShare ETF code mapping requires a six-digit .SH or .SZ symbol",
                details={"provider": DataSource.AKSHARE.value, "symbol": symbol},
            )
        return code

    @staticmethod
    def _validate_frame(
        frame: object,
        *,
        required_columns: frozenset[str],
        dataset: str,
        operation: str,
    ) -> None:
        if not isinstance(frame, pd.DataFrame):
            raise DomainError(
                "DATA_SOURCE_SCHEMA_CHANGED",
                "AKShare returned an unsupported response type",
                details={
                    "provider": DataSource.AKSHARE.value,
                    "dataset": dataset,
                    "operation": operation,
                    "response_type": type(frame).__name__,
                },
            )

        columns = {str(column) for column in frame.columns}
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            raise DomainError(
                "DATA_SOURCE_SCHEMA_CHANGED",
                "AKShare response columns no longer match the expected schema",
                details={
                    "provider": DataSource.AKSHARE.value,
                    "dataset": dataset,
                    "operation": operation,
                    "missing_columns": tuple(missing_columns),
                },
            )

    @staticmethod
    def _select_calendar_range(
        frame: pd.DataFrame,
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        parsed_dates = pd.to_datetime(frame["trade_date"], errors="coerce")
        if bool(parsed_dates.isna().any()):
            raise DomainError(
                "DATA_SOURCE_SCHEMA_CHANGED",
                "AKShare trade calendar contains an unreadable date",
                details={
                    "provider": DataSource.AKSHARE.value,
                    "dataset": _CALENDAR_DATASET,
                    "operation": "tool_trade_date_hist_sina",
                    "invalid_field": "trade_date",
                },
            )

        start = pd.Timestamp(start_date)
        end = pd.Timestamp(end_date)
        return frame.loc[(parsed_dates >= start) & (parsed_dates <= end)]

    @staticmethod
    def _unavailable_error(
        *,
        dataset: str,
        operation: str,
        symbol: str | None = None,
    ) -> DomainError:
        details: dict[str, object] = {
            "provider": DataSource.AKSHARE.value,
            "dataset": dataset,
            "operation": operation,
        }
        if symbol is not None:
            details["symbol"] = symbol
        return DomainError(
            "DATA_SOURCE_UNAVAILABLE",
            "AKShare data source is unavailable",
            details=details,
            retryable=True,
        )

    @staticmethod
    def _load_daily_bars_fetcher() -> DailyBarsFetcher:
        return cast(
            DailyBarsFetcher,
            AkshareProvider._load_function("fund_etf_hist_em", dataset=_DAILY_DATASET),
        )

    @staticmethod
    def _load_trade_calendar_fetcher() -> TradeCalendarFetcher:
        return cast(
            TradeCalendarFetcher,
            AkshareProvider._load_function(
                "tool_trade_date_hist_sina",
                dataset=_CALENDAR_DATASET,
            ),
        )

    @staticmethod
    def _load_function(name: str, *, dataset: str) -> Callable[..., object]:
        try:
            module = import_module("akshare")
        except Exception as exc:
            raise AkshareProvider._unavailable_error(
                dataset=dataset,
                operation=name,
            ) from exc

        function = getattr(module, name, None)
        if not callable(function):
            raise DomainError(
                "DATA_SOURCE_SCHEMA_CHANGED",
                "Installed AKShare package does not expose the expected interface",
                details={
                    "provider": DataSource.AKSHARE.value,
                    "dataset": dataset,
                    "operation": name,
                },
            )
        return cast(Callable[..., object], function)
