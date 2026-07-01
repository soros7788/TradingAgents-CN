# 缠论分析 MCP Server — Codex 配置指南

## 概述

为 Codex 提供缠论技术分析能力，封装了三个核心工具：

| 工具名 | 功能 |
|--------|------|
| `chanlun_analyze_stock` | 单级别缠论分析（日线/30min/5min/1min） |
| `chanlun_interval_strategy` | 区间套策略分析（30min/5min/1min三级别联动） |
| `chanlun_daily_report` | A股龙虎榜收盘日报生成 |

## 依赖安装

```bash
cd TradingAgents-CN
pip install mcp akshare pandas pydantic numpy httpx
```

## Codex 配置

在 Codex 的 MCP 配置文件中添加以下内容（通常位于 `~/.codex/config.json` 或 Codex 设置中的 MCP Servers 部分）：

```json
{
  "mcpServers": {
    "chanlun": {
      "command": "python3",
      "args": [
        "/path/to/TradingAgents-CN/scripts/chanlun_mcp_server.py"
      ],
      "env": {
        "PYTHONPATH": "/path/to/TradingAgents-CN:/path/to/stock-chanlun/backend"
      }
    }
  }
}
```

**注意**：
- 将 `/path/to/TradingAgents-CN` 替换为实际路径
- `stock-chanlun` 需与 `TradingAgents-CN` 处于同级目录
- 确保 Python 环境中已安装所有依赖

## 工具调用示例

### 1. 单级别缠论分析

```
分析002463的30分钟级别缠论结构
```

Codex 会自动调用：
```json
{
  "stock_code": "002463",
  "level": "30min",
  "response_format": "markdown"
}
```

### 2. 区间套策略分析

```
002463区间套策略，大级别定方向小级别找买卖点
```

Codex 会自动调用：
```json
{
  "stock_code": "002463",
  "response_format": "markdown"
}
```

### 3. 生成龙虎榜日报

```
生成今天的龙虎榜收盘日报，分析前10只股票
```

Codex 会自动调用：
```json
{
  "date": null,
  "top_n": 10,
  "skip_llm": true,
  "skip_strategy": false,
  "response_format": "markdown"
}
```

## 输入参数说明

### ChanlunAnalyzeInput

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `stock_code` | str | 必填 | 6位股票代码，如 "002463" |
| `level` | enum | "daily" | daily / 30min / 5min / 1min |
| `days` | int | 120 | 日线分析K线天数（30-500） |
| `response_format` | enum | "markdown" | markdown / json |

### IntervalStrategyInput

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `stock_code` | str | 必填 | 6位股票代码 |
| `response_format` | enum | "markdown" | markdown / json |

### DailyReportInput

| 字段 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `date` | str | null | YYYY-MM-DD，默认今天 |
| `top_n` | int | 10 | 分析前N只股票（1-50） |
| `skip_llm` | bool | true | 跳过AI点评 |
| `skip_strategy` | bool | false | 跳过区间套策略 |
| `response_format` | enum | "markdown" | markdown / json |

## 文件位置

- MCP Server: `scripts/chanlun_mcp_server.py`
- 策略引擎: `tradingagents/daily_report/chanlun_strategy.py`
- 缠论适配: `tradingagents/daily_report/chanlun_adapter.py`
- 缠论引擎: `../stock-chanlun/backend/chanlun/`
