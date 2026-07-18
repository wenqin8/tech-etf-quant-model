"""Generate the first real daily signal and seed a paper account to track it.

    uv run python scripts/first_signal_and_paper.py --strategy etf_rotation

Idempotent: re-running returns the same signal (same context) and reuses the
existing paper account by name.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

from etf_quant_lab.composition import build_full_context
from etf_quant_lab.contracts.enums import CostScenario, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.paper import PaperFillSource
from etf_quant_lab.contracts.signal import GenerateDailySignalRequest, SignalStatus
from etf_quant_lab.domain.rebalance import build_rebalance_proposal
from etf_quant_lab.services.costs import load_cost_scenarios

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
TREND_PARAMS: dict[str, object] = {
    "fast_window": 20,
    "slow_window": 60,
    "maximum_position_weight": Decimal("0.25"),
    "minimum_cash_weight": Decimal("0.10"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="生成首个真实信号并开模拟账户")
    parser.add_argument(
        "--strategy",
        choices=("etf_rotation", "trend_baseline"),
        default="etf_rotation",
    )
    parser.add_argument("--account", default="PAPER_MAIN")
    parser.add_argument("--cash", default="1000000")
    args = parser.parse_args()

    context = build_full_context()
    bars = context.batches.query_daily_bars()  # type: ignore[attr-defined]
    if not bars:
        print("没有激活行情, 请先运行 scripts/bootstrap_market_data.py")
        return 1
    latest = max(bar.trade_date for bar in bars)
    print(f"最新行情日: {latest}  激活标的 {len({b.symbol for b in bars})} 只")

    if args.strategy == "etf_rotation":
        strategy_id, params = StrategyId.ETF_ROTATION, ROTATION_PARAMS
    else:
        strategy_id, params = StrategyId.TREND_BASELINE, TREND_PARAMS

    print(f"\n1) 生成 {strategy_id.value} 信号 (交易日 {latest}) ...")
    try:
        signal = context.signal.generate_daily(  # type: ignore[attr-defined]
            GenerateDailySignalRequest(
                trade_date=latest,
                strategy_id=strategy_id,
                strategy_version="1.0.0",
                parameters=params,
            )
        )
    except DomainError as error:
        print(f"信号生成失败: {error.code} - {error.message}")
        return 1

    print(f"   信号 {signal.signal_id}  状态 {signal.status}  风险 {signal.risk_state.value}")
    if signal.status == SignalStatus.BLOCKED:
        print(f"   阻断原因: {signal.blocked_reason}")
        return 0
    print(f"   目标现金比例 {signal.target_cash_weight}")
    for item in signal.items:
        print(
            f"   {item.action.value:4} {item.symbol}  目标权重 {item.target_weight}  "
            f"参考收盘 {item.reference_close}  评分 {item.score}  "
            f"原因 {','.join(item.reason_codes)}"
        )

    print(f"\n2) 开/复用模拟账户 {args.account} ...")
    account = context.paper.get_account_by_name(args.account)  # type: ignore[attr-defined]
    if account is None:
        account = context.paper.create_account(name=args.account, initial_cash=Decimal(args.cash))  # type: ignore[attr-defined]
        print(f"   新建账户 {account.account_id}  现金 {account.cash_balance}")
    else:
        print(f"   复用账户 {account.account_id}  现金 {account.cash_balance}")

    if not signal.items:
        print("\n   信号无目标持仓(全部持币), 无需下单。")
        return 0

    print("\n3) 生成调仓建议 ...")
    reference_prices = {
        item.symbol: item.reference_close
        for item in signal.items
        if item.reference_close
    }
    target_weights = {item.symbol: item.target_weight for item in signal.items}
    cost_models = load_cost_scenarios(Path("config/cost_scenarios.yaml"))
    proposal = build_rebalance_proposal(
        target_weights=target_weights,
        current_positions={},
        cash=account.cash_balance,
        reference_prices=reference_prices,
        lot_sizes={s: 100 for s in target_weights},
        cost_model=cost_models[CostScenario.NORMAL],
    )
    print(f"   总权益 {proposal.total_equity}  预测调仓后现金 {proposal.predicted_cash}")
    for trade in proposal.trades:
        print(
            f"   建议 {trade.side.value:4} {trade.symbol}  {trade.quantity} 份  "
            f"@ {trade.reference_price}  现金影响 {trade.estimated_cash_delta}  "
            f"达成权重 {trade.achieved_weight}"
        )
    for skip in proposal.skipped:
        print(f"   跳过 {skip.symbol}: {skip.reason} {skip.detail}")

    print("\n4) 确认并模拟成交(下一开盘价用参考收盘近似) ...")
    filled = 0
    for trade in proposal.trades:
        order = context.paper.propose_order(  # type: ignore[attr-defined]
            account_id=account.account_id,
            symbol=trade.symbol,
            side=trade.side,
            quantity=trade.quantity,
            signal_id=signal.signal_id,
        )
        context.paper.record_fill(  # type: ignore[attr-defined]
            order_id=order.order_id,
            trade_date=latest,
            price=trade.reference_price,
            commission=Decimal("5"),
            source=PaperFillSource.NEXT_OPEN,
        )
        filled += 1
    refreshed = context.paper.get_account(account.account_id)  # type: ignore[attr-defined]
    print(f"   已成交 {filled} 笔, 账户现金 {refreshed.cash_balance}")
    cash, positions = context.paper.recompute_from_ledger(account.account_id)  # type: ignore[attr-defined]
    print(f"   账本重算校验: 现金 {cash}  持仓 {positions}")
    print("\n完成: 首个真实信号与模拟账户已建立。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
