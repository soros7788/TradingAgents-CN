#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v5 完整版研究模块 (2026-10-04 验收版)

架构（用户拍板）：
- R0：生产 pipeline（canonical_bars_to_recursive_r0 → analyze_same_rules，5min）
- R1 段 = R0 movements 1:1 直转，无分型笔
- 中枢：生产 _build_zhongshus + 2% 最小尺寸过滤（去噪音）
    # ≥2 个中枢 → 必须有不重叠的一对才算趋势（20261011 fix: 趋势=双中枢不重叠）
    def _has_non_overlapping(zs):
        for _i in range(len(zs)):
            for _j in range(_i + 1, len(zs)):
                _a, _b = zs[_i], zs[_j]
                if _a.range_high < _b.range_low or _b.range_high < _a.range_low:
                    return True
        return False
    if not _has_non_overlapping(zs):
        return "盘整", {**detail_base, "direction": "neutral", "reason": "中枢重叠"}
    # 有不重叠 → 趋势，方向由首末中枢中心决定

注意：研究代码，不碰生产。生产 v5-lite 继续运行。
"""
import os, sys
sys.path.insert(0, os.path.expanduser('~/TradingAgents-CN'))

import csv
from datetime import datetime, timedelta, timezone
from typing import List, Tuple, Dict

from tradingagents.chan.canonical_market_snapshot import CanonicalBar
from tradingagents.chan.recursive_input_adapter import canonical_bars_to_recursive_r0
from tradingagents.chan.recursive_core.structure_rules import (
    analyze_same_rules, _build_zhongshus,
)
from tradingagents.chan.recursive_core.models import RecursiveSegment

# 生产 R0 时间框架
R0_TIMEFRAME = "5m"
# 中枢最小尺寸过滤（百分比）
MIN_ZHONGSHU_SIZE_PCT = 2.0

TZ_SH = timezone(timedelta(hours=8))


def csv_to_canonical_bars(
    csv_path: str, symbol: str,
    max_bars: int = 2500, end_date: str = None,
) -> List[CanonicalBar]:
    """CSV K线 → List[CanonicalBar]（生产格式，Asia/Shanghai 时区）."""
    bars = []
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            dt_str = r.get('day', r.get('datetime', r.get('date', '')))
            if end_date and dt_str[:10] > end_date:
                continue
            try:
                dt = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')
            except ValueError:
                try:
                    dt = datetime.strptime(dt_str[:10], '%Y-%m-%d')
                except ValueError:
                    continue
            dt = dt.replace(tzinfo=TZ_SH)
            bars.append(CanonicalBar(
                symbol=symbol,
                timeframe=R0_TIMEFRAME,
                start_time=dt - timedelta(minutes=5),
                end_time=dt,
                open=float(r.get('open', r['close'])),
                high=float(r['high']),
                low=float(r['low']),
                close=float(r['close']),
                volume=float(r.get('volume', 0)),
            ))
            if len(bars) >= max_bars:
                break
    return bars


def v5_classify(
    csv_path: str, symbol: str,
    max_bars: int = 2500, end_date: str = None,
    min_size_pct: float = MIN_ZHONGSHU_SIZE_PCT,
) -> Tuple[str, Dict]:
    """
    v5 分类主入口。

    Returns:
        (state, detail)
        state: "盘整" / "趋势-向上" / "趋势-向下" / "无"
        detail: {"n_zhongshu": int, "n_raw_zhongshu": int,
                 "n_movements": int, "direction": str}
    """
    # 1. 生产 R0 pipeline
    bars = csv_to_canonical_bars(csv_path, symbol, max_bars, end_date)
    if len(bars) < 10:
        return "无", {"reason": "K线不足", "n_bars": len(bars)}

    as_of = bars[-1].end_time
    r0_units = canonical_bars_to_recursive_r0(
        symbol=symbol, timeframe=R0_TIMEFRAME, bars=bars, as_of=as_of,
    )

    # 2. R0 movements（只取 confirmed）
    r0_result = analyze_same_rules(r0_units, level="R0")
    r0_movs = [m for m in r0_result.movements if m.status == "confirmed"]
    if len(r0_movs) < 3:
        return "无", {"reason": "R0 movements不足", "n_movements": len(r0_movs)}

    # 3. R1 段 = R0 movements 1:1 直转
    now = datetime.now(TZ_SH)
    segments = [
        RecursiveSegment(
            id=f"R1-SEG-{i:03d}", level="R1",
            direction=m.direction, status="confirmed", confirmed_at=now,
            high=m.high, low=m.low,
        )
        for i, m in enumerate(r0_movs)
    ]

    # 4. 生产 _build_zhongshus + 尺寸过滤
    zs_all = _build_zhongshus(segments, "R1")
    zs = []
    for z in zs_all:
        height = z.range_high - z.range_low
        mid = (z.range_high + z.range_low) / 2
        if mid > 0 and (height / mid * 100) >= min_size_pct:
            zs.append(z)

    detail_base = {
        "n_movements": len(r0_movs),
        "n_raw_zhongshu": len(zs_all),
        "n_zhongshu": len(zs),
    }

    if not zs:
        return "无", {**detail_base, "reason": "无有效中枢（尺寸过滤后）"}

    if len(zs) == 1:
        return "盘整", {**detail_base, "direction": "neutral"}

    # ≥2 个中枢 → 必须有不重叠的一对才算趋势（20261011 fix: 趋势=双中枢不重叠）
    def _has_non_overlapping(zs):
        for _i in range(len(zs)):
            for _j in range(_i + 1, len(zs)):
                _a, _b = zs[_i], zs[_j]
                if _a.range_high < _b.range_low or _b.range_high < _a.range_low:
                    return True
        return False
    if not _has_non_overlapping(zs):
        return "盘整", {**detail_base, "direction": "neutral", "reason": "中枢重叠"}
    # 有不重叠 → 趋势，方向由首末中枢中心决定
    centers = [(z.range_high + z.range_low) / 2 for z in zs]
    direction = "bullish" if centers[-1] > centers[0] else "bearish"
    state = "趋势-向上" if direction == "bullish" else "趋势-向下"
    return state, {**detail_base, "direction": direction}


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else \
        "/home/gorgesoros39/TradingAgents-CN/kline_cache/000001_5m.csv"
    sym = sys.argv[2] if len(sys.argv) > 2 else "000001"
    s, d = v5_classify(path, sym, max_bars=500)
    print(f"v5: {s}")
    print(f"detail: {d}")
