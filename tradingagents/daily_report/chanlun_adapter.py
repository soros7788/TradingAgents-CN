"""
缠论分析适配器

使用自包含的缠论引擎 (scripts/chanlun_engine.py)，不再依赖 stock-chanlun 外部包。

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

import pandas as pd

logger = logging.getLogger(__name__)

# 将 scripts 目录加入 path 以导入自包含引擎
_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from chanlun_engine import analyze as _chanlun_analyze  # noqa: E402
from chanlun_engine import result_to_dict as _result_to_dict  # noqa: E402
from chanlun_engine import fetch_kline_akshare  # noqa: E402


def _to_compat_dict(result_dict: Dict[str, Any], code: str) -> Dict[str, Any]:
    """把新引擎的结果转成旧接口兼容的 dict 格式。"""
    # 添加 support_resistance 字段别名
    sr = result_dict.get("support_resistance", [])
    # 转换 datetime 格式 (只保留日期)
    signals = []
    for s in result_dict.get("signals", []):
        signals.append({
            "type": s["type"],
            "level": s["level"],
            "price": s["price"],
            "datetime": s.get("datetime", "")[:10],
            "confidence": s["confidence"],
            "stop_loss": s.get("stop_loss"),
            "take_profit": s.get("take_profit"),
            "description": s.get("description", ""),
        })

    zhongshus = []
    for zs in result_dict.get("zhongshus", []):
        zhongshus.append({
            "range_low": zs["range_low"],
            "range_high": zs["range_high"],
            "start": zs.get("start", ""),
            "end": zs.get("end", ""),
        })

    bis = []
    for b in result_dict.get("bis", [])[-5:]:
        bis.append({
            "direction": b["direction"],
            "high": b["high"],
            "low": b["low"],
            "start": b.get("start", "")[:10],
            "end": b.get("end", "")[:10],
        })

    return {
        "trend": result_dict.get("trend", "未知"),
        "signals": signals,
        "support_resistance": sr,
        "zhongshus": zhongshus,
        "bis": bis,
        "summary": result_dict.get("summary", ""),
        "current_price": result_dict.get("current_price"),
        "stock_code": code,
    }


def analyze_stock(code: str, days: int = 120) -> Optional[Dict[str, Any]]:
    """
    对单只股票执行缠论分析。

    参数:
        code: 6 位股票代码，如 "000001"
        days: 取多少天的日线 K 线，默认 120 天（约半年）

    返回:
        dict 或 None（数据不足 / 分析失败时返回 None）
    """
    try:
        result = _chanlun_analyze(code, level="daily", days=days)
        if result is None:
            logger.warning("缠论分析: %s 分析失败", code)
            return None
        raw = _result_to_dict(result)
        return _to_compat_dict(raw, code)
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
    import json
    r = analyze_stock("000001")
    if r:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("分析失败")