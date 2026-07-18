"""Shared helpers for the Streamlit pages: context, labels, translations."""

from __future__ import annotations

import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

from etf_quant_lab.app_context import ApplicationContext
from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import QualityGateStatus

MARKET_TZ = ZoneInfo("Asia/Shanghai")

ACTION_LABELS = {"BUY": "买入", "SELL": "卖出", "HOLD": "持有"}
GATE_LABELS = {
    QualityGateStatus.PASSED: "通过",
    QualityGateStatus.PASSED_WITH_WARNINGS: "通过(有提示)",
    QualityGateStatus.FAILED: "未通过",
}
BATCH_STATUS_LABELS = {
    "ACTIVE": "已激活",
    "VALIDATING": "校验中",
    "FETCHING": "获取中",
    "REJECTED": "已拒绝",
    "SUPERSEDED": "已被替代",
}
TASK_STATUS_LABELS = {
    "SUCCEEDED": "成功",
    "FAILED": "失败",
    "SKIPPED": "已跳过(重复触发)",
    "RUNNING": "运行中",
    "BLOCKED": "被阻断",
}
REASON_LABELS = {
    "MOMENTUM_TOP_RANK": "动量排名靠前",
    "ABOVE_TREND_FILTER": "高于趋势均线",
    "TREND_FAST_ABOVE_SLOW": "快线上穿慢线",
    "TREND_FILTER_BYPASSED": "趋势过滤已跳过",
    "EXIT_NOT_IN_TARGET": "退出: 不在目标组合中",
    "NO_TRADEABLE_SYMBOL": "无可交易标的",
    "INSUFFICIENT_CANDIDATES": "候选标的不足",
}
RULE_LABELS = {
    "daily_bar.extreme_return": "单日涨跌幅异常",
    "daily_bar.duplicate_key": "重复行情记录",
    "daily_bar.future_date": "出现未来日期",
    "daily_bar.non_trading_date": "非交易日行情",
    "daily_bar.trading_calendar_gap": "交易日数据缺失",
    "daily_bar.staleness": "数据过期",
    "daily_bar.missing_field": "缺少必需字段",
    "daily_bar.invalid_record": "记录无法解析",
}


@st.cache_resource(max_entries=2)
def _build_context(module_generation: str) -> ApplicationContext:
    """One shared application context per Streamlit server process.

    ``module_generation`` busts the resource cache whenever Streamlit's hot
    reload evicts any project module from ``sys.modules``: reloaded modules
    define brand-new classes (enums, DomainError, services), and a context
    cached from the previous generation would keep handing out objects of the
    old classes — breaking ``except DomainError`` and enum comparisons on
    pages, and raising AttributeError for newly added service methods.
    """

    del module_generation  # participates in the cache key only
    return build_full_context()


def _module_generation() -> str:
    """Fingerprint the identity of currently imported project modules.

    ``id(module)`` changes when a module is re-imported after hot-reload
    eviction, so the joined ids form a stable key per module generation.
    """

    return ",".join(
        f"{name}:{id(module)}"
        for name, module in sorted(sys.modules.items())
        if name.startswith("etf_quant_lab")
    )


def get_context() -> ApplicationContext:
    return _build_context(_module_generation())


@st.cache_data(ttl=600, max_entries=4, show_spinner="加载行情数据 ...")
def _load_bars_cached(manifest_checksum: str) -> tuple[DailyBar, ...]:
    """Load all active daily bars once per manifest state.

    The manifest checksum is the cache key: any sync that activates a new batch
    changes it, so the cache can never serve stale data, while repeated page
    loads within one data state skip the 3-4s Parquet scan entirely.
    """

    del manifest_checksum  # participates in the cache key only
    context = get_context()
    bars: tuple[DailyBar, ...] = context.resolve("batches").query_daily_bars()  # type: ignore[attr-defined]
    return bars


def load_daily_bars() -> tuple[DailyBar, ...]:
    """Cached accessor for the active daily bars; pages should use this."""

    context = get_context()
    checksum = context.resolve("batches").active_daily_bar_manifest_checksum()  # type: ignore[attr-defined]
    return _load_bars_cached(checksum)


def clear_bars_cache() -> None:
    """Drop the cached bars after a sync/pipeline activates new batches."""

    _load_bars_cached.clear()


def market_today() -> object:
    return datetime.now(MARKET_TZ).date()


def market_time_str(value: object, fmt: str = "%m-%d %H:%M") -> str:
    """Format a stored UTC timestamp in Beijing time for display."""

    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.astimezone(MARKET_TZ).strftime(fmt)
    return str(value)[:16]


def instrument_names(context: ApplicationContext) -> dict[str, str]:
    return {
        instrument.symbol: instrument.name
        for instrument in context.resolve("instruments").list_all()  # type: ignore[attr-defined]
    }


def symbol_with_name(symbol: str, names: dict[str, str]) -> str:
    name = names.get(symbol, "")
    return f"{name} {symbol}".strip()


def translate_reasons(codes: tuple[str, ...]) -> str:
    return "、".join(REASON_LABELS.get(code, code) for code in codes)


def percent(value: object) -> str:
    return f"{float(value) * 100:.0f}%"  # type: ignore[arg-type]


def yuan(value: object) -> str:
    return f"{float(value):,.2f} 元"  # type: ignore[arg-type]
