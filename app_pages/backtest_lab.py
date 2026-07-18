"""回测实验室页: 用真实历史检验策略, 理解收益与风险指标。"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import streamlit as st

from etf_quant_lab.contracts.enums import CostScenario, StrategyId
from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.services.backtest import BacktestRequest, run_backtest
from etf_quant_lab.services.costs import load_cost_scenarios
from etf_quant_lab.services.daily_pipeline import DEFAULT_PARAMETERS
from etf_quant_lab.ui.shared import get_context, load_daily_bars

st.title("回测实验室")
st.caption("回测 = 用真实历史数据检验策略过去的表现。历史表现不代表未来收益。")

context = get_context()

TREND_PARAMS: dict[str, object] = {
    "fast_window": 20,
    "slow_window": 60,
    "maximum_position_weight": Decimal("0.25"),
    "minimum_cash_weight": Decimal("0.10"),
}
STRATEGY_CHOICES = {
    "ETF 轮动": (StrategyId.ETF_ROTATION, DEFAULT_PARAMETERS),
    "趋势基准": (StrategyId.TREND_BASELINE, TREND_PARAMS),
}
COST_LABELS = {
    "理想(无成本, 仅供对照)": CostScenario.IDEAL,
    "正常(推荐)": CostScenario.NORMAL,
    "悲观(高费用压力测试)": CostScenario.PESSIMISTIC,
}

bars = load_daily_bars()
if not bars:
    st.warning("没有行情数据, 请先到数据中心同步。")
    st.stop()
latest = max(bar.trade_date for bar in bars)
earliest = min(bar.trade_date for bar in bars)
symbols = tuple(sorted({bar.symbol for bar in bars}))
st.write(f"可用数据: {len(symbols)} 只标的, {earliest} → {latest}。")

col1, col2, col3 = st.columns(3)
with col1:
    strategy_label = st.selectbox("策略", list(STRATEGY_CHOICES))
with col2:
    years = st.slider(
        "回测年数", 1, 5, 3, help="不含策略预热所需的历史。上限取决于已同步的数据长度。"
    )
with col3:
    cost_label = st.selectbox(
        "成本情景", list(COST_LABELS), index=1,
        help="包含佣金与滑点假设。结论请以「正常」或「悲观」为准, 不要只看「理想」。",
    )

if st.button("运行回测", type="primary", icon=":material/play_arrow:"):
    strategy_id, parameters = STRATEGY_CHOICES[strategy_label]
    cost_models = load_cost_scenarios(Path("config/cost_scenarios.yaml"))
    start = max(earliest + timedelta(days=250), latest - timedelta(days=years * 365))
    request = BacktestRequest(
        strategy_id=strategy_id,
        strategy_version="1.0.0",
        parameters=parameters,
        symbols=symbols,
        start_date=start,
        end_date=latest,
        initial_cash=Decimal("1000000"),
        cost_model=cost_models[COST_LABELS[cost_label]],
        lot_sizes={symbol: 100 for symbol in symbols},
        rebalance_every_bars=5,
    )
    try:
        with st.spinner("回测运行中(全程本地计算) ..."):
            result = run_backtest(context.resolve("strategy"), request=request, bars=bars)  # type: ignore[arg-type]
    except DomainError as error:
        st.error(f"回测失败: {error.message} ({error.code})")
        st.stop()

    metrics = result.metrics
    st.subheader(f"{strategy_label} · {metrics.start_date} → {metrics.end_date}")

    def fmt(value: float | None, *, pct: bool = True) -> str:
        if value is None:
            return "—"
        return f"{value * 100:.2f}%" if pct else f"{value:.2f}"

    r1c1, r1c2, r1c3, r1c4 = st.columns(4)
    r1c1.metric("累计收益", fmt(metrics.total_return),
                help="整个回测期的总收益率。")
    r1c2.metric("年化收益", fmt(metrics.annual_return),
                help="折算成每年的平均收益率, 便于跨期比较。")
    r1c3.metric("最大回撤", fmt(metrics.max_drawdown),
                help="净值从最高点跌到最低点的最大幅度。数值越小(越接近0)越好。")
    r1c4.metric("夏普比率", fmt(metrics.sharpe_ratio, pct=False),
                help="每承担一份波动换来多少收益。>1 优秀, 0.5~1 尚可, <0 说明亏损。")

    r2c1, r2c2, r2c3, r2c4 = st.columns(4)
    r2c1.metric("年化波动", fmt(metrics.annual_volatility),
                help="净值波动的剧烈程度, 越大意味着过程越颠簸。")
    r2c2.metric("最长水下期", f"{metrics.longest_underwater_days} 天",
                help="净值创新高之前最久等了多少个交易日。")
    r2c3.metric("交易次数", str(metrics.trade_count))
    r2c4.metric("总交易成本", f"{float(metrics.cost_total):,.0f} 元",
                help="佣金与滑点合计。次数越多成本侵蚀越大。")

    st.subheader("净值曲线")
    st.line_chart(
        {
            "净值(元)": {
                str(record.trade_date): float(record.total_equity)
                for record in result.ledger.records
            }
        },
        height=300,
    )
    if metrics.notes:
        st.caption("指标注记: " + ", ".join(metrics.notes))
    st.caption(
        "回测按 T 日收盘决策、T+1 开盘成交的保守假设, 已计入手数取整/现金约束/停牌不可成交。"
        " 同样输入重复运行结果完全一致。"
    )
