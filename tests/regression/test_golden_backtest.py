"""Golden-data regression: fixed sample, pinned result hash, tamper detection.

The dataset below is deterministic and committed with the test.  If any change
to strategy, execution, ledger or metric code alters the backtest outcome, the
pinned digest changes and this test fails — the doc's acceptance rule
"修改一条固定历史行情后, 回归测试能发现差异" is asserted directly.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from etf_quant_lab.contracts.data import DailyBar
from etf_quant_lab.contracts.enums import CostScenario, DataSource, Exchange, StrategyId
from etf_quant_lab.contracts.execution import CostModel
from etf_quant_lab.domain.strategies.trend_baseline import TrendBaselineStrategy
from etf_quant_lab.domain.strategy_registry import StrategyRegistry
from etf_quant_lab.services.backtest import BacktestRequest, BacktestResult, run_backtest
from etf_quant_lab.services.strategy import StrategyService

INGESTED_AT = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)
START = date(2026, 6, 1)

# Pinned digest of the golden run.  Update ONLY when an intentional behavior
# change is reviewed; the new value must be justified in the commit message.
GOLDEN_DIGEST = "093f7460ada21a3607be23afb58fba6ebd18c171ce6e86f17ca53a0aba905d31"

_CLOSES = [
    "4.00", "4.03", "3.98", "4.05", "4.10", "4.08", "4.15", "4.20", "4.18", "4.25",
    "4.30", "4.28", "4.35", "4.31", "4.40", "4.45", "4.42", "4.50", "4.55", "4.60",
]

NORMAL_COST = CostModel(
    scenario=CostScenario.NORMAL,
    commission_rate=Decimal("0.00025"),
    minimum_commission=Decimal("5"),
    slippage_bps=Decimal("5"),
)


def _golden_bars() -> tuple[DailyBar, ...]:
    bars = []
    for offset, close in enumerate(_CLOSES):
        close_price = Decimal(close)
        open_price = close_price - Decimal("0.01")
        bars.append(
            DailyBar(
                symbol="510300.SH",
                trade_date=START + timedelta(days=offset),
                exchange=Exchange.SSE,
                open=open_price,
                high=close_price + Decimal("0.02"),
                low=open_price - Decimal("0.02"),
                close=close_price,
                volume=Decimal("10000"),
                amount=Decimal("41000"),
                source=DataSource.TUSHARE,
                batch_id="01K0D7F7P6XQ4M2Z8H9B3C5NV1",
                ingested_at=INGESTED_AT,
            )
        )
    return tuple(bars)


def _run(bars: tuple[DailyBar, ...]) -> BacktestResult:
    registry = StrategyRegistry()
    registry.register(TrendBaselineStrategy())
    return run_backtest(
        StrategyService(registry),
        request=BacktestRequest(
            strategy_id=StrategyId.TREND_BASELINE,
            strategy_version="1.0.0",
            parameters={
                "fast_window": 3,
                "slow_window": 5,
                "maximum_position_weight": Decimal("0.6"),
                "minimum_cash_weight": Decimal("0.1"),
            },
            symbols=("510300.SH",),
            start_date=START + timedelta(days=6),
            end_date=START + timedelta(days=19),
            initial_cash=Decimal("100000"),
            cost_model=NORMAL_COST,
            lot_sizes={"510300.SH": 100},
        ),
        bars=bars,
    )


def _digest(result: BacktestResult) -> str:
    """Serialize the audit-relevant outcome into a stable SHA-256."""

    payload = {
        "records": [
            {
                "date": record.trade_date.isoformat(),
                "cash": str(record.cash),
                "equity": str(record.total_equity),
                "positions": {k: v for k, v in sorted(record.positions.items())},
            }
            for record in result.ledger.records
        ],
        "trades": [
            {
                "date": dated.trade_date.isoformat(),
                "symbol": dated.trade.symbol,
                "side": dated.trade.side.value,
                "quantity": dated.trade.quantity,
                "price": str(dated.trade.executed_price),
                "commission": str(dated.trade.commission),
                "cash_delta": str(dated.trade.cash_delta),
            }
            for dated in result.ledger.trades
        ],
        "metrics": {
            "total_return": _round(result.metrics.total_return),
            "max_drawdown": _round(result.metrics.max_drawdown),
            "trade_count": result.metrics.trade_count,
            "cost_total": str(result.metrics.cost_total),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def _round(value: float | None) -> str | None:
    return None if value is None else f"{value:.10f}"


def test_golden_backtest_digest_is_stable() -> None:
    result = _run(_golden_bars())

    assert _digest(result) == GOLDEN_DIGEST, (
        "回归哈希变化: 若为有意的行为变更, 请在评审后更新 GOLDEN_DIGEST 并在提交说明中注明原因"
    )


def test_tampering_one_close_changes_the_digest() -> None:
    bars = list(_golden_bars())
    original = bars[10]
    bars[10] = DailyBar(
        symbol=original.symbol,
        trade_date=original.trade_date,
        exchange=original.exchange,
        open=original.open,
        high=original.high + Decimal("0.50"),
        low=original.low,
        close=original.close + Decimal("0.50"),  # the tampered close
        volume=original.volume,
        amount=original.amount,
        source=original.source,
        batch_id=original.batch_id,
        ingested_at=original.ingested_at,
    )

    tampered = _run(tuple(bars))

    assert _digest(tampered) != GOLDEN_DIGEST


def test_golden_run_is_reproducible_within_session() -> None:
    bars = _golden_bars()

    assert _digest(_run(bars)) == _digest(_run(bars))
