"""运行日志页: 任务执行记录与审计事件, 出问题先来这里。"""

from __future__ import annotations

import streamlit as st

from etf_quant_lab.ui.presenters import build_audit_trail, build_task_history
from etf_quant_lab.ui.shared import TASK_STATUS_LABELS, get_context, market_time_str

st.title("运行日志")
st.caption("每次自动/手动任务的执行记录。绿色成功, 红色失败, 失败原因都在这里。")

context = get_context()
database = context.resolve("database")

st.subheader("任务运行记录")
history = build_task_history(database)  # type: ignore[arg-type]
if not history.runs:
    st.info("暂无任务记录。运行每日流水线或调度器后这里会出现记录。")
else:
    st.dataframe(
        [
            {
                "任务": {"daily_pipeline": "每日流水线", "daily_signal": "每日信号",
                         "data_sync": "数据同步"}.get(run.task_name, run.task_name),
                "结果": TASK_STATUS_LABELS.get(run.status, run.status),
                "重试次数": run.retry_count,
                "摘要": str(run.result_summary) if run.result_summary else "",
                "失败原因": run.error_message or "",
            }
            for run in history.runs[:30]
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption("「已跳过(重复触发)」表示同名任务正在运行, 系统自动避免了重复执行。")

st.subheader("审计事件")
trail = build_audit_trail(database, limit=50)  # type: ignore[arg-type]
if not trail.events:
    st.info("暂无审计事件。")
else:
    st.dataframe(
        [
            {
                "时间": market_time_str(event["created_at"], "%m-%d %H:%M:%S"),
                "事件": {"TASK_SUCCEEDED": "任务成功", "TASK_FAILED": "任务失败",
                         "TASK_RECOVERED": "超时恢复"}.get(
                    str(event["event_type"]), str(event["event_type"])
                ),
                "级别": {"INFO": "信息", "WARN": "警告", "ERROR": "错误"}.get(
                    str(event["severity"]), str(event["severity"])
                ),
                "详情": str(event["payload"]),
            }
            for event in trail.events
        ],
        hide_index=True,
        width="stretch",
    )
