"""总览页: 回答"今天系统状态如何, 我该做什么"。"""

from __future__ import annotations

import streamlit as st

from etf_quant_lab.ui.presenters import build_dashboard
from etf_quant_lab.ui.shared import (
    BATCH_STATUS_LABELS,
    GATE_LABELS,
    get_context,
    instrument_names,
    load_daily_bars,
    market_today,
    percent,
    symbol_with_name,
    translate_reasons,
)

st.title("总览")
st.caption("一眼看懂: 数据新不新、信号有没有、下一步做什么。")

context = get_context()
today = market_today()
view = build_dashboard(
    as_of=today,  # type: ignore[arg-type]
    batch_repository=context.resolve("batches"),  # type: ignore[arg-type]
    calendar_repository=context.resolve("calendar"),  # type: ignore[arg-type]
    quality_service=context.resolve("quality"),  # type: ignore[arg-type]
    signal_repository=context.resolve("signals"),  # type: ignore[arg-type]
    bars=load_daily_bars(),
)
names = instrument_names(context)

if view.data_ready:
    st.success(view.banner)
else:
    st.warning(view.banner)

left, middle, right = st.columns(3)
left.metric(
    "最新行情日",
    str(view.latest_bar_date or "无数据"),
    help="数据库里最近一个交易日的收盘数据。收盘后同步则等于今天。",
)
middle.metric(
    "最近同步状态",
    BATCH_STATUS_LABELS.get(
        view.latest_batch.status.value if view.latest_batch else "", "无记录"
    ),
    help="最近一次数据同步的结果。「已激活」表示数据可用。",
)
right.metric(
    "数据质量检查",
    GATE_LABELS.get(view.gate_status, "未运行") if view.gate_status else "未运行",
    help="自动质量检查结果。「通过(有提示)」不影响使用; 「未通过」的数据不会被采用。",
)

st.subheader("下一步建议")
if view.latest_bar_date is None:
    st.markdown("1. 前往 **数据中心** 页, 点击「同步最新数据」。")
elif not view.data_ready:
    st.markdown("1. 数据有问题, 前往 **数据中心** 查看原因并重新同步。")
elif view.open_signal is None:
    st.markdown("1. 数据已就绪, 前往 **今日信号** 页生成信号。")
else:
    st.markdown(
        "1. 查看下方最新信号; 需要跟随时前往 **模拟账户** 页确认模拟下单。\n"
        "2. 每个交易日收盘后(约 16:30)回到 **数据中心** 点一次「运行每日流水线」,"
        " 或让 `scripts/run_scheduler.py` 在后台自动完成。"
    )

signal = view.open_signal
st.subheader("最新信号" + (f" ({signal.trade_date})" if signal else ""))
if signal is None:
    st.info("尚未生成信号。")
elif signal.status == "BLOCKED":
    st.error(f"该日信号被阻断: {signal.blocked_reason}")
else:
    st.write(
        f"共 {len(signal.items)} 条目标仓位, 建议保留现金 {percent(signal.target_cash_weight)}。"
    )
    if signal.items:
        st.dataframe(
            [
                {
                    "标的": symbol_with_name(item.symbol, names),
                    "建议动作": {"BUY": "买入", "SELL": "卖出", "HOLD": "持有"}.get(
                        item.action.value, item.action.value
                    ),
                    "目标权重": percent(item.target_weight),
                    "参考收盘价": None
                    if item.reference_close is None
                    else float(item.reference_close),
                    "入选原因": translate_reasons(item.reason_codes),
                }
                for item in signal.items
            ],
            hide_index=True,
            width="stretch",
        )
    st.caption(
        "目标权重 = 该标的应占总资产的比例。参考收盘价仅用于解释, 模拟成交按下一交易日开盘价。"
    )
