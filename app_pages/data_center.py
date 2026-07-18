"""数据中心页: 同步数据、查看批次与质量结果、一键每日流水线。"""

from __future__ import annotations

from datetime import timedelta

import streamlit as st

from etf_quant_lab.contracts.errors import DomainError
from etf_quant_lab.data.providers.akshare import AkshareProvider
from etf_quant_lab.services.daily_pipeline import run_daily_pipeline
from etf_quant_lab.services.data_sync import DataSyncService, active_adjustment
from etf_quant_lab.ui.presenters import build_data_center
from etf_quant_lab.ui.shared import (
    BATCH_STATUS_LABELS,
    GATE_LABELS,
    RULE_LABELS,
    clear_bars_cache,
    get_context,
    instrument_names,
    load_daily_bars,
    market_time_str,
    market_today,
    symbol_with_name,
)

st.title("数据中心")
st.caption("行情数据的同步、质量检查与批次历史。数据是一切研究的地基。")

context = get_context()
names = instrument_names(context)
today = market_today()

action_col, pipeline_col = st.columns(2)
with action_col:
    st.markdown("**增量同步**  \n只拉最近两周数据补齐缺口, 日常用这个, 快。")
    if st.button("同步最新数据", type="primary", icon=":material/sync:"):
        sync_service = DataSyncService(
            provider=AkshareProvider(id_generator=context.ids, publication_eligible=True),
            parquet_store=context.resolve("parquet_store"),  # type: ignore[arg-type]
            batch_repository=context.resolve("batches"),  # type: ignore[arg-type]
            quality_service=context.resolve("quality"),  # type: ignore[arg-type]
            id_generator=context.ids,
        )
        symbols = tuple(
            instrument.symbol
            for instrument in context.resolve("instruments").list_all()  # type: ignore[attr-defined]
            if instrument.enabled
        )
        adjustment = active_adjustment(context.resolve("batches"))  # type: ignore[arg-type]
        progress = st.progress(0.0, text="开始同步 ...")
        succeeded, failed = 0, 0
        for index, symbol in enumerate(symbols):
            progress.progress(
                (index + 1) / len(symbols),
                text=f"同步 {symbol_with_name(symbol, names)} ({index + 1}/{len(symbols)})",
            )
            try:
                batch, _ = sync_service.sync_daily_bars(
                    symbols=(symbol,),
                    start_date=today - timedelta(days=14),  # type: ignore[operator]
                    end_date=today,  # type: ignore[arg-type]
                    incremental=True,
                    adjustment=adjustment,
                )
                if batch.status.value == "ACTIVE":
                    succeeded += 1
                else:
                    failed += 1
            except DomainError:
                failed += 1
        progress.empty()
        clear_bars_cache()  # new batches activated: drop the bars cache
        if failed:
            st.warning(f"完成: {succeeded} 只成功, {failed} 只被质量检查拒绝或失败。")
        else:
            st.success(f"完成: 全部 {succeeded} 只同步成功。")
        st.rerun()

with pipeline_col:
    st.markdown("**每日流水线**  \n收盘后一键完成: 同步 → 质量检查 → 生成信号 → 记录净值。")
    if st.button("运行每日流水线", icon=":material/play_circle:"):
        with st.spinner("流水线运行中 ..."):
            result = run_daily_pipeline(context, trade_date=today)  # type: ignore[arg-type]
        clear_bars_cache()  # pipeline may activate new batches
        if result.skipped_reason:
            st.info(f"已跳过: {result.skipped_reason}")
        else:
            signal_text = (
                "无" if result.signal is None else f"{result.signal.status}"
            )
            st.success(
                f"完成: 同步 {result.synced_symbols} 只"
                + (f", 拒绝 {len(result.rejected_symbols)} 只" if result.rejected_symbols else "")
                + f"; 信号 {signal_text}"
                + (f"; 账户净值 {result.nav_equity} 元" if result.nav_equity else "")
            )
        st.rerun()

st.divider()

view = build_data_center(
    batch_repository=context.resolve("batches"),  # type: ignore[arg-type]
    quality_service=context.resolve("quality"),  # type: ignore[arg-type]
    limit=30,
    bars=load_daily_bars(),
)
st.metric("最新行情日", str(view.latest_bar_date or "无数据"))

st.subheader("最近同步批次")
if not view.batches:
    st.info("还没有同步过数据。点击上方「同步最新数据」开始。")
else:
    st.dataframe(
        [
            {
                "时间": market_time_str(batch.fetched_at),
                "标的": symbol_with_name(
                    str(batch.metadata.get("symbol_key", "")), names
                )
                if "," not in str(batch.metadata.get("symbol_key", ""))
                else "多标的",
                "状态": BATCH_STATUS_LABELS.get(batch.status.value, batch.status.value),
                "行数": batch.row_count,
                "质量检查": GATE_LABELS.get(view.gate_by_batch.get(batch.batch_id), "未运行")
                if batch.batch_id in view.gate_by_batch
                else "未运行",
                "同步方式": "增量"
                if batch.metadata.get("sync_mode") == "incremental"
                else "全量",
            }
            for batch in view.batches
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "「已被替代」是正常状态: 新批次激活后旧批次自动退役, 历史仍保留可审计。"
        "「已拒绝」表示质量检查发现问题, 该批数据不会被使用。"
    )

st.subheader("质量问题记录")
database = context.resolve("database")
with database.read_connection() as connection:  # type: ignore[attr-defined]
    issue_rows = connection.execute(
        """
        SELECT i.rule_code, i.severity, i.symbol, i.trade_date, i.message
        FROM quality_issues i
        JOIN quality_reports r ON r.report_id = i.report_id
        ORDER BY r.generated_at DESC
        LIMIT 20
        """
    ).fetchall()
if not issue_rows:
    st.info("暂无质量问题记录。")
else:
    severity_labels = {"BLOCKING": "阻断", "ERROR": "错误", "WARNING": "提示", "INFO": "信息"}
    st.dataframe(
        [
            {
                "问题类型": RULE_LABELS.get(str(row[0]), str(row[0])),
                "严重度": severity_labels.get(str(row[1]), str(row[1])),
                "标的": symbol_with_name(str(row[2] or ""), names),
                "日期": str(row[3] or ""),
                "说明": str(row[4]),
            }
            for row in issue_rows
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(
        "示例: 银行ETF(512800.SH) 因数据源未处理基金份额拆分, 出现单日近 50% 的价格跳变, "
        "被自动拦截以保护回测与信号的准确性。"
    )
