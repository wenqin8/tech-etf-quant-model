"""Provider protocol shared by Tushare, AKShare and test doubles."""

from __future__ import annotations

from typing import Protocol

from etf_quant_lab.contracts.data import DailyBarsQuery, RawProviderBatch, TradeCalendarQuery
from etf_quant_lab.contracts.enums import DataSource


class MarketDataProvider(Protocol):
    """Minimal external-data contract; implementations must not persist data directly."""

    @property
    def source(self) -> DataSource: ...

    def fetch_daily_bars(self, query: DailyBarsQuery) -> RawProviderBatch: ...

    def fetch_trade_calendar(self, query: TradeCalendarQuery) -> RawProviderBatch: ...

