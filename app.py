"""ETF Quant Lab local Streamlit entry point (research & paper trading only)."""

from __future__ import annotations

import streamlit as st

st.set_page_config(
    page_title="ETF Quant Lab",
    page_icon=":material/query_stats:",
    layout="wide",
)

pages = [
    st.Page("app_pages/overview.py", title="总览", icon=":material/home:", default=True),
    st.Page("app_pages/data_center.py", title="数据中心", icon=":material/database:"),
    st.Page("app_pages/signals.py", title="今日信号", icon=":material/notifications_active:"),
    st.Page(
        "app_pages/paper_account.py",
        title="模拟账户",
        icon=":material/account_balance_wallet:",
    ),
    st.Page("app_pages/backtest_lab.py", title="回测实验室", icon=":material/science:"),
    st.Page("app_pages/run_logs.py", title="运行日志", icon=":material/receipt_long:"),
    st.Page("app_pages/help.py", title="帮助", icon=":material/help:"),
]

navigation = st.navigation(pages)
st.sidebar.caption("个人研究与模拟交易工具, 不是投资建议, 不连接真实券商。")
navigation.run()
