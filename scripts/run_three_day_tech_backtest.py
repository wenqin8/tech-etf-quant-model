r"""Run the focused regime-aware technology ETF strategy on canonical history.

The script is read-only with respect to market data.  Signals are decided at
the close and executed by the shared backtest engine at the next session open.

    .\.venv\Scripts\python.exe .\scripts\run_three_day_tech_backtest.py
"""

from __future__ import annotations

import argparse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import cast

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.enums import CostScenario, StrategyId
from etf_quant_lab.contracts.performance import PerformanceMetrics
from etf_quant_lab.domain.strategies.three_day_tech import (
    CHIP_SYMBOLS,
    EQUIPMENT_SYMBOLS,
    SATELLITE_SYMBOLS,
)
from etf_quant_lab.services.backtest import BacktestRequest, run_backtest
from etf_quant_lab.services.costs import load_cost_scenarios
from etf_quant_lab.services.strategy import StrategyService

SYMBOLS = (*EQUIPMENT_SYMBOLS, *CHIP_SYMBOLS, *SATELLITE_SYMBOLS)


def _parameters(args: argparse.Namespace) -> dict[str, object]:
    return {
        "pullback_window": 3,
        "pullback_threshold": Decimal(args.pullback),
        "require_reversal_confirmation": True,
        "trend_fast_window": 20,
        "trend_slow_window": 60,
        "sideways_band": Decimal("0.02"),
        "swing_holding_bars": args.swing_days,
        "atr_window": args.atr_window,
        "initial_stop_atr_multiple": Decimal(args.atr_stop_multiple),
        "breakeven_trigger_atr_multiple": Decimal(args.breakeven_atr),
        "time_stop_bars": args.time_stop_days,
        "time_stop_min_return": Decimal(args.time_stop_return),
        "first_profit_atr_multiple": Decimal(args.first_profit_atr),
        "first_profit_remaining_fraction": Decimal("0.70"),
        "trailing_profit_drawdown": Decimal(args.trailing_drawdown),
        "second_profit_remaining_fraction": Decimal("0.20"),
        "volume_window": 20,
        "volume_surge_multiple": Decimal(args.volume_surge),
        "upper_shadow_body_multiple": Decimal("2"),
        "acceleration_daily_return": Decimal(args.acceleration_return),
        "bollinger_window": 20,
        "bollinger_std_multiple": Decimal("2"),
        "extreme_profit": Decimal(args.extreme_profit),
        "extreme_daily_drop": Decimal(args.extreme_drop),
        "equipment_weight": Decimal("0.40"),
        "equipment_entry_weight": Decimal("0.20"),
        "chip_weight": Decimal("0.30"),
        "chip_entry_weight": Decimal("0.10"),
        "satellite_weight": Decimal("0.15"),
        "satellite_entry_weight": Decimal("0.05"),
        "minimum_cash_weight": Decimal("0.15"),
    }


def _fmt(value: float | None, *, percent: bool = True) -> str:
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%" if percent else f"{value:.2f}"


def _print_metrics(metrics: PerformanceMetrics) -> None:
    print(f"区间: {metrics.start_date} → {metrics.end_date} ({metrics.effective_days}日)")
    print(
        f"累计收益 {_fmt(metrics.total_return)}  年化 {_fmt(metrics.annual_return)}  "
        f"最大回撤 {_fmt(metrics.max_drawdown)}"
    )
    print(
        f"年化波动 {_fmt(metrics.annual_volatility)}  "
        f"夏普 {_fmt(metrics.sharpe_ratio, percent=False)}  "
        f"卡玛 {_fmt(metrics.calmar_ratio, percent=False)}"
    )
    print(
        f"交易次数 {metrics.trade_count}  总成本 {metrics.cost_total}  "
        f"换手 {_fmt(metrics.turnover, percent=False)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="科技ETF状态自适应回撤策略回测")
    parser.add_argument("--years", type=int, default=3)
    parser.add_argument("--cash", default="15000")
    parser.add_argument("--pullback", default="-0.075")
    parser.add_argument("--swing-days", type=int, default=5)
    parser.add_argument("--atr-window", type=int, default=10)
    parser.add_argument("--atr-stop-multiple", default="3.0")
    parser.add_argument("--breakeven-atr", default="1.5")
    parser.add_argument("--time-stop-days", type=int, default=5)
    parser.add_argument("--time-stop-return", default="0.02")
    parser.add_argument("--first-profit-atr", default="3")
    parser.add_argument("--trailing-drawdown", default="0.09")
    parser.add_argument("--volume-surge", default="2")
    parser.add_argument("--acceleration-return", default="0.05")
    parser.add_argument("--extreme-profit", default="0.50")
    parser.add_argument("--extreme-drop", default="0.07")
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args()

    context = build_full_context()
    bars = context.batches.query_daily_bars(symbols=SYMBOLS)  # type: ignore[attr-defined]
    present = {bar.symbol for bar in bars}
    missing = sorted(set(SYMBOLS) - present)
    if missing:
        print("缺少行情: " + ", ".join(missing))
        return 1

    earliest = min(bar.trade_date for bar in bars)
    data_latest = max(bar.trade_date for bar in bars)
    latest = min(args.end_date, data_latest) if args.end_date else data_latest
    # Reserve roughly one slow-window warmup before the measured interval.
    default_start = max(
        earliest + timedelta(days=90),
        latest - timedelta(days=args.years * 365),
    )
    start = max(args.start_date, earliest) if args.start_date else default_start
    if start >= latest:
        parser.error("start-date 必须早于 end-date 和最新行情日")
    costs = load_cost_scenarios(Path("config/cost_scenarios.yaml"))
    request = BacktestRequest(
        strategy_id=StrategyId.THREE_DAY_TECH,
        strategy_version="1.0.0",
        parameters=_parameters(args),
        symbols=SYMBOLS,
        start_date=start,
        end_date=latest,
        initial_cash=Decimal(args.cash),
        cost_model=costs[CostScenario.NORMAL],
        lot_sizes={symbol: 100 for symbol in SYMBOLS},
        rebalance_every_bars=1,
    )
    strategy_service = cast(StrategyService, context.resolve("strategy"))
    result = run_backtest(strategy_service, request=request, bars=bars)

    print("=== THREE_DAY_TECH / NORMAL成本 ===")
    print(
        "核心底仓: 设备20% + 芯片10%; 机会仓: 创新药或机器人5%; "
        "上限40%/30%/15%; 最低现金15%"
    )
    print(
        f"回撤{args.pullback}  震荡/下跌最长{args.swing_days}日  "
        f"初始止损{args.atr_stop_multiple}ATR  保本触发{args.breakeven_atr}ATR  "
        f"首批止盈{args.first_profit_atr}ATR  回撤止盈{args.trailing_drawdown}"
    )
    _print_metrics(result.metrics)
    if result.ledger.records:
        final = result.ledger.records[-1]
        print(f"期末权益 {final.total_equity}  期末现金 {final.cash}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
