<div align="center">

# ETF Quant Lab

**本地运行的 A 股 ETF 日频量化研究与模拟交易工作台**

[English](README.en.md) | 简体中文

数据同步 · 质量门禁 · 策略回测 · 每日信号 · 模拟账户 —— 全部在你自己的电脑上完成

![总览页](docs/screenshots/overview.png)

</div>

---

## 这是什么

ETF Quant Lab 是一个面向个人投资研究者的本地量化工具，围绕 A 股 ETF 的**日频**数据做完整闭环：

```
行情同步 → 质量门禁 → 策略回测 → 每日信号 → 模拟下单 → 净值跟踪
```

它刻意保持简单和诚实：

- **纯本地**：数据存在你电脑上（DuckDB + Parquet），界面只监听 127.0.0.1，无遥测。
- **不碰真钱**：不连接券商、不自动下单，模拟账户用虚拟资金跟踪信号效果。
- **不自欺**：严格防未来函数（T 日收盘决策、T+1 开盘成交）、强制交易成本假设、
  质量门禁不可绕过 —— 坏数据宁可拒绝也不混进回测。

> ⚠️ 本项目仅供学习与研究，输出不构成投资建议，历史回测不代表未来收益。

## 功能一览

| 页面 | 功能 |
|---|---|
| 总览 | 数据新鲜度、质量状态、最新信号，一眼看懂"今天该做什么" |
| 数据中心 | 一键增量同步 / 每日流水线，批次历史与质量问题全程可审计 |
| 今日信号 | 基于最新收盘生成目标持仓建议，每条附入选原因 |
| 模拟账户 | 虚拟资金按信号建仓，T+1 规则、账本可独立重算校验 |
| 回测实验室 | 真实历史 + 成本情景回测，收益/回撤/夏普等 8 项指标 |
| 运行日志 | 每次任务的执行记录与审计事件 |

### 界面截图

<details>
<summary><b>今日信号</b> —— 每条建议附目标权重、参考价与入选原因</summary>

![今日信号](docs/screenshots/signals.png)
</details>

<details>
<summary><b>回测实验室</b> —— 含成本的诚实回测，指标带悬停解释</summary>

![回测实验室](docs/screenshots/backtest.png)
</details>

<details>
<summary><b>数据中心</b> —— 批次生命周期与质量门禁结果全透明</summary>

![数据中心](docs/screenshots/data_center.png)
</details>

## 设计亮点

- **数据可信优先**：原始快照只追加、标准化层原子发布、SHA-256 校验和；
  质量规则（重复 / 未来日期 / 日历缺口 / 异常跳变 / 过期）不通过则整批拒绝。
- **前复权口径一致**：默认 QFQ 消除基金份额拆分造成的假跳变，
  增量同步自动跟随存量历史的复权口径，防止接缝混用。
- **防未来函数**：策略只能看到 as-of 日期之前的数据，越界直接抛错；
  同一输入重复运行结果比特级一致（黄金回归测试锁定）。
- **处处幂等**：信号四元组键、订单自然键、净值按日 upsert ——
  重复点击、重复调度都不会产生重复数据。
- **网络韧性**：浏览器 UA 补丁、代理/直连自动切换、数据源降级、指数退避重试。
- **工程质量**：290+ 测试（单元/集成/回归），mypy strict 全绿，分层架构
  （contracts / domain / services / storage / ui 单向依赖）。

## 快速开始

需要 Python 3.12+ 和 [uv](https://docs.astral.sh/uv/)。

```bash
# 1. 安装依赖
uv sync --all-groups

# 2. 初始化目录与数据库（幂等）
uv run python -m etf_quant_lab.cli init

# 3. 首次全量同步（默认标的池 5 年历史, 数据来自 AKShare, 无需任何 Token）
uv run python scripts/bootstrap_market_data.py

# 4. 启动界面（浏览器打开 http://127.0.0.1:8510）
uv run streamlit run app.py
```

第一次使用建议先读界面里的「帮助」页 —— 它假设你是量化新手，把每个概念都讲了一遍。

### 日常使用

每个交易日收盘后（约 16:30）任选其一：

```bash
# 方式 A: 界面操作 —— 数据中心页点「运行每日流水线」
# 方式 B: 命令行一次
uv run python scripts/run_scheduler.py --once
# 方式 C: 常驻调度 —— 每个交易日 16:30 自动运行
uv run python scripts/run_scheduler.py
```

### 可选配置

- **Tushare 数据源**：复制 `.env.example` 为 `.env` 并填入 `EQL_TUSHARE_TOKEN`
  即可启用 Tushare 通道（需要 fund_daily 权限）；不配置则使用 AKShare，功能完全一样。
- **标的池**：编辑 `config/universe.yaml` 增删 ETF（默认 26 只，覆盖宽基/半导体/
  机器人/新能源/医疗/军工等主题）。
- **策略参数**：编辑 `config/strategy_presets.yaml`。
- **成本情景**：编辑 `config/cost_scenarios.yaml`（理想 / 正常 / 悲观三档）。

## 内置策略

| 策略 | 思路 |
|---|---|
| ETF 轮动 | 按风险调整动量排名，持有最强的 N 只，动量走弱自动切换到防守资产 |
| 趋势基准 | 快慢均线过滤，均线上方等权持有，作为对照基准 |

策略接口是纯函数式的（输入 as-of 数据切片，输出目标组合），添加自己的策略
只需实现 `Strategy` 协议并注册，防未来函数与权重校验由框架强制执行。

## 开发

```bash
uv run pytest            # 290+ 测试, 覆盖率门槛 85%
uv run ruff check .      # 代码风格
uv run mypy src          # 严格类型检查
```

架构分层（依赖单向向下）：

```
app_pages/ (Streamlit 页面)
   └─ ui/ (presenters, 无框架依赖, 可独立测试)
       └─ services/ (数据同步 / 信号 / 回测 / 模拟账户 / 流水线)
           └─ domain/ (策略 / 风控 / 再平衡 / 执行, 纯函数)
               └─ contracts/ (数据契约与枚举)
           └─ storage/ (DuckDB + Parquet)
```

## 安全边界

- 首版**严禁**连接真实券商或自动下单，代码中不存在任何实盘接口。
- 所有行情数据与账户数据仅存本地，不上传任何服务器。
- Streamlit 遥测已关闭，服务只绑定 127.0.0.1。

## 许可证

[MIT](LICENSE)
