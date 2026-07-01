#!/usr/bin/env python3
"""
Google Gemini 缠论分析工具封装

直接给 Gemini 使用的 Function Calling 代码，包含：
- 3个Function Declaration（工具声明）
- 3个实际执行函数
- 完整的Gemini调用示例

依赖安装：
    pip install google-genai akshare pandas pydantic numpy

用法：
    1. 设置 GOOGLE_API_KEY 环境变量
    2. 运行 python gemini_chanlun_tools.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

# ── 路径设置 ─────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# stock-chanlun 路径
_STOCK_CHANLUN = ROOT.parent / "stock-chanlun" / "backend"
if not _STOCK_CHANLUN.exists():
    _STOCK_CHANLUN = Path("/workspace/stock-chanlun/backend").resolve()
if str(_STOCK_CHANLUN) not in sys.path:
    sys.path.insert(0, str(_STOCK_CHANLUN))


# ══════════════════════════════════════════════════════════════
#  Part 1: Function Declaration（Gemini 工具声明）
# ══════════════════════════════════════════════════════════════

FUNCTION_DECLARATIONS = [
    {
        "name": "chanlun_analyze_stock",
        "description": "对单只股票进行缠论技术分析，识别分型、笔、线段、中枢、买卖点。支持日线/30min/5min/1min四个级别。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码，6位数字，如 '002463' 或 '600584'"
                },
                "level": {
                    "type": "string",
                    "enum": ["daily", "30min", "5min", "1min"],
                    "description": "分析级别，默认 daily（日线）"
                },
                "days": {
                    "type": "integer",
                    "description": "日线分析时取多少天K线，仅level=daily时生效，默认120"
                },
                "response_format": {
                    "type": "string",
                    "enum": ["markdown", "json"],
                    "description": "输出格式，默认markdown（人类可读）"
                }
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "chanlun_interval_strategy",
        "description": "区间套交易策略分析：同时监控30min/5min/1min三个级别，根据'大级别定方向、小级别找买卖点'原则输出操作建议。",
        "parameters": {
            "type": "object",
            "properties": {
                "stock_code": {
                    "type": "string",
                    "description": "股票代码，6位数字，如 '002463'"
                },
                "response_format": {
                    "type": "string",
                    "enum": ["markdown", "json"],
                    "description": "输出格式，默认markdown"
                }
            },
            "required": ["stock_code"],
        },
    },
    {
        "name": "chanlun_daily_report",
        "description": "生成A股龙虎榜收盘日报，包含缠论分析和区间套策略监控。抓取当日龙虎榜数据，对上榜个股进行多维度分析。",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "日期 YYYY-MM-DD，默认今天"
                },
                "top_n": {
                    "type": "integer",
                    "description": "取前N只龙虎榜股票分析，默认10"
                },
                "skip_llm": {
                    "type": "boolean",
                    "description": "是否跳过DeepSeek AI点评（节省token），默认true"
                },
                "skip_strategy": {
                    "type": "boolean",
                    "description": "是否跳过区间套策略分析（节省分钟级数据拉取时间），默认false"
                }
            },
            "required": [],
        },
    },
]


# ══════════════════════════════════════════════════════════════
#  Part 2: 实际执行函数
# ══════════════════════════════════════════════════════════════

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


def chanlun_analyze_stock(
    stock_code: str,
    level: str = "daily",
    days: int = 120,
    response_format: str = "markdown",
) -> str:
    """
    对单只股票进行缠论技术分析。
    """
    try:
        from tradingagents.daily_report.chanlun_adapter import analyze_stock

        if level == "daily":
            result = analyze_stock(stock_code, days=days)
        else:
            from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy
            stg = ChanlunStrategy(stock_code)
            period_map = {
                "30min": ("30min", "30", 120),
                "5min": ("5min", "5", 1000),
                "1min": ("1min", "1", 2000),
            }
            lv, period, klines = period_map[level]
            raw = stg._fetch_and_analyze(lv, period, klines)
            if raw is None:
                return "错误: 数据不足或拉取失败，无法进行分析"

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
            return f"错误: {stock_code} {level} 级别分析失败（数据不足）"

        if response_format == "json":
            return json.dumps(result, ensure_ascii=False, indent=2)

        lines = [
            f"# {stock_code} 缠论分析 ({level})",
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
        return f"错误: 分析失败 - {type(e).__name__}: {e}"


def chanlun_interval_strategy(
    stock_code: str,
    response_format: str = "markdown",
) -> str:
    """
    区间套交易策略分析：同时监控30min/5min/1min三个级别。
    """
    try:
        from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy
        stg = ChanlunStrategy(stock_code)
        result = stg.run()

        if response_format == "json":
            return json.dumps(result, ensure_ascii=False, indent=2)

        return result["text"]

    except Exception as e:
        return f"错误: 策略分析失败 - {type(e).__name__}: {e}"


def chanlun_daily_report(
    date: Optional[str] = None,
    top_n: int = 10,
    skip_llm: bool = True,
    skip_strategy: bool = False,
) -> str:
    """
    生成A股龙虎榜收盘日报。
    """
    try:
        _load_env()
        os.environ["REPORT_TOP_N"] = str(top_n)
        if skip_llm:
            os.environ.pop("DEEPSEEK_API_KEY", None)

        from scripts.daily_close_report import run

        file_path = run(
            date=date,
            skip_llm=skip_llm,
            skip_strategy=skip_strategy
        )

        return (
            f"✅ 龙虎榜收盘日报已生成\n"
            f"📁 文件路径: {file_path}\n"
            f"📅 日期: {date or datetime.now().strftime('%Y-%m-%d')}\n"
            f"📊 分析股票数: 前 {top_n} 只"
        )

    except Exception as e:
        return f"错误: 日报生成失败 - {type(e).__name__}: {e}"


# ══════════════════════════════════════════════════════════════
#  Part 3: Gemini 调用封装
# ══════════════════════════════════════════════════════════════

class GeminiChanlunAgent:
    """
    Google Gemini + 缠论分析工具 封装类

    用法：
        agent = GeminiChanlunAgent(api_key="你的Gemini API Key")
        response = agent.chat("分析002463的30分钟缠论结构")
        print(response)
    """

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash"):
        try:
            from google import genai
            from google.genai import types
        except ImportError:
            raise ImportError("请先安装 google-genai: pip install google-genai")

        self.api_key = api_key or os.environ.get("GOOGLE_API_KEY")
        if not self.api_key:
            raise ValueError("需要提供 api_key 或设置 GOOGLE_API_KEY 环境变量")

        self.client = genai.Client(api_key=self.api_key)
        self.model = model
        self.types = types

        # 注册工具
        self.tools = [
            types.Tool(function_declarations=FUNCTION_DECLARATIONS)
        ]

        # 工具映射表
        self.function_map = {
            "chanlun_analyze_stock": chanlun_analyze_stock,
            "chanlun_interval_strategy": chanlun_interval_strategy,
            "chanlun_daily_report": chanlun_daily_report,
        }

        # 对话历史
        self.history = []

    def chat(self, message: str) -> str:
        """
        与 Gemini 对话，自动调用缠论分析工具。

        Args:
            message: 用户输入，如 "分析002463的30分钟缠论结构"

        Returns:
            Gemini 的回复文本
        """
        # 添加用户消息到历史
        self.history.append(
            self.types.Content(role="user", parts=[self.types.Part(text=message)])
        )

        # 第一次调用：让 Gemini 决定是否使用工具
        response = self.client.models.generate_content(
            model=self.model,
            contents=self.history,
            config=self.types.GenerateContentConfig(
                tools=self.tools,
                temperature=0.2,
            ),
        )

        # 检查是否有函数调用
        if response.candidates and response.candidates[0].content.parts:
            part = response.candidates[0].content.parts[0]

            if part.function_call:
                func_call = part.function_call
                func_name = func_call.name
                func_args = dict(func_call.args) if func_call.args else {}

                print(f"[工具调用] {func_name}({json.dumps(func_args, ensure_ascii=False)})")

                # 执行函数
                if func_name in self.function_map:
                    result = self.function_map[func_name](**func_args)
                else:
                    result = f"错误: 未知工具 {func_name}"

                # 将工具结果返回给 Gemini
                self.history.append(
                    self.types.Content(
                        role="model",
                        parts=[self.types.Part(function_call=func_call)]
                    )
                )
                self.history.append(
                    self.types.Content(
                        role="user",
                        parts=[
                            self.types.Part(
                                function_response=self.types.FunctionResponse(
                                    name=func_name,
                                    response={"result": result},
                                )
                            )
                        ]
                    )
                )

                # 第二次调用：让 Gemini 总结工具结果
                final_response = self.client.models.generate_content(
                    model=self.model,
                    contents=self.history,
                    config=self.types.GenerateContentConfig(temperature=0.2),
                )

                reply_text = final_response.text or ""
                self.history.append(
                    self.types.Content(
                        role="model",
                        parts=[self.types.Part(text=reply_text)]
                    )
                )
                return reply_text

        # 没有工具调用，直接返回回复
        reply_text = response.text or ""
        self.history.append(
            self.types.Content(
                role="model",
                parts=[self.types.Part(text=reply_text)]
            )
        )
        return reply_text

    def reset(self):
        """清空对话历史"""
        self.history = []


# ══════════════════════════════════════════════════════════════
#  Part 4: 测试入口
# ══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Gemini 缠论分析工具")
    parser.add_argument("--api-key", help="Gemini API Key，也可通过 GOOGLE_API_KEY 环境变量设置")
    parser.add_argument("--test", action="store_true", help="运行测试")
    parser.add_argument("--interactive", action="store_true", help="交互式对话模式")
    args = parser.parse_args()

    if args.test:
        print("=== 测试模式 ===")
        print("测试单级别分析...")
        result = chanlun_analyze_stock("002463", level="30min")
        print(result[:800])
        print("\n---")
        print("测试区间套策略...")
        result = chanlun_interval_strategy("002463")
        print(result[:800])
        print("\n测试完成")

    elif args.interactive:
        api_key = args.api_key or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            print("错误: 需要提供 --api-key 或设置 GOOGLE_API_KEY 环境变量")
            sys.exit(1)

        agent = GeminiChanlunAgent(api_key=api_key)
        print("=== Gemini 缠论分析助手 ===")
        print("输入问题，如：分析002463的30分钟缠论结构")
        print("输入 'quit' 或 'exit' 退出")
        print()

        while True:
            user_input = input("你: ").strip()
            if user_input.lower() in ("quit", "exit", "q"):
                break
            if not user_input:
                continue

            try:
                reply = agent.chat(user_input)
                print(f"\nGemini: {reply}\n")
            except Exception as e:
                print(f"错误: {e}")

    else:
        print("Google Gemini 缠论分析工具")
        print()
        print("用法:")
        print("  python gemini_chanlun_tools.py --test              # 测试工具函数")
        print("  python gemini_chanlun_tools.py --interactive       # 交互式对话")
        print()
        print("代码中调用:")
        print("  from gemini_chanlun_tools import GeminiChanlunAgent")
        print("  agent = GeminiChanlunAgent(api_key='你的API Key')")
        print("  reply = agent.chat('分析002463的30分钟缠论结构')")
