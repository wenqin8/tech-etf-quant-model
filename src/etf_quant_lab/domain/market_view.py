"""As-of-date-bounded market data view for strategies.

This module is the anti-lookahead boundary from STYLE §10: a strategy only ever
receives a :class:`MarketDataView`, which asserts at construction and on every
read that no bar is dated after ``as_of_date``.  Strategies cannot reach the
repository, the network, or the clock through it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.errors import DomainError

FUTURE_DATA_ACCESS = "STRAT_FUTURE_DATA_ACCESS"


@dataclass(frozen=True, slots=True)
class MarketDataView:
    """Immutable per-decision-date slice of canonical daily bars.

    ``bars`` must already be limited to ``as_of_date``; the constructor fails
    loudly (rather than silently filtering) when a future-dated bar sneaks in, so
    a wiring bug upstream cannot masquerade as a valid slice.
    """

    as_of_date: date
    bars: tuple[DailyBar, ...]
    _by_symbol: dict[str, tuple[DailyBar, ...]] = field(
        init=False,
        repr=False,
        compare=False,
        hash=False,
    )

    def __post_init__(self) -> None:
        future = [bar for bar in self.bars if bar.trade_date > self.as_of_date]
        if future:
            offender = max(future, key=lambda bar: bar.trade_date)
            raise DomainError(
                FUTURE_DATA_ACCESS,
                "市场数据切片包含晚于 as_of_date 的行情",
                details={
                    "as_of_date": self.as_of_date.isoformat(),
                    "max_trade_date": offender.trade_date.isoformat(),
                    "symbol": offender.symbol,
                },
            )
        grouped: dict[str, list[DailyBar]] = {}
        for bar in self.bars:
            grouped.setdefault(bar.symbol, []).append(bar)
        by_symbol = {
            symbol: tuple(sorted(symbol_bars, key=lambda item: item.trade_date))
            for symbol, symbol_bars in grouped.items()
        }
        object.__setattr__(self, "_by_symbol", by_symbol)

    @property
    def symbols(self) -> tuple[str, ...]:
        """Symbols present in this slice, sorted for deterministic iteration."""

        return tuple(sorted(self._by_symbol))

    def history(self, symbol: str, *, max_bars: int | None = None) -> tuple[DailyBar, ...]:
        """Return one symbol's bars ascending by date, optionally tail-limited."""

        bars = self._by_symbol.get(symbol, ())
        if max_bars is not None:
            if max_bars < 0:
                raise ValueError("max_bars must not be negative")
            bars = bars[-max_bars:] if max_bars else ()
        for bar in bars:  # Defensive re-assertion: reads can never leak the future.
            if bar.trade_date > self.as_of_date:
                raise DomainError(
                    FUTURE_DATA_ACCESS,
                    "读取到晚于 as_of_date 的行情",
                    details={
                        "as_of_date": self.as_of_date.isoformat(),
                        "symbol": symbol,
                        "trade_date": bar.trade_date.isoformat(),
                    },
                )
        return bars

    def latest(self, symbol: str) -> DailyBar | None:
        """Return the most recent bar at or before ``as_of_date`` for one symbol."""

        bars = self._by_symbol.get(symbol, ())
        return bars[-1] if bars else None

    def bar_count(self, symbol: str) -> int:
        """Return how many historical bars this slice holds for one symbol."""

        return len(self._by_symbol.get(symbol, ()))
