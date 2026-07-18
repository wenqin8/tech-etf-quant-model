"""Normalize raw provider batches into canonical daily bars with findings.

The canonical :class:`DailyBar` contract already rejects non-positive prices and
broken OHLC ordering at construction time, so any raw row that would violate
those invariants is reported as a blocking :class:`QualityFinding` instead of
crashing the batch.  This is the single place where node-3/4 provider output is
turned into the node-5 storage input, and where node-6 record-level rules
("OHLC 非法", "缺失字段") take effect.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from etf_quant_lab.contracts.data import DailyBar, RawProviderBatch
from etf_quant_lab.contracts.enums import DataSource, Exchange, QualitySeverity
from etf_quant_lab.contracts.quality import QualityFinding

RULE_INVALID_RECORD = "daily_bar.invalid_record"
RULE_MISSING_FIELD = "daily_bar.missing_field"

_REQUIRED_TUSHARE_FIELDS = (
    "symbol",
    "trade_date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
# AKShare 东方财富 ETF daily columns; volume arrives in lots (手, 100 shares).
_REQUIRED_AKSHARE_EM_FIELDS = (
    "query_symbol",
    "日期",
    "开盘",
    "最高",
    "最低",
    "收盘",
    "成交量",
    "成交额",
)
# AKShare sina ETF daily columns; volume arrives directly in shares.
_REQUIRED_AKSHARE_SINA_FIELDS = (
    "query_symbol",
    "date",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)
_AKSHARE_LOT_SHARES = Decimal(100)
_EXCHANGE_BY_SUFFIX = {"SH": Exchange.SSE, "SZ": Exchange.SZSE}


@dataclass(frozen=True, slots=True)
class NormalizedBatch:
    """Canonical bars plus per-record findings from one normalization pass."""

    bars: tuple[DailyBar, ...]
    findings: tuple[QualityFinding, ...]


def normalize_tushare_daily_bars(
    batch: RawProviderBatch,
    *,
    batch_id: str,
    ingested_at: datetime,
) -> NormalizedBatch:
    """Convert a Tushare ``fund_daily`` raw batch into canonical bars and findings.

    ``batch_id`` and ``ingested_at`` are supplied by the caller so the canonical
    identity is independent of the transient raw fetch identity.
    """

    bars: list[DailyBar] = []
    findings: list[QualityFinding] = []
    for index, record in enumerate(batch.records):
        symbol = _record_symbol(record)
        missing = _missing_fields(record)
        if missing:
            findings.append(
                QualityFinding(
                    rule_code=RULE_MISSING_FIELD,
                    severity=QualitySeverity.BLOCKING,
                    message="原始行情记录缺少必需字段",
                    symbol=symbol,
                    observed_value={"row_index": index, "missing_fields": tuple(missing)},
                )
            )
            continue
        try:
            bars.append(_to_daily_bar(record, batch_id=batch_id, ingested_at=ingested_at))
        except (ValueError, KeyError, InvalidOperation, TypeError) as exc:
            findings.append(
                QualityFinding(
                    rule_code=RULE_INVALID_RECORD,
                    severity=QualitySeverity.BLOCKING,
                    message="原始行情记录无法转换为标准日线",
                    symbol=symbol,
                    observed_value={"row_index": index, "reason": str(exc)},
                )
            )
    return NormalizedBatch(bars=tuple(bars), findings=tuple(findings))


def akshare_close_map(batch: RawProviderBatch) -> dict[tuple[str, date], Decimal]:
    """Extract a ``(symbol, trade_date) -> close`` map from an AKShare raw batch.

    Used for cross-source comparison only; rows with unreadable dates or closes
    are skipped so a malformed secondary sample never blocks the primary source.
    """

    closes: dict[tuple[str, date], Decimal] = {}
    for record in batch.records:
        symbol = record.get("query_symbol")
        raw_date = record.get("日期")
        raw_close = record.get("收盘")
        if not isinstance(symbol, str):
            continue
        parsed_date = _parse_iso_date(raw_date)
        close = _to_decimal(raw_close)
        if parsed_date is None or close is None:
            continue
        closes[(symbol.strip().upper(), parsed_date)] = close
    return closes


def normalize_akshare_daily_bars(
    batch: RawProviderBatch,
    *,
    batch_id: str,
    ingested_at: datetime,
) -> NormalizedBatch:
    """Convert an AKShare ``fund_daily`` raw batch into canonical bars.

    Two record dialects are supported and auto-detected per record: the
    Eastmoney feed (Chinese columns, volume in lots of 100 shares) and the sina
    feed (English columns, volume already in shares).  Bad records become
    BLOCKING findings, mirroring the Tushare normalizer.
    """

    bars: list[DailyBar] = []
    findings: list[QualityFinding] = []
    for index, record in enumerate(batch.records):
        symbol_value = record.get("query_symbol")
        symbol = (
            symbol_value.strip().upper()
            if isinstance(symbol_value, str) and symbol_value.strip()
            else None
        )
        is_sina = "date" in record
        required = _REQUIRED_AKSHARE_SINA_FIELDS if is_sina else _REQUIRED_AKSHARE_EM_FIELDS
        missing = [field for field in required if record.get(field) is None]
        if missing:
            findings.append(
                QualityFinding(
                    rule_code=RULE_MISSING_FIELD,
                    severity=QualitySeverity.BLOCKING,
                    message="原始行情记录缺少必需字段",
                    symbol=symbol,
                    observed_value={"row_index": index, "missing_fields": tuple(missing)},
                )
            )
            continue
        try:
            converter = _akshare_sina_record_to_bar if is_sina else _akshare_em_record_to_bar
            bars.append(converter(record, batch_id=batch_id, ingested_at=ingested_at))
        except (ValueError, KeyError, InvalidOperation, TypeError) as exc:
            findings.append(
                QualityFinding(
                    rule_code=RULE_INVALID_RECORD,
                    severity=QualitySeverity.BLOCKING,
                    message="原始行情记录无法转换为标准日线",
                    symbol=symbol,
                    observed_value={"row_index": index, "reason": str(exc)},
                )
            )
    return NormalizedBatch(bars=tuple(bars), findings=tuple(findings))


def _akshare_em_record_to_bar(
    record: Mapping[str, object],
    *,
    batch_id: str,
    ingested_at: datetime,
) -> DailyBar:
    symbol = str(record["query_symbol"]).strip().upper()
    exchange = _exchange_for_symbol(symbol)
    volume_lots = _require_decimal(record["成交量"])
    return DailyBar(
        symbol=symbol,
        trade_date=_require_date(record["日期"]),
        exchange=exchange,
        open=_require_decimal(record["开盘"]),
        high=_require_decimal(record["最高"]),
        low=_require_decimal(record["最低"]),
        close=_require_decimal(record["收盘"]),
        volume=volume_lots * _AKSHARE_LOT_SHARES,
        amount=_require_decimal(record["成交额"]),
        source=DataSource.AKSHARE,
        batch_id=batch_id,
        ingested_at=ingested_at,
    )


def _akshare_sina_record_to_bar(
    record: Mapping[str, object],
    *,
    batch_id: str,
    ingested_at: datetime,
) -> DailyBar:
    symbol = str(record["query_symbol"]).strip().upper()
    exchange = _exchange_for_symbol(symbol)
    return DailyBar(
        symbol=symbol,
        trade_date=_require_date(record["date"]),
        exchange=exchange,
        open=_require_decimal(record["open"]),
        high=_require_decimal(record["high"]),
        low=_require_decimal(record["low"]),
        close=_require_decimal(record["close"]),
        volume=_require_decimal(record["volume"]),
        amount=_require_decimal(record["amount"]),
        source=DataSource.AKSHARE,
        batch_id=batch_id,
        ingested_at=ingested_at,
    )


def _record_symbol(record: Mapping[str, object]) -> str | None:
    value = record.get("symbol")
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def _missing_fields(record: Mapping[str, object]) -> list[str]:
    return [
        field
        for field in _REQUIRED_TUSHARE_FIELDS
        if record.get(field) is None
    ]


def _to_daily_bar(
    record: Mapping[str, object],
    *,
    batch_id: str,
    ingested_at: datetime,
) -> DailyBar:
    symbol = str(record["symbol"]).strip().upper()
    exchange = _exchange_for_symbol(symbol)
    trade_date = _require_date(record["trade_date"])
    pre_close = _to_decimal(record.get("pre_close"))
    return DailyBar(
        symbol=symbol,
        trade_date=trade_date,
        exchange=exchange,
        open=_require_decimal(record["open"]),
        high=_require_decimal(record["high"]),
        low=_require_decimal(record["low"]),
        close=_require_decimal(record["close"]),
        volume=_require_decimal(record["volume"]),
        amount=_require_decimal(record["amount"]),
        source=DataSource.TUSHARE,
        batch_id=batch_id,
        ingested_at=ingested_at,
        pre_close=pre_close,
    )


def _exchange_for_symbol(symbol: str) -> Exchange:
    _, separator, suffix = symbol.partition(".")
    if separator != "." or suffix not in _EXCHANGE_BY_SUFFIX:
        raise ValueError(f"unsupported symbol suffix: {symbol}")
    return _EXCHANGE_BY_SUFFIX[suffix]


def _require_date(value: object) -> date:
    parsed = _parse_iso_date(value)
    if parsed is None:
        raise ValueError(f"unparseable trade_date: {value!r}")
    return parsed


def _require_decimal(value: object) -> Decimal:
    parsed = _to_decimal(value)
    if parsed is None:
        raise ValueError(f"unparseable numeric value: {value!r}")
    return parsed


def _parse_iso_date(value: object) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, str):
        return None
    text = value.strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            continue
    return None


def _to_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return Decimal(text)
        except InvalidOperation:
            return None
    return None
