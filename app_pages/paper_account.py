"""模拟账户页: 资产概览、净值曲线、持仓、按信号确认模拟下单。"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import streamlit as st

from etf_quant_lab.contracts.enums import CostScenario, OrderSide
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.paper import PaperFillSource
from etf_quant_lab.domain.rebalance import build_rebalance_proposal
from etf_quant_lab.services.costs import load_cost_scenarios
from etf_quant_lab.ui.presenters import build_paper_account_view
from etf_quant_lab.ui.shared import (
    get_context,
    instrument_names,
    load_daily_bars,
    market_today,
    symbol_with_name,
    yuan,
)

st.title("模拟账户")
st.caption("虚拟资金跟踪信号效果。所有成交都是模拟的, 不涉及真实资金。")

context = get_context()
names = instrument_names(context)
paper = context.resolve("paper")
database = context.resolve("database")
today = market_today()

ACCOUNT_NAME = "PAPER_MAIN"
account = paper.get_account_by_name(ACCOUNT_NAME)  # type: ignore[attr-defined]

if account is None:
    st.info("还没有模拟账户。创建一个开始跟踪信号。")
    initial = st.number_input(
        "初始虚拟资金(元)", min_value=10000, value=1000000, step=100000
    )
    if st.button("创建模拟账户", type="primary", icon=":material/add_circle:"):
        paper.create_account(name=ACCOUNT_NAME, initial_cash=Decimal(int(initial)))  # type: ignore[attr-defined]
        st.rerun()
    st.stop()

view = build_paper_account_view(paper, database, account.account_id)  # type: ignore[arg-type]

if view.ledger_consistent is False:
    st.error(view.ledger_message + " 账户已冻结, 请检查运行日志。")

navs = paper.list_nav_snapshots(account.account_id)  # type: ignore[attr-defined]
latest_equity = navs[-1][3] if navs else None
profit = None if latest_equity is None else latest_equity - account.initial_cash

c1, c2, c3, c4 = st.columns(4)
c1.metric("总资产(最近估值)", yuan(latest_equity) if latest_equity else "尚未估值",
          help="现金 + 持仓市值。每日流水线运行后按收盘价更新。")
c2.metric(
    "累计盈亏",
    "—" if profit is None else yuan(profit),
    delta=None if profit is None else f"{float(profit / account.initial_cash) * 100:+.2f}%",
    help="相对初始资金的变化。",
)
c3.metric("可用现金", yuan(account.cash_balance))
c4.metric("账户状态", {"ACTIVE": "正常", "FROZEN": "已冻结", "RESET": "已重置"}.get(
    view.account_status or "", view.account_status or ""
))

if navs:
    st.subheader("净值曲线")
    st.line_chart(
        {"总资产(元)": {str(row[0]): float(row[3]) for row in navs}},
        height=260,
    )
else:
    st.info("净值曲线将在每日流水线运行后开始记录。")

st.subheader("当前持仓")
if not view.positions:
    st.info("暂无持仓。")
else:
    st.dataframe(
        [
            {
                "标的": symbol_with_name(row.symbol, names),
                "持有数量": row.quantity,
                "今日可卖": row.available_quantity,
                "T+1 待解冻": row.pending_quantity,
                "平均成本": row.average_cost,
            }
            for row in view.positions
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("「T+1 待解冻」是今天买入的部分, 按 A 股规则次一交易日才能卖出。")

st.subheader("按最新信号模拟建仓")
signals_repo = context.resolve("signals")
bars = load_daily_bars()
latest_bar_date = max((bar.trade_date for bar in bars), default=None)
signal = (
    None
    if latest_bar_date is None
    else signals_repo.find_latest_for_date(latest_bar_date)  # type: ignore[attr-defined]
)
if signal is None or not signal.items or signal.status != "VALID":
    st.info("暂无可执行的有效信号。先到「今日信号」页生成。")
else:
    current_positions = {row.symbol: row.quantity for row in view.positions}
    reference_prices = {
        item.symbol: item.reference_close
        for item in signal.items
        if item.reference_close is not None
    }
    target_weights = {item.symbol: item.target_weight for item in signal.items}
    cost_models = load_cost_scenarios(Path("config/cost_scenarios.yaml"))
    proposal = build_rebalance_proposal(
        target_weights=target_weights,
        current_positions=current_positions,
        cash=account.cash_balance,
        reference_prices=reference_prices,
        lot_sizes={symbol: 100 for symbol in set(target_weights) | set(current_positions)},
        cost_model=cost_models[CostScenario.NORMAL],
    )
    if not proposal.trades:
        st.success("当前持仓已符合信号目标, 无需调仓。")
    else:
        st.write(
            f"信号日 {signal.trade_date}; 预计调仓后剩余现金 {yuan(proposal.predicted_cash)}。"
        )
        st.dataframe(
            [
                {
                    "标的": symbol_with_name(trade.symbol, names),
                    "动作": "买入" if trade.side is OrderSide.BUY else "卖出",
                    "数量(份)": trade.quantity,
                    "参考价": float(trade.reference_price),
                    "预计现金影响": yuan(trade.estimated_cash_delta),
                }
                for trade in proposal.trades
            ],
            hide_index=True,
            width="stretch",
        )
        st.warning("确认后将按参考价立即记录模拟成交(近似下一开盘价), 该操作会写入账本。")
        if st.button("确认模拟下单", type="primary", icon=":material/task_alt:"):
            filled, errors = 0, []
            for trade in proposal.trades:
                try:
                    order = paper.propose_order(  # type: ignore[attr-defined]
                        account_id=account.account_id,
                        symbol=trade.symbol,
                        side=trade.side,
                        quantity=trade.quantity,
                        signal_id=signal.signal_id,
                    )
                    paper.record_fill(  # type: ignore[attr-defined]
                        order_id=order.order_id,
                        trade_date=signal.trade_date,
                        price=trade.reference_price,
                        commission=Decimal("5"),
                        source=PaperFillSource.NEXT_OPEN,
                    )
                    filled += 1
                except DomainError as error:
                    errors.append(f"{trade.symbol}: {error.message}")
            if errors:
                st.error("部分失败: " + "; ".join(errors))
            st.success(f"已模拟成交 {filled} 笔。重复点击不会重复成交(防重复机制)。")
            st.rerun()

st.subheader("最近订单")
if not view.orders:
    st.info("暂无订单记录。")
else:
    st.dataframe(
        [
            {
                "标的": symbol_with_name(row.symbol, names),
                "方向": "买入" if row.side == "BUY" else "卖出",
                "数量": row.quantity,
                "状态": {"FILLED": "已成交", "PROPOSED": "待确认", "REJECTED": "已拒绝",
                         "CANCELLED": "已取消", "CONFIRMED": "已确认"}.get(row.status, row.status),
            }
            for row in view.orders[:15]
        ],
        hide_index=True,
        width="stretch",
    )
