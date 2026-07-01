"""
缠论分析适配器

把 stock-chanlun 的缠论引擎封装成轻量接口，供日报流水线直接调用。

依赖:
    pip install pandas numpy pydantic

用法:
    from tradingagents.daily_report.chanlun_adapter import analyze_stock
    result = analyze_stock("000001", days=120)
    # result 为 dict，包含 trend / signals / support_resistance / summary
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import pandas as pd

logger = logging.getLogger(__name__)

# 让 stock-chanlun 后端模块可被导入
# 假设 stock-chanlun 与 TradingAgents-CN 处于同级目录
_CHANLUN_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "stock-chanlun" / "backend"
if not _CHANLUN_ROOT.exists():
    # 兜底：回退到旧绝对路径（沙箱环境）
    _CHANLUN_ROOT = Path("/workspace/stock-chanlun/backend").resolve()
CHANLUN_ROOT = _CHANLUN_ROOT
if str(CHANLUN_ROOT) not in sys.path:
    sys.path.insert(0, str(CHANLUN_ROOT))

# stock-chanlun 的数据服务（纯 httpx，不依赖 akshare）
from chanlun.engine import ChanlunEngine  # noqa: E402
from chanlun.elements import ChanlunAnalysis  # noqa: E402


def _to_plain_dict(analysis: ChanlunAnalysis) -> Dict[str, Any]:
    """把 Pydantic 模型转成纯 dict，方便 JSON 序列化和模板渲染。"""
    signals = []
    for s in analysis.signals:
        signals.append({
            "type": s.type,
            "level": s.level,
            "price": round(s.price, 2),
            "datetime": s.datetime.strftime("%Y-%m-%d") if s.datetime else "",
            "confidence": round(s.confidence, 2),
            "stop_loss": round(s.stop_loss, 2) if s.stop_loss else None,
            "take_profit": round(s.take_profit, 2) if s.take_profit else None,
            "description": s.description,
        })

    sr_levels = []
    for lvl in analysis.support_resistance:
        sr_levels.append({
            "type": lvl.type,
            "price": round(lvl.price, 2),
            "source": lvl.source,
            "strength": round(lvl.strength, 2),
        })

    # 只保留最近 3 个中枢和最近 5 笔，避免数据膨胀
    zhongshus = []
    for zs in analysis.zhongshus[-3:]:
        zhongshus.append({
            "range_low": round(zs.range_low, 2),
            "range_high": round(zs.range_high, 2),
            "start": zs.start.strftime("%Y-%m-%d") if zs.start else "",
            "end": zs.end.strftime("%Y-%m-%d") if zs.end else "",
        })

    bis = []
    for b in analysis.bis[-5:]:
        bis.append({
            "direction": b.direction,
            "high": round(b.high, 2),
            "low": round(b.low, 2),
            "start": b.start.strftime("%Y-%m-%d") if b.start else "",
            "end": b.end.strftime("%Y-%m-%d") if b.end else "",
        })

    return {
        "trend": analysis.trend,
        "signals": signals,
        "support_resistance": sr_levels,
        "zhongshus": zhongshus,
        "bis": bis,
        "summary": analysis.summary,
    }


def _get_kline_akshare(code: str, days: int = 120):
    """
    用 AKShare 腾讯数据源获取日线 K 线，返回 DataFrame [date, open, high, low, close, volume]。

    说明:
        - 优先使用 stock_zh_a_daily（腾讯接口），避开沙箱/代理对东财的封锁
        - symbol 需带交易所前缀：sh（上海）/ sz（深圳）
    """
    import akshare as ak  # noqa: E402

    sym = code.zfill(6)
    # 判断交易所前缀
    if sym.startswith(("6", "9", "688")):
        prefix = "sh"
    else:
        prefix = "sz"

    try:
        df = ak.stock_zh_a_daily(symbol=f"{prefix}{sym}", adjust="qfq")
    except Exception as e:
        logger.warning("腾讯 K 线接口失败 %s: %s", sym, e)
        return None

    if df is None or df.empty:
        return None

    df = df.tail(days).copy()
    # stock_zh_a_daily 返回列名已经是小写英文，无需重命名
    df["date"] = pd.to_datetime(df["date"])
    return df[["date", "open", "high", "low", "close", "volume"]]


def analyze_stock(code: str, days: int = 120) -> Optional[Dict[str, Any]]:
    """
    对单只股票执行缠论分析。

    参数:
        code: 6 位股票代码，如 "000001"
        days: 取多少天的日线 K 线，默认 120 天（约半年）

    返回:
        dict 或 None（数据不足 / 分析失败时返回 None，由上层决定如何处理）
    """
    try:
        df = _get_kline_akshare(code, days=days)
        if df is None or df.empty or len(df) < 30:
            logger.warning("缠论分析: %s K 线不足 (%d 条)，跳过", code, len(df) if df is not None else 0)
            return None

        engine = ChanlunEngine(df)
        analysis = engine.analyze(level="daily")
        analysis.stock_code = code
        return _to_plain_dict(analysis)
    except Exception as e:
        logger.warning("缠论分析失败 %s: %s", code, e)
        return None


def batch_analyze(codes: list[str], days: int = 120) -> Dict[str, Any]:
    """
    批量分析，返回 {code: result} 映射。失败项会被跳过。
    """
    results = {}
    for code in codes:
        r = analyze_stock(code, days=days)
        if r:
            results[code] = r
    logger.info("缠论批量分析完成: %d/%d 只成功", len(results), len(codes))
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    print(analyze_stock("000001"))
