# A 股收盘日报自动化 — 本地部署指南

基于 [TradingAgents-CN](https://github.com/soros7788/TradingAgents-CN) + [stock-chanlun](https://github.com/soros7788/stock-chanlun) 的 A 股龙虎榜收盘日报系统。

## 功能

- 每日自动抓取 A 股龙虎榜数据（AKShare）
- 对龙虎榜个股进行缠论结构分析（分型 / 笔 / 线段 / 中枢 / 买卖点）
- **区间套策略监控**：同时分析 30min/5min/1min 三个级别，根据"大级别定方向、小级别找买卖点"原则输出操作建议
- DeepSeek AI 对每只龙虎榜个股生成短点评
- 渲染排版精美的 HTML 日报（自适应移动端）
- 支持 TRAE Work 定时任务调度

## 目录结构（新增部分）

```
TradingAgents-CN/
├── tradingagents/daily_report/          # 新增：日报核心模块
│   ├── __init__.py
│   ├── lhb_provider.py                  # 龙虎榜数据抓取
│   ├── chanlun_adapter.py               # 缠论分析封装（日线级别）
│   ├── chanlun_strategy.py              # 区间套策略引擎（30min/5min/1min）
│   ├── deepseek_summary.py              # DeepSeek AI 点评
│   └── html_report.py                   # HTML 日报渲染
├── scripts/
│   └── daily_close_report.py            # 主编排脚本
├── .env.daily_report                    # 配置文件模板
└── DAILY_REPORT_README.md               # 本文件
```

## 环境要求

- Python 3.10+
- 能访问外网（AKShare / DeepSeek API 需要）

## 安装依赖

```bash
cd TradingAgents-CN
pip install akshare httpx pandas pydantic ta numpy
```

## 配置

复制配置模板并填入你的 DeepSeek API Key：

```bash
cp .env.daily_report .env.daily_report.local
```

编辑 `.env.daily_report.local`：

```
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxx
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-chat

REPORT_OUTPUT_DIR=/workspace/reports
REPORT_TOP_N=20
REPORT_RETRY_MINUTES=30
```

## 运行方式

### 1. 手动运行（完整功能）

```bash
python scripts/daily_close_report.py --env-file .env.daily_report.local
```

### 2. 指定日期

```bash
python scripts/daily_close_report.py --date 2026-06-18 --env-file .env.daily_report.local
```

### 3. 跳过 AI 点评（省 token）

```bash
python scripts/daily_close_report.py --skip-llm --env-file .env.daily_report.local
```

### 4. 跳过缠论分析（网络受限环境）

```bash
python scripts/daily_close_report.py --skip-chanlun --env-file .env.daily_report.local
```

### 5. 跳过区间套策略（分钟级数据较慢）

```bash
python scripts/daily_close_report.py --skip-strategy --env-file .env.daily_report.local
```

### 6. TRAE Work 定时任务

### 6. TRAE Work 定时任务

在 TRAE Work 中创建自动化任务：

- **命令**：`cd /path/to/TradingAgents-CN && python3 scripts/daily_close_report.py --env-file .env.daily_report.local`
- **Cron**：`30 16 * * 1-5`（北京时间周一至周五 16:30）
- **时区**：`Asia/Shanghai`

## 输出

HTML 报告生成在 `REPORT_OUTPUT_DIR` 目录下，文件名格式：`YYYY-MM-DD.html`

用浏览器打开即可查看，支持手机端。

## 日报内容

- **概览区**：上榜股票数、净买入合计、最高涨幅、数据状态
- **市场总览**：DeepSeek AI 全局点评
- **区间套策略监控**：每只股票的 30min/5min/1min 级别状态汇总 + 交易建议（等待 / 轻仓试多 / 重仓做多 / 减仓观望）
- **龙虎榜排行**：净买入前 N 只表格
- **个股详情**：每只包含
  - 基本信息（涨跌幅、成交额、净买入、上榜原因）
  - 龙虎榜席位（买方 / 卖方）
  - 缠论日线分析（趋势标签、买卖点标签、支撑/阻力位、中枢区间）
  - 区间套策略建议标签
  - AI 短点评

## 常见问题

**Q: 缠论分析失败 / K 线不足**
A: 系统已自动切换到腾讯数据源（`ak.stock_zh_a_daily`）。若仍失败，可能是网络受限，请用 `--skip-chanlun` 跳过。

**Q: DeepSeek 调用失败**
A: 检查 `DEEPSEEK_API_KEY` 是否有效、余额是否充足。失败时报告会标注 "AI 点评不可用"，不影响其他内容。

**Q: 龙虎榜未发布**
A: 收盘后 30 分钟内数据可能尚未更新，脚本会自动等待 30 分钟后重试一次。

## 依赖仓库

- [TradingAgents-CN](https://github.com/soros7788/TradingAgents-CN) — 多智能体股票分析平台
- [stock-chanlun](https://github.com/soros7788/stock-chanlun) — 缠论智能分析系统
