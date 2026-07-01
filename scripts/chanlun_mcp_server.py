#!/usr/bin/env python3
"""
缠论分析 MCP Server

为 Codex 提供缠论技术分析能力，包括：
- 单级别缠论分析（分型/笔/线段/中枢/买卖点）
- 多级别区间套策略（30min/5min/1min）
- 龙虎榜收盘日报生成

运行方式:
    python scripts/chanlun_mcp_server.py
    # 或使用 stdio 传输（Codex 默认）
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ── 路径设置 ─────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# stock-chanlun 路径
_STOCK_CHANLUN = ROOT.parent / "stock-chanlun" / "backend"
if not _STOCK_CHANLUN.exists():
    _STOCK_CHANLUN = Path("/workspace/stock-chanlun/backend").resolve()
if str(_STOCK_CHANLUN) not in sys.path:
    sys.path.insert(0, str(_STOCK_CHANLUN))

# ── MCP 初始化 ───────────────────────────────────────────────
try:
    from mcp.server.fastmcp import FastMCP
except ImportError as e:
    print(f"错误: 未安装 mcp 库。请运行: pip install mcp")
    raise SystemExit(1)

mcp = FastMCP("chanlun_mcp")

# ── 日志 ─────────────────────────────────────────────────────
logger = logging.getLogger("chanlun_mcp")

# ── 枚举 ─────────────────────────────────────────────────────
class TimeLevel(str, Enum):
    """缠论分析级别"""
    DAILY = "daily"
    THIRTY_MIN = "30min"
    FIVE_MIN = "5min"
    ONE_MIN = "1min"

class ResponseFormat(str, Enum):
    """输出格式"""
    MARKDOWN = "markdown"
    JSON = "json"

# ── Pydantic 输入模型 ────────────────────────────────────────
class ChanlunAnalyzeInput(BaseModel):
    """单级别缠论分析输入"""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    stock_code: str = Field(
        ...,
        description="股票代码，6位数字，如 '002463' 或 '600584'",
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$"
    )
    level: TimeLevel = Field(
        default=TimeLevel.DAILY,
        description="分析级别: daily(日线) / 30min / 5min / 1min"
    )
    days: int = Field(
        default=120,
        description="日线分析时取多少天K线（仅level=daily时生效）",
        ge=30,
        le=500
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="输出格式: markdown(人类可读) / json(结构化数据)"
    )

class IntervalStrategyInput(BaseModel):
    """区间套策略分析输入"""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    stock_code: str = Field(
        ...,
        description="股票代码，6位数字，如 '002463'",
        min_length=6,
        max_length=6,
        pattern=r"^\d{6}$"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="输出格式"
    )

class DailyReportInput(BaseModel):
    """龙虎榜收盘日报输入"""
    model_config = ConfigDict(str_strip_whitespace=True, validate_assignment=True)

    date: Optional[str] = Field(
        default=None,
        description="日期 YYYY-MM-DD，默认今天",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    top_n: int = Field(
        default=10,
        description="取前N只龙虎榜股票分析",
        ge=1,
        le=50
    )
    skip_llm: bool = Field(
        default=True,
        description="是否跳过DeepSeek AI点评（节省token）"
    )
    skip_strategy: bool = Field(
        default=False,
        description="是否跳过区间套策略分析（节省分钟级数据拉取时间）"
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="输出格式"
    )

# ── 共享工具函数 ─────────────────────────────────────────────
def _fmt_price(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "—"


def _load_env() -> None:
    """加载 .env.daily_report 配置"""
    env_path = ROOT / ".env.daily_report"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v


# ── 工具 1: 单级别缠论分析 ──────────────────────────────────
@mcp.tool(
    name="chanlun_analyze_stock",
    annotations={
        "title": "缠论单级别技术分析",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def chanlun_analyze_stock(params: ChanlunAnalyzeInput) -> str:
    """
    对单只股票进行缠论技术分析，包括分型、笔、线段、中枢、买卖点识别。

    支持日线(daily)、30分钟、5分钟、1分钟四个级别。
    数据源为AKShare腾讯接口，无需额外配置。

    Args:
        params: ChanlunAnalyzeInput 包含:
            - stock_code (str): 6位股票代码，如 "002463"
            - level (TimeLevel): 分析级别，默认 daily
            - days (int): 日线分析时K线天数，默认120
            - response_format (ResponseFormat): 输出格式

    Returns:
        str: Markdown 或 JSON 格式的分析结果，包含趋势、买卖点、支撑阻力、中枢区间

    Examples:
        - "分析002463日线缠论结构" -> stock_code="002463", level="daily"
        - "看看30分钟级别的一卖确认了没" -> stock_code="002463", level="30min"
    """
    try:
        from tradingagents.daily_report.chanlun_adapter import analyze_stock

        if params.level == TimeLevel.DAILY:
            result = analyze_stock(params.stock_code, days=params.days)
        else:
            # 分钟级分析直接用策略引擎中的_fetch_and_analyze逻辑
            from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy
            stg = ChanlunStrategy(params.stock_code)
            period_map = {
                TimeLevel.THIRTY_MIN: ("30min", "30", 120),
                TimeLevel.FIVE_MIN: ("5min", "5", 1000),
                TimeLevel.ONE_MIN: ("1min", "1", 2000),
            }
            lv, period, klines = period_map[params.level]
            raw = stg._fetch_and_analyze(lv, period, klines)
            if raw is None:
                return "错误: 数据不足或拉取失败，无法进行分析"

            # 构建类似analyze_stock返回的dict
            sig = raw["latest_signal"]
            result = {
                "trend": raw["trend"],
                "signals": [{
                    "type": sig.type,
                    "level": sig.level,
                    "price": round(sig.price, 2),
                    "datetime": sig.datetime.strftime("%Y-%m-%d %H:%M") if sig.datetime else "",
                    "confidence": round(sig.confidence, 2),
                    "stop_loss": round(sig.stop_loss, 2) if sig.stop_loss else None,
                    "description": sig.description,
                }] if sig else [],
                "summary": raw["summary"],
                "current_price": raw["current_price"],
                "zhongshu_low": raw["zhongshu_low"],
                "zhongshu_high": raw["zhongshu_high"],
            }

        if result is None:
            return f"错误: {params.stock_code} {params.level.value} 级别分析失败（数据不足）"

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(result, ensure_ascii=False, indent=2)

        # Markdown 格式
        lines = [
            f"# {params.stock_code} 缠论分析 ({params.level.value})",
            "",
            f"**当前价**: {_fmt_price(result.get('current_price'))}",
            f"**趋势**: {result.get('trend', '未知')}",
            f"**总结**: {result.get('summary', '—')}",
            "",
        ]

        if result.get("zhongshu_low"):
            lines.append(
                f"**最新中枢**: [{_fmt_price(result['zhongshu_low'])}, {_fmt_price(result['zhongshu_high'])}]"
            )

        signals = result.get("signals", [])
        if signals:
            lines.append("## 买卖点信号")
            for s in signals[-5:]:
                lines.append(
                    f"- **{s['type']}** @ {s['price']:.2f} "
                    f"(置信度 {s.get('confidence', 0):.0%})"
                )
                if s.get("description"):
                    lines.append(f"  - {s['description']}")

        sr = result.get("support_resistance", [])
        if sr:
            supports = [f"{x['price']:.2f}" for x in sr if x.get("type") == "support"][:3]
            resistances = [f"{x['price']:.2f}" for x in sr if x.get("type") == "resistance"][:3]
            lines.append("")
            lines.append("## 支撑阻力")
            if supports:
                lines.append(f"- 支撑: {' / '.join(supports)}")
            if resistances:
                lines.append(f"- 阻力: {' / '.join(resistances)}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("缠论分析失败: %s", e)
        return f"错误: 分析失败 - {type(e).__name__}: {e}"


# ── 工具 2: 区间套策略分析 ──────────────────────────────────
@mcp.tool(
    name="chanlun_interval_strategy",
    annotations={
        "title": "缠论区间套交易策略",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def chanlun_interval_strategy(params: IntervalStrategyInput) -> str:
    """
    区间套交易策略分析：同时监控30min/5min/1min三个级别，输出操作建议。

    核心逻辑："大级别定方向，小级别找买卖点"
    - 30分钟级别定大方向
    - 5分钟级别找中级别买卖点
    - 1分钟级别找精确入场点

    Args:
        params: IntervalStrategyInput 包含:
            - stock_code (str): 6位股票代码，如 "002463"
            - response_format (ResponseFormat): 输出格式

    Returns:
        str: 包含各级别状态、交易建议、逻辑推理的分析报告

    Examples:
        - "002463区间套策略分析" -> stock_code="002463"
        - "看看30分钟一卖后该怎么操作" -> stock_code="002463"
    """
    try:
        from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy

        stg = ChanlunStrategy(params.stock_code)
        result = stg.run()

        if params.response_format == ResponseFormat.JSON:
            return json.dumps(result, ensure_ascii=False, indent=2)

        return result["text"]

    except Exception as e:
        logger.exception("区间套策略分析失败: %s", e)
        return f"错误: 策略分析失败 - {type(e).__name__}: {e}"


# ── 工具 3: 龙虎榜收盘日报 ──────────────────────────────────
@mcp.tool(
    name="chanlun_daily_report",
    annotations={
        "title": "A股龙虎榜收盘日报",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
async def chanlun_daily_report(params: DailyReportInput) -> str:
    """
    生成A股龙虎榜收盘日报，包含缠论分析和区间套策略监控。

    工作流：
    1. 抓取当日龙虎榜数据
    2. 对上榜个股进行缠论日线分析
    3. 进行30min/5min/1min区间套策略分析（可跳过）
    4. 渲染HTML报告

    Args:
        params: DailyReportInput 包含:
            - date (Optional[str]): 日期 YYYY-MM-DD，默认今天
            - top_n (int): 分析前N只股票，默认10
            - skip_llm (bool): 是否跳过AI点评，默认True
            - skip_strategy (bool): 是否跳过策略分析，默认False
            - response_format (ResponseFormat): 输出格式

    Returns:
        str: 日报HTML文件路径或错误信息

    Examples:
        - "生成今天龙虎榜日报" -> date=None, top_n=10
        - "看看2026-06-25的龙虎榜" -> date="2026-06-25"
    """
    try:
        _load_env()
        os.environ["REPORT_TOP_N"] = str(params.top_n)
        if params.skip_llm:
            os.environ.pop("DEEPSEEK_API_KEY", None)

        from scripts.daily_close_report import run

        file_path = run(
            date=params.date,
            skip_llm=params.skip_llm,
            skip_strategy=params.skip_strategy
        )

        if params.response_format == ResponseFormat.JSON:
            return json.dumps({
                "file_path": file_path,
                "date": params.date or datetime.now().strftime("%Y-%m-%d"),
                "top_n": params.top_n,
            }, ensure_ascii=False, indent=2)

        return (
            f"✅ 龙虎榜收盘日报已生成\n"
            f"📁 文件路径: {file_path}\n"
            f"📅 日期: {params.date or datetime.now().strftime('%Y-%m-%d')}\n"
            f"📊 分析股票数: 前 {params.top_n} 只"
        )

    except Exception as e:
        logger.exception("日报生成失败: %s", e)
        return f"错误: 日报生成失败 - {type(e).__name__}: {e}"


# ── 主入口 ──────────────────────────────────────────────────
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    # stdio 传输（Codex 默认调用方式）
    mcp.run()
