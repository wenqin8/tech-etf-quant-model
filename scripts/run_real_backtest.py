"""Run the two released strategies over real history and print an honest report.

Uses the active canonical bars in DuckDB (populated by
``bootstrap_market_data.py``).  Both strategies run under the NORMAL cost
scenario over the same window so results are directly comparable.

    uv run python scripts/run_real_backtest.py --years 2
"""

from __future__ import annotations

import argparse
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.enums import CostScenario, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.performance import PerformanceMetrics
from etf_quant_lab.services.backtest import BacktestRequest, run_backtest
from etf_quant_lab.services.costs import load_cost_scenarios

TREND_PARAMS: dict[str, object] = {
    "fast_window": 20,
    "slow_window": 60,
    "maximum_position_weight": Decimal("0.25"),
    "minimum_cash_weight": Decimal("0.10"),
}
ROTATION_PARAMS: dict[str, object] = {
    "momentum_window_short": 20,
    "momentum_window_medium": 60,
    "momentum_window_long": 120,
    "volatility_window": 20,
    "trend_filter_days": 120,
    "holdings_count": 3,
    "maximum_position_weight": Decimal("0.35"),
    "minimum_cash_weight": Decimal("0.10"),
    "cash_when_all_filtered": True,
}


def _fmt(value: float | None, *, percent: bool = True) -> str:
    if value is None:
        return "  n/a "
    return f"{value * 100:6.2f}%" if percent else f"{value:6.2f}"


def _print_metrics(label: str, metrics: PerformanceMetrics) -> None:
    print(f"\n=== {label} ===")
    print(f"区间: {metrics.start_date} → {metrics.end_date}  有效交易日 {metrics.effective_days}")
    print(f"累计收益 {_fmt(metrics.total_return)}   年化 {_fmt(metrics.annual_return)}")
    print(
        f"年化波动 {_fmt(metrics.annual_volatility)}   "
        f"夏普 {_fmt(metrics.sharpe_ratio, percent=False)}   "
        f"卡玛 {_fmt(metrics.calmar_ratio, percent=False)}"
    )
    print(
        f"最大回撤 {_fmt(metrics.max_drawdown)}   "
        f"最长水下 {metrics.longest_underwater_days} 天"
    )
    print(
        f"交易次数 {metrics.trade_count}   总成本 {metrics.cost_total}   "
        f"换手 {_fmt(metrics.turnover, percent=False)}"
    )
    if metrics.notes:
        print(f"注记: {', '.join(metrics.notes)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="真实历史回测")
    parser.add_argument("--years", type=int, default=2, help="回测年数(不含预热)")
    parser.add_argument("--cash", default="1000000", help="初始资金")
    args = parser.parse_args()

    context = build_full_context()
    bars = context.batches.query_daily_bars()  # type: ignore[attr-defined]
    if not bars:
        print("数据库中没有激活行情, 请先运行 scripts/bootstrap_market_data.py")
        return 1
    latest = max(bar.trade_date for bar in bars)
    earliest = min(bar.trade_date for bar in bars)
    symbols = tuple(sorted({bar.symbol for bar in bars}))
    print(f"激活行情: {len(bars)} 行, {len(symbols)} 标的, {earliest} → {latest}")

    start = max(earliest + timedelta(days=250), latest - timedelta(days=args.years * 365))
    cost_models = load_cost_scenarios(Path("config/cost_scenarios.yaml"))
    normal_cost = cost_models[CostScenario.NORMAL]
    lot_sizes = {symbol: 100 for symbol in symbols}

    runs = (
        ("趋势基准 TREND_BASELINE (NORMAL 成本)", StrategyId.TREND_BASELINE, TREND_PARAMS),
        ("ETF 轮动 ETF_ROTATION (NORMAL 成本)", StrategyId.ETF_ROTATION, ROTATION_PARAMS),
    )
    for label, strategy_id, params in runs:
        request = BacktestRequest(
            strategy_id=strategy_id,
            strategy_version="1.0.0",
            parameters=params,
            symbols=symbols,
            start_date=start,
            end_date=latest,
            initial_cash=Decimal(args.cash),
            cost_model=normal_cost,
            lot_sizes=lot_sizes,
            rebalance_every_bars=5,  # weekly cadence per strategy presets
        )
        try:
            result = run_backtest(context.strategy, request=request, bars=bars)  # type: ignore[attr-defined]
        except DomainError as error:
            print(f"\n=== {label} ===\n回测失败: {error.code} - {error.message}")
            continue
        _print_metrics(label, result.metrics)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
