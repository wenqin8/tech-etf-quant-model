"""今日信号页: 选择策略、生成信号、理解每一条建议。"""

from __future__ import annotations

from decimal import Decimal

import streamlit as st

from etf_quant_lab.contracts.enums import StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.contracts.signal import GenerateDailySignalRequest, SignalStatus
from etf_quant_lab.services.daily_pipeline import DEFAULT_PARAMETERS
from etf_quant_lab.ui.shared import (
    get_context,
    instrument_names,
    load_daily_bars,
    market_today,
    percent,
    symbol_with_name,
    translate_reasons,
)

st.title("今日信号")
st.caption("信号 = 策略根据最新收盘数据给出的目标持仓建议。仅供研究, 不会自动下单。")

context = get_context()
names = instrument_names(context)
today = market_today()

TREND_PARAMS: dict[str, object] = {
    "fast_window": 20,
    "slow_window": 60,
    "maximum_position_weight": Decimal("0.25"),
    "minimum_cash_weight": Decimal("0.10"),
}
STRATEGY_CHOICES = {
    "ETF 轮动 (持有动量最强的 3 只)": (StrategyId.ETF_ROTATION, DEFAULT_PARAMETERS),
    "趋势基准 (均线上方等权持有)": (StrategyId.TREND_BASELINE, TREND_PARAMS),
}

choice = st.selectbox(
    "选择策略",
    list(STRATEGY_CHOICES),
    help="两个策略都基于收盘数据、次日开盘执行的保守假设; 参数与预设配置一致。",
)
strategy_id, parameters = STRATEGY_CHOICES[choice]

with st.expander("查看策略参数(当前使用预设, 修改请编辑 config/strategy_presets.yaml)"):
    st.json({key: str(value) for key, value in parameters.items()})

bars = load_daily_bars()
latest_bar_date = max((bar.trade_date for bar in bars), default=None)
if latest_bar_date is None:
    st.warning("还没有行情数据。请先到数据中心同步。")
    st.stop()

target_date = latest_bar_date
st.write(f"将基于最新收盘数据 (**{target_date}**) 生成信号。")

if st.button("生成信号", type="primary", icon=":material/bolt:"):
    try:
        with st.spinner("计算中 ..."):
            signal = context.resolve("signal").generate_daily(  # type: ignore[attr-defined]
                GenerateDailySignalRequest(
                    trade_date=target_date,
                    strategy_id=strategy_id,
                    strategy_version="1.0.0",
                    parameters=parameters,
                )
            )
        st.session_state["latest_signal_id"] = signal.signal_id
        st.rerun()
    except DomainError as error:
        st.error(f"生成失败: {error.message} ({error.code})")

signal = context.resolve("signals").find_latest_for_date(target_date)  # type: ignore[attr-defined]
if signal is None:
    st.info("该交易日还没有信号。点击上方「生成信号」。")
    st.stop()

st.subheader(f"{signal.trade_date} 信号")
if signal.status == SignalStatus.BLOCKED:
    st.error(f"信号被阻断: {signal.blocked_reason}。数据问题解决前不会给出持仓建议。")
    st.stop()

st.success(
    f"信号有效。共 {len(signal.items)} 条目标仓位, "
    f"建议保留现金 {percent(signal.target_cash_weight)}。"
    " 相同数据重复生成会得到同一份信号(防重复机制)。"
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
                "动量评分": None if item.score is None else round(float(item.score), 3),
                "入选原因": translate_reasons(item.reason_codes),
            }
            for item in signal.items
        ],
        hide_index=True,
        width="stretch",
    )
st.caption(
    "动量评分越高代表近期风险调整后涨势越强。想按信号模拟建仓? 前往「模拟账户」页确认下单。"
)
