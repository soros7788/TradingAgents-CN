#!/usr/bin/env python3
"""
缠论分析引擎 - 自包含实现（不依赖 stock-chanlun 外部包）

实现核心缠论概念：
  分型 (Fractal)  → 顶分型/底分型识别
  笔 (Bi)         → 连接分型，至少间隔1根K线
  线段 (Segment)  → 笔的组合
  中枢 (Zhongshu) → 3段重叠区间
  买卖点 (Signal) → 一买/二买/三买/一卖/二卖/三卖 + 背驰

数据源: AKShare (腾讯接口)
运行: python scripts/chanlun_engine.py --code 002463 --level daily
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("chanlun")


# ══════════════════════════════════════════════════════════════
#  Data Models
# ══════════════════════════════════════════════════════════════

@dataclass
class Kline:
    date: pd.Timestamp
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Fractal:
    """分型"""
    index: int          # K线索引
    type: str           # "top" / "bottom"
    price: float        # 分型极值 (顶=high, 底=low)
    date: pd.Timestamp


@dataclass
class Bi:
    """笔"""
    start: Fractal
    end: Fractal
    direction: str      # "up" / "down"

    @property
    def high(self) -> float:
        return max(self.start.price, self.end.price)

    @property
    def low(self) -> float:
        return min(self.start.price, self.end.price)


@dataclass
class Segment:
    """线段"""
    start: Fractal
    end: Fractal
    direction: str      # "up" / "down"
    bis: List[Bi] = field(default_factory=list)

    @property
    def high(self) -> float:
        return max(self.start.price, self.end.price)

    @property
    def low(self) -> float:
        return min(self.start.price, self.end.price)


@dataclass
class Zhongshu:
    """中枢"""
    range_low: float
    range_high: float
    start: pd.Timestamp
    end: pd.Timestamp
    segments: List[Segment] = field(default_factory=list)

    @property
    def mid(self) -> float:
        return (self.range_low + self.range_high) / 2


@dataclass
class Signal:
    """买卖点信号"""
    type: str           # "一买" / "二买" / "三买" / "一卖" / "二卖" / "三卖"
    level: str          # "强" / "中" / "弱"
    price: float
    datetime: pd.Timestamp
    confidence: float = 0.5
    stop_loss: Optional[float] = None
    take_profit: Optional[float] = None
    description: str = ""


@dataclass
class ChanlunResult:
    """缠论分析结果"""
    stock_code: str
    level: str
    current_price: float
    trend: str
    fractals: List[Fractal] = field(default_factory=list)
    bis: List[Bi] = field(default_factory=list)
    segments: List[Segment] = field(default_factory=list)
    zhongshus: List[Zhongshu] = field(default_factory=list)
    signals: List[Signal] = field(default_factory=list)
    support_resistance: List[dict] = field(default_factory=list)
    summary: str = ""


# ══════════════════════════════════════════════════════════════
#  数据获取
# ══════════════════════════════════════════════════════════════

def fetch_kline_akshare(code: str, days: int = 120, period: str = "daily") -> Optional[pd.DataFrame]:
    """
    通过 AKShare 获取K线数据。

    Args:
        code: 股票代码 (6位)
        days: 日线天数
        period: "daily" / "30" / "5" / "1"

    Returns:
        DataFrame [date, open, high, low, close, volume] or None
    """
    try:
        import akshare as ak
    except ImportError:
        logger.error("请先安装 akshare: pip install akshare")
        return None

    sym = code.zfill(6)
    if sym.startswith(("6", "9", "688")):
        prefix = "sh"
    else:
        prefix = "sz"

    try:
        if period == "daily":
            df = ak.stock_zh_a_daily(symbol=f"{prefix}{sym}", adjust="qfq")
            if df is None or df.empty:
                return None
            df = df.tail(days).copy()
            df["date"] = pd.to_datetime(df["date"])
        else:
            df = ak.stock_zh_a_minute(symbol=f"{prefix}{sym}", period=period)
            if df is None or df.empty:
                return None
            df.rename(columns={"day": "date"}, inplace=True)
            df["date"] = pd.to_datetime(df["date"])
            for col in ["open", "high", "low", "close", "volume"]:
                df[col] = pd.to_numeric(df[col], errors="coerce")
            max_klines = {"30": 120, "5": 1000, "1": 2000}.get(period, 500)
            df = df.tail(max_klines).copy()

        if len(df) < 30:
            logger.warning("K线数据不足: %d 条", len(df))
            return None

        return df[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    except Exception as e:
        logger.error("获取K线失败 %s: %s", code, e)
        return None


# ══════════════════════════════════════════════════════════════
#  缠论核心算法
# ══════════════════════════════════════════════════════════════

def find_fractals(df: pd.DataFrame) -> List[Fractal]:
    """
    识别顶分型和底分型。

    顶分型: K[i].high > K[i-1].high 且 K[i].high > K[i+1].high
    底分型: K[i].low < K[i-1].low 且 K[i].low < K[i+1].low

    包含关系处理: 当两根K线存在包含关系时进行合并
    """
    if len(df) < 3:
        return []

    # Step 1: 处理包含关系，合并K线
    processed = _merge_inclusions(df)

    # Step 2: 识别分型
    fractals: List[Fractal] = []
    for i in range(1, len(processed) - 1):
        prev = processed.iloc[i - 1]
        curr = processed.iloc[i]
        next_ = processed.iloc[i + 1]

        # 顶分型
        if curr["high"] > prev["high"] and curr["high"] > next_["high"]:
            fractals.append(Fractal(
                index=curr["_orig_idx"],
                type="top",
                price=curr["high"],
                date=curr["date"],
            ))
        # 底分型
        elif curr["low"] < prev["low"] and curr["low"] < next_["low"]:
            fractals.append(Fractal(
                index=curr["_orig_idx"],
                type="bottom",
                price=curr["low"],
                date=curr["date"],
            ))

    # Step 3: 分型必须交替 (顶-底-顶-底...)
    fractals = _ensure_alternating(fractals)

    logger.info("分型识别: %d 个 (顶: %d, 底: %d)",
                len(fractals),
                sum(1 for f in fractals if f.type == "top"),
                sum(1 for f in fractals if f.type == "bottom"))
    return fractals


def _merge_inclusions(df: pd.DataFrame) -> pd.DataFrame:
    """
    处理K线包含关系:
    - 如果 K[i] 和 K[i+1] 存在包含关系 (一方完全包含另一方)，则合并
    - 上升趋势: 取高高
    - 下降趋势: 取低低
    """
    if len(df) < 2:
        result = df.copy()
        result["_orig_idx"] = range(len(result))
        return result

    rows = []
    orig_indices = []

    i = 0
    while i < len(df):
        if i == 0:
            rows.append(df.iloc[0].to_dict())
            orig_indices.append(i)
            i += 1
            continue

        curr = df.iloc[i]
        prev = rows[-1]

        # 检查包含关系
        high_contain = prev["high"] >= curr["high"] and prev["low"] <= curr["low"]
        low_contain = curr["high"] >= prev["high"] and curr["low"] <= prev["low"]

        if high_contain or low_contain:
            # 判断方向: 看前几根K线
            if len(rows) >= 2:
                direction_up = prev["high"] > rows[-2]["high"]
            else:
                direction_up = curr["close"] > curr["open"]

            if direction_up:
                # 上升中取高高
                merged = prev.copy()
                merged["high"] = max(prev["high"], curr["high"])
                merged["low"] = max(prev["low"], curr["low"])
                merged["close"] = curr["close"]
                merged["date"] = curr["date"]
            else:
                # 下降中取低低
                merged = prev.copy()
                merged["high"] = min(prev["high"], curr["high"])
                merged["low"] = min(prev["low"], curr["low"])
                merged["close"] = curr["close"]
                merged["date"] = curr["date"]

            rows[-1] = merged
            # 不增加 orig_idx，因为是合并
            i += 1
        else:
            rows.append(curr.to_dict())
            orig_indices.append(i)
            i += 1

    result = pd.DataFrame(rows)
    result["_orig_idx"] = orig_indices
    return result


def _ensure_alternating(fractals: List[Fractal]) -> List[Fractal]:
    """确保分型严格交替 (顶底交替)。同类型连续出现时保留更极端的。"""
    if len(fractals) <= 1:
        return fractals

    result = [fractals[0]]
    for f in fractals[1:]:
        if f.type == result[-1].type:
            # 同类型，保留更极端的
            if f.type == "top" and f.price > result[-1].price:
                result[-1] = f
            elif f.type == "bottom" and f.price < result[-1].price:
                result[-1] = f
        else:
            # 不同类型，检查间隔是否足够 (至少1根K线)
            if abs(f.index - result[-1].index) >= 3:
                result.append(f)
            else:
                # 间隔不够，但如果更极端也替换
                if f.type == "top" and f.price > result[-1].price:
                    result[-1] = f
                elif f.type == "bottom" and f.price < result[-1].price:
                    result[-1] = f

    return result


def find_bis(fractals: List[Fractal]) -> List[Bi]:
    """
    从分型构建笔。

    笔的条件:
    1. 顶底分型交替
    2. 顶底之间至少有1根独立K线 (间隔 >= 3 根)
    3. 顶分型高点 > 底分型低点
    """
    bis: List[Bi] = []
    if len(fractals) < 2:
        return bis

    i = 0
    while i < len(fractals) - 1:
        start = fractals[i]
        end = fractals[i + 1]

        # 必须顶底交替
        if start.type == end.type:
            i += 1
            continue

        # 间隔检查 (至少1根独立K线 = 索引差 >= 3)
        if abs(end.index - start.index) < 3:
            i += 1
            continue

        # 价格合理性
        if start.type == "bottom" and end.type == "top":
            if end.price <= start.price:
                i += 1
                continue
            direction = "up"
        elif start.type == "top" and end.type == "bottom":
            if end.price >= start.price:
                i += 1
                continue
            direction = "down"
        else:
            i += 1
            continue

        bis.append(Bi(start=start, end=end, direction=direction))
        i += 1

    logger.info("笔识别: %d 笔", len(bis))
    return bis


def find_segments(bis: List[Bi]) -> List[Segment]:
    """
    从笔构建线段。

    线段由至少3笔组成，其中前三笔必须有重叠部分。
    简化实现: 使用特征序列法。
    """
    segments: List[Segment] = []
    if len(bis) < 3:
        return segments

    i = 0
    while i < len(bis) - 2:
        # 取连续3笔
        b1, b2, b3 = bis[i], bis[i + 1], bis[i + 2]

        # 特征序列: b2 的反向笔
        # 如果 b1 是上升笔, b2 是下降笔, b3 是上升笔
        # 线段破坏条件: b2 的低点 < b1 的低点 (上升线段) 或 b2 的高点 > b1 的高点 (下降线段)

        if b1.direction == "up" and b2.direction == "down":
            # 上升线段的破坏: 下降笔的低点低于前一笔的低点
            if b2.low < b1.low:
                # 上升线段被破坏
                seg = _build_segment(bis, 0, i + 1)
                if seg:
                    segments.append(seg)
                i += 1
                continue

        elif b1.direction == "down" and b2.direction == "up":
            # 下降线段的破坏: 上升笔的高点高于前一笔的高点
            if b2.high > b1.high:
                seg = _build_segment(bis, 0, i + 1)
                if seg:
                    segments.append(seg)
                i += 1
                continue

        # 如果3笔完成但没有破坏，继续扩展
        # 找线段终点
        end_idx = _find_segment_end(bis, i)
        if end_idx > i + 2:
            seg = _build_segment(bis, i, end_idx)
            if seg:
                segments.append(seg)
            i = end_idx
        else:
            i += 1

    # 处理剩余笔
    if len(bis) - i >= 3:
        seg = _build_segment(bis, i, len(bis) - 1)
        if seg:
            segments.append(seg)

    logger.info("线段识别: %d 段", len(segments))
    return segments


def _find_segment_end(bis: List[Bi], start: int) -> int:
    """找线段的终点索引。"""
    if start + 2 >= len(bis):
        return start + 2

    first = bis[start]
    direction = first.direction

    for i in range(start + 2, len(bis)):
        curr = bis[i]
        if direction == "up":
            # 上升线段: 当出现一笔低点低于前一笔低点时确认
            if curr.direction == "down" and i >= start + 1:
                prev_down = bis[i - 1] if i > start else None
                if prev_down and curr.low < prev_down.low:
                    return i - 1
        else:
            # 下降线段: 当出现一笔高点高于前一笔高点时确认
            if curr.direction == "up" and i >= start + 1:
                prev_up = bis[i - 1] if i > start else None
                if prev_up and curr.high > prev_up.high:
                    return i - 1

    return len(bis) - 1


def _build_segment(bis: List[Bi], start: int, end: int) -> Optional[Segment]:
    """从笔构建线段。"""
    if end - start < 2:
        return None

    seg_bis = bis[start:end + 1]
    direction = seg_bis[0].direction

    # 修正: 确保线段方向正确
    highs = [b.high for b in seg_bis]
    lows = [b.low for b in seg_bis]

    if direction == "up":
        # 上升线段: 高点创新高
        if max(highs) <= highs[0]:
            direction = "down"
    else:
        # 下降线段: 低点创新低
        if min(lows) >= lows[0]:
            direction = "up"

    start_fractal = seg_bis[0].start
    end_fractal = seg_bis[-1].end

    return Segment(
        start=start_fractal,
        end=end_fractal,
        direction=direction,
        bis=seg_bis,
    )


def find_zhongshus(segments: List[Segment]) -> List[Zhongshu]:
    """
    从线段构建中枢。

    中枢: 至少3段重叠区间。
    ZG = min(各段高点), ZD = max(各段低点)
    如果 ZG > ZD, 则形成中枢。
    """
    zhongshus: List[Zhongshu] = []
    if len(segments) < 3:
        return zhongshus

    i = 0
    while i < len(segments) - 2:
        # 取连续3段
        s1, s2, s3 = segments[i], segments[i + 1], segments[i + 2]

        highs = [s1.high, s2.high, s3.high]
        lows = [s1.low, s2.low, s3.low]

        zg = min(highs)   # 中枢上沿
        zd = max(lows)    # 中枢下沿

        if zg > zd:
            # 形成中枢
            zs = Zhongshu(
                range_low=zd,
                range_high=zg,
                start=s1.start.date,
                end=s3.end.date,
                segments=[s1, s2, s3],
            )

            # 尝试扩展中枢
            j = i + 3
            while j < len(segments):
                sj = segments[j]
                # 检查是否与中枢重叠
                new_high = min(zg, sj.high)
                new_low = max(zd, sj.low)
                if new_high > new_low:
                    # 延伸中枢
                    zg = new_high
                    zd = new_low
                    zs.range_low = zd
                    zs.range_high = zg
                    zs.end = sj.end.date
                    zs.segments.append(sj)
                    j += 1
                else:
                    break

            zhongshus.append(zs)
            i = j
        else:
            i += 1

    logger.info("中枢识别: %d 个", len(zhongshus))
    return zhongshus


def find_signals(
    bis: List[Bi],
    segments: List[Segment],
    zhongshus: List[Zhongshu],
    df: pd.DataFrame,
) -> List[Signal]:
    """
    识别买卖点信号。

    规则:
    - 一买: 下跌趋势中，最后一个中枢下方出现底分型 + 背驰
    - 二买: 一买后的回调低点不破一买价格
    - 三买: 回调不破中枢下沿
    - 一卖: 上涨趋势中，最后一个中枢上方出现顶分型 + 背驰
    - 二卖: 一卖后的反弹高点不破一卖价格
    - 三卖: 反弹不破中枢上沿

    背驰判断: MACD 面积比较 / 趋势力度比较
    """
    signals: List[Signal] = []

    if len(bis) < 3 or len(zhongshus) < 1:
        return signals

    # 使用 MACD 判断背驰
    macd_bullish, macd_bearish = _detect_divergence(df)

    # 遍历所有笔寻找买卖点
    for i, bi in enumerate(bis):
        # 买点检测
        if bi.direction == "down" and i > 0:
            prev_bi = bis[i - 1]

            # 一买: 底分型 + 背驰
            if bi.end.type == "bottom":
                divergence = _check_divergence(df, bi, prev_bi, "bottom")

                # 检查是否在中枢下方
                below_zhongshu = False
                for zs in zhongshus:
                    if bi.end.price < zs.range_low:
                        below_zhongshu = True
                        break

                if divergence and below_zhongshu:
                    signals.append(Signal(
                        type="一买",
                        level="强",
                        price=bi.end.price,
                        datetime=bi.end.date,
                        confidence=0.8,
                        stop_loss=bi.end.price * 0.97,
                        take_profit=bi.end.price * 1.15,
                        description=f"底背驰+中枢下方，一买@{bi.end.price:.2f}",
                    ))
                elif divergence:
                    signals.append(Signal(
                        type="一买",
                        level="中",
                        price=bi.end.price,
                        datetime=bi.end.date,
                        confidence=0.6,
                        stop_loss=bi.end.price * 0.97,
                        description=f"底背驰，一买@{bi.end.price:.2f}",
                    ))

            # 二买: 回调低点不破前低
            if bi.end.type == "bottom" and i >= 2:
                prev_bottoms = [b for b in bis[:i] if b.end.type == "bottom"]
                if prev_bottoms:
                    prev_low = min(b.end.price for b in prev_bottoms[-3:])
                    if bi.end.price > prev_low and bi.end.price < bi.start.price:
                        # 检查是否有MACD底背驰
                        if _check_divergence(df, bi, bis[i - 2] if i >= 2 else bi, "bottom"):
                            signals.append(Signal(
                                type="二买",
                                level="中",
                                price=bi.end.price,
                                datetime=bi.end.date,
                                confidence=0.65,
                                stop_loss=bi.end.price * 0.97,
                                take_profit=bi.end.price * 1.12,
                                description=f"回调不破前低，二买@{bi.end.price:.2f}",
                            ))

        # 卖点检测
        if bi.direction == "up" and i > 0:
            prev_bi = bis[i - 1]

            # 一卖: 顶分型 + 背驰
            if bi.end.type == "top":
                divergence = _check_divergence(df, bi, prev_bi, "top")

                above_zhongshu = False
                for zs in zhongshus:
                    if bi.end.price > zs.range_high:
                        above_zhongshu = True
                        break

                if divergence and above_zhongshu:
                    signals.append(Signal(
                        type="一卖",
                        level="强",
                        price=bi.end.price,
                        datetime=bi.end.date,
                        confidence=0.8,
                        stop_loss=bi.end.price * 1.03,
                        take_profit=bi.end.price * 0.85,
                        description=f"顶背驰+中枢上方，一卖@{bi.end.price:.2f}",
                    ))
                elif divergence:
                    signals.append(Signal(
                        type="一卖",
                        level="中",
                        price=bi.end.price,
                        datetime=bi.end.date,
                        confidence=0.6,
                        stop_loss=bi.end.price * 1.03,
                        description=f"顶背驰，一卖@{bi.end.price:.2f}",
                    ))

            # 二卖: 反弹高点不破前高
            if bi.end.type == "top" and i >= 2:
                prev_tops = [b for b in bis[:i] if b.end.type == "top"]
                if prev_tops:
                    prev_high = max(b.end.price for b in prev_tops[-3:])
                    if bi.end.price < prev_high and bi.end.price > bi.start.price:
                        if _check_divergence(df, bi, bis[i - 2] if i >= 2 else bi, "top"):
                            signals.append(Signal(
                                type="二卖",
                                level="中",
                                price=bi.end.price,
                                datetime=bi.end.date,
                                confidence=0.65,
                                stop_loss=bi.end.price * 1.03,
                                take_profit=bi.end.price * 0.88,
                                description=f"反弹不破前高，二卖@{bi.end.price:.2f}",
                            ))

    # 三买/三卖检测
    for zs in zhongshus:
        for i, bi in enumerate(bis):
            # 三买: 回调不破中枢下沿
            if bi.direction == "down" and bi.end.type == "bottom":
                if zs.range_low <= bi.end.price < zs.range_high:
                    # 在中枢形成后的回调
                    if bi.end.date > zs.end:
                        # 检查是否有买点
                        has_buy_after = any(
                            s for s in signals
                            if s.type in ("一买", "二买") and s.datetime > zs.end
                        )
                        if not has_buy_after:
                            signals.append(Signal(
                                type="三买",
                                level="中",
                                price=bi.end.price,
                                datetime=bi.end.date,
                                confidence=0.55,
                                stop_loss=zs.range_low * 0.97,
                                take_profit=zs.range_high * 1.05,
                                description=f"回调不破中枢下沿，三买@{bi.end.price:.2f}",
                            ))

            # 三卖: 反弹不破中枢上沿
            if bi.direction == "up" and bi.end.type == "top":
                if zs.range_low < bi.end.price <= zs.range_high:
                    if bi.end.date > zs.end:
                        has_sell_after = any(
                            s for s in signals
                            if s.type in ("一卖", "二卖") and s.datetime > zs.end
                        )
                        if not has_sell_after:
                            signals.append(Signal(
                                type="三卖",
                                level="中",
                                price=bi.end.price,
                                datetime=bi.end.date,
                                confidence=0.55,
                                stop_loss=zs.range_high * 1.03,
                                take_profit=zs.range_low * 0.95,
                                description=f"反弹不破中枢上沿，三卖@{bi.end.price:.2f}",
                            ))

    # 按时间排序
    signals.sort(key=lambda s: s.datetime)

    # 去重 (同位置同类型只保留最强的)
    unique_signals = []
    for s in signals:
        duplicate = False
        for u in unique_signals:
            if abs(s.price - u.price) / max(s.price, 1) < 0.02 and s.type == u.type:
                if s.confidence > u.confidence:
                    unique_signals.remove(u)
                    break
                else:
                    duplicate = True
                    break
        if not duplicate:
            unique_signals.append(s)

    signals = unique_signals
    logger.info("信号识别: %d 个", len(signals))
    return signals


def _check_divergence(
    df: pd.DataFrame,
    curr_bi: Bi,
    prev_bi: Bi,
    fractal_type: str,
) -> bool:
    """
    检查背驰。

    方法: 比较两个同向笔的MACD面积 (红柱/绿柱总和)。
    价格创新高/低，但MACD面积减小 = 背驰。
    """
    if len(df) < 30:
        return False

    # 计算 MACD
    closes = df["close"].values
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26
    dea = _ema(dif, 9)
    macd = (dif - dea) * 2

    # 获取笔对应的MACD区间
    curr_start_idx = _find_closest_idx(df, curr_bi.start.date)
    curr_end_idx = _find_closest_idx(df, curr_bi.end.date)
    prev_start_idx = _find_closest_idx(df, prev_bi.start.date)
    prev_end_idx = _find_closest_idx(df, prev_bi.end.date)

    if curr_start_idx is None or curr_end_idx is None:
        return False
    if prev_start_idx is None or prev_end_idx is None:
        return False

    curr_area = abs(np.sum(macd[min(curr_start_idx, curr_end_idx):max(curr_start_idx, curr_end_idx) + 1]))
    prev_area = abs(np.sum(macd[min(prev_start_idx, prev_end_idx):max(prev_start_idx, prev_end_idx) + 1]))

    if prev_area == 0:
        return False

    # 当前面积 < 前一段面积 = 背驰
    return curr_area < prev_area * 0.8


def _ema(data: np.ndarray, period: int) -> np.ndarray:
    """指数移动平均。"""
    result = np.zeros_like(data)
    result[0] = data[0]
    multiplier = 2 / (period + 1)
    for i in range(1, len(data)):
        result[i] = (data[i] - result[i - 1]) * multiplier + result[i - 1]
    return result


def _find_closest_idx(df: pd.DataFrame, date: pd.Timestamp) -> Optional[int]:
    """找最接近的日期索引。"""
    matches = df[df["date"] <= date]
    if len(matches) == 0:
        return None
    return matches.index[-1]


def _detect_divergence(df: pd.DataFrame) -> Tuple[bool, bool]:
    """检测MACD顶/底背驰。"""
    if len(df) < 30:
        return False, False

    closes = df["close"].values
    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif = ema12 - ema26

    # 简单判断: DIF 与价格的背离
    price_highs = []
    dif_highs = []
    price_lows = []
    dif_lows = []

    for i in range(2, len(closes) - 2):
        # 价格高点
        if closes[i] > closes[i - 1] and closes[i] > closes[i + 1]:
            price_highs.append((i, closes[i]))
            dif_highs.append((i, dif[i]))
        # 价格低点
        if closes[i] < closes[i - 1] and closes[i] < closes[i + 1]:
            price_lows.append((i, closes[i]))
            dif_lows.append((i, dif[i]))

    bullish = False  # 底背驰
    bearish = False  # 顶背驰

    if len(price_lows) >= 2:
        p1, p2 = price_lows[-2], price_lows[-1]
        d1, d2 = dif_lows[-2], dif_lows[-1]
        if p2[1] < p1[1] and d2[1] > d1[1]:
            bullish = True

    if len(price_highs) >= 2:
        p1, p2 = price_highs[-2], price_highs[-1]
        d1, d2 = dif_highs[-2], dif_highs[-1]
        if p2[1] > p1[1] and d2[1] < d1[1]:
            bearish = True

    return bullish, bearish


def _compute_support_resistance(
    fractals: List[Fractal],
    zhongshus: List[Zhongshu],
    current_price: float,
) -> List[dict]:
    """计算支撑阻力位。"""
    levels = []

    # 来自中枢
    for zs in zhongshus:
        if zs.range_low < current_price:
            levels.append({"type": "support", "price": round(zs.range_low, 2), "source": "中枢", "strength": 0.7})
        if zs.range_high > current_price:
            levels.append({"type": "resistance", "price": round(zs.range_high, 2), "source": "中枢", "strength": 0.7})

    # 来自分型
    for f in fractals:
        if f.type == "bottom" and f.price < current_price:
            levels.append({"type": "support", "price": round(f.price, 2), "source": "底分型", "strength": 0.5})
        elif f.type == "top" and f.price > current_price:
            levels.append({"type": "resistance", "price": round(f.price, 2), "source": "顶分型", "strength": 0.5})

    # 去重 + 排序
    seen = set()
    unique = []
    for l in sorted(levels, key=lambda x: abs(x["price"] - current_price)):
        if l["price"] not in seen:
            seen.add(l["price"])
            unique.append(l)

    return unique[:8]


def _determine_trend(bis: List[Bi], zhongshus: List[Zhongshu], df: pd.DataFrame) -> str:
    """判断当前趋势。"""
    if len(bis) < 3:
        return "震荡"

    recent_bis = bis[-5:] if len(bis) >= 5 else bis

    up_count = sum(1 for b in recent_bis if b.direction == "up")
    down_count = sum(1 for b in recent_bis if b.direction == "down")

    # 检查高点和低点的演化
    highs = [b.high for b in recent_bis]
    lows = [b.low for b in recent_bis]

    higher_highs = highs[-1] > highs[0] if len(highs) > 1 else False
    lower_lows = lows[-1] < lows[0] if len(lows) > 1 else False

    if higher_highs and up_count > down_count:
        return "上涨趋势"
    elif lower_lows and down_count > up_count:
        return "下跌趋势"
    elif higher_highs and lower_lows:
        return "震荡"
    else:
        return "震荡"


# ══════════════════════════════════════════════════════════════
#  主分析函数
# ══════════════════════════════════════════════════════════════

def analyze(
    stock_code: str,
    level: str = "daily",
    days: int = 120,
) -> Optional[ChanlunResult]:
    """
    对单只股票进行缠论分析。

    Args:
        stock_code: 6位股票代码
        level: "daily" / "30min" / "5min" / "1min"
        days: 日线K线天数

    Returns:
        ChanlunResult or None
    """
    period_map = {
        "daily": ("daily", days),
        "30min": ("30", 120),
        "5min": ("5", 1000),
        "1min": ("1", 2000),
    }

    if level not in period_map:
        logger.error("未知级别: %s", level)
        return None

    period, klines = period_map[level]

    logger.info("获取 %s %s 级别K线 (周期=%s, 数量=%d)...", stock_code, level, period, klines)
    df = fetch_kline_akshare(stock_code, days=days if level == "daily" else klines, period=period)

    if df is None or len(df) < 30:
        logger.error("K线数据不足")
        return None

    logger.info("K线获取成功: %d 条", len(df))

    # 执行缠论分析
    fractals = find_fractals(df)
    bis = find_bis(fractals)
    segments = find_segments(bis)
    zhongshus = find_zhongshus(segments)
    signals = find_signals(bis, segments, zhongshus, df)

    current_price = float(df["close"].iloc[-1])
    trend = _determine_trend(bis, zhongshus, df)
    sr = _compute_support_resistance(fractals, zhongshus, current_price)

    # 生成总结
    latest_zs = zhongshus[-1] if zhongshus else None
    latest_sig = signals[-1] if signals else None

    summary_parts = [f"当前处于{trend}。"]
    if latest_zs:
        summary_parts.append(f"最新中枢区间 [{latest_zs.range_low:.2f}, {latest_zs.range_high:.2f}]。")
    if latest_sig:
        summary_parts.append(f"最近信号: {latest_sig.type}@{latest_sig.price:.2f} (置信度 {latest_sig.confidence:.0%})。")
    if not signals:
        summary_parts.append("暂无明确买卖点信号，建议观望。")

    return ChanlunResult(
        stock_code=stock_code,
        level=level,
        current_price=current_price,
        trend=trend,
        fractals=fractals,
        bis=bis,
        segments=segments,
        zhongshus=zhongshus,
        signals=signals,
        support_resistance=sr,
        summary="".join(summary_parts),
    )


def result_to_dict(result: ChanlunResult) -> dict:
    """转成可序列化的dict。"""
    def _f(v):
        """转成原生float。"""
        return round(float(v), 2) if v is not None else None

    return {
        "stock_code": result.stock_code,
        "level": result.level,
        "current_price": _f(result.current_price),
        "trend": result.trend,
        "fractals": [
            {"index": int(f.index), "type": f.type, "price": _f(f.price), "date": f.date.strftime("%Y-%m-%d %H:%M")}
            for f in result.fractals[-10:]
        ],
        "bis": [
            {
                "direction": b.direction,
                "high": _f(b.high),
                "low": _f(b.low),
                "start": b.start.date.strftime("%Y-%m-%d %H:%M"),
                "end": b.end.date.strftime("%Y-%m-%d %H:%M"),
            }
            for b in result.bis[-10:]
        ],
        "segments": [
            {
                "direction": s.direction,
                "high": _f(s.high),
                "low": _f(s.low),
                "start": s.start.date.strftime("%Y-%m-%d %H:%M"),
                "end": s.end.date.strftime("%Y-%m-%d %H:%M"),
            }
            for s in result.segments[-5:]
        ],
        "zhongshus": [
            {
                "range_low": _f(zs.range_low),
                "range_high": _f(zs.range_high),
                "start": zs.start.strftime("%Y-%m-%d"),
                "end": zs.end.strftime("%Y-%m-%d"),
            }
            for zs in result.zhongshus[-3:]
        ],
        "signals": [
            {
                "type": s.type,
                "level": s.level,
                "price": _f(s.price),
                "datetime": s.datetime.strftime("%Y-%m-%d %H:%M"),
                "confidence": _f(s.confidence),
                "stop_loss": _f(s.stop_loss),
                "take_profit": _f(s.take_profit),
                "description": s.description,
            }
            for s in result.signals[-8:]
        ],
        "support_resistance": [
            {
                "type": sr["type"],
                "price": _f(sr["price"]),
                "source": sr["source"],
                "strength": _f(sr["strength"]),
            }
            for sr in result.support_resistance
        ],
        "summary": result.summary,
    }


def result_to_markdown(result: ChanlunResult) -> str:
    """转成Markdown报告。"""
    lines = [
        f"# {result.stock_code} 缠论分析 ({result.level})",
        "",
        f"**当前价**: {result.current_price:.2f}",
        f"**趋势**: {result.trend}",
        f"**总结**: {result.summary}",
        "",
    ]

    if result.zhongshus:
        lines.append("## 中枢")
        for zs in result.zhongshus[-3:]:
            lines.append(f"- [{zs.range_low:.2f}, {zs.range_high:.2f}] ({zs.start.strftime('%Y-%m-%d')} ~ {zs.end.strftime('%Y-%m-%d')})")
        lines.append("")

    if result.signals:
        lines.append("## 买卖点信号")
        for s in result.signals[-8:]:
            lines.append(f"- **{s.type}** ({s.level}) @ {s.price:.2f}  置信度 {s.confidence:.0%}")
            if s.description:
                lines.append(f"  - {s.description}")
        lines.append("")

    if result.support_resistance:
        supports = [f"{x['price']:.2f}" for x in result.support_resistance if x["type"] == "support"][:3]
        resistances = [f"{x['price']:.2f}" for x in result.support_resistance if x["type"] == "resistance"][:3]
        if supports:
            lines.append(f"**支撑**: {' / '.join(supports)}")
        if resistances:
            lines.append(f"**阻力**: {' / '.join(resistances)}")

    return "\n".join(lines)


# ══════════════════════════════════════════════════════════════
#  区间套策略
# ══════════════════════════════════════════════════════════════

class IntervalStrategy:
    """
    区间套交易策略: 同时监控多级别。

    原则: 大级别定方向，小级别找买卖点。
    """

    def __init__(self, stock_code: str):
        self.stock_code = stock_code.zfill(6)

    def run(self) -> dict:
        """运行多级别分析。"""
        configs = [
            ("30min", 120),
            ("5min", 1000),
            ("1min", 2000),
        ]

        levels = {}
        for level, klines in configs:
            logger.info("分析 %s 级别...", level)
            result = analyze(self.stock_code, level=level)
            if result:
                levels[level] = result
            else:
                logger.warning("%s 级别分析失败", level)

        advice = self._judge(levels)

        # 生成文本
        lines = [
            f"=== {self.stock_code} 区间套交易策略 ===",
            f"当前价: {list(levels.values())[0].current_price if levels else 'N/A'}",
            "",
            "【各级别状态】",
        ]

        for lv_name in ["30min", "5min", "1min"]:
            lv = levels.get(lv_name)
            if lv:
                zs_str = ""
                if lv.zhongshus:
                    zs = lv.zhongshus[-1]
                    zs_str = f" 中枢[{zs.range_low:.2f}, {zs.range_high:.2f}]"
                sig_str = " 无信号"
                if lv.signals:
                    s = lv.signals[-1]
                    sig_str = f" {s.type}@{s.price:.2f} ({s.confidence:.0%})"
                lines.append(f"  {lv_name}: {lv.trend}{sig_str}{zs_str}")
            else:
                lines.append(f"  {lv_name}: 数据缺失")

        lines.extend([
            "",
            "【交易建议】",
            f"  操作: {advice['action']}",
            f"  置信度: {advice['confidence']:.0%}",
            f"  风险: {advice['risk_level']}",
        ])
        if advice.get("target_zone"):
            lines.append(f"  目标区间: [{advice['target_zone'][0]:.2f}, {advice['target_zone'][1]:.2f}]")
        if advice.get("stop_loss"):
            lines.append(f"  止损: {advice['stop_loss']:.2f}")
        lines.extend([
            "",
            "【逻辑】",
            f"  {advice['reasoning']}",
        ])

        return {
            "text": "\n".join(lines),
            "levels": {k: result_to_dict(v) for k, v in levels.items()},
            "advice": advice,
        }

    def _judge(self, levels: dict) -> dict:
        """区间套核心判断。"""
        m30 = levels.get("30min")
        m5 = levels.get("5min")
        m1 = levels.get("1min")

        if not m30 or not m5:
            return {
                "action": "数据不足",
                "confidence": 0.0,
                "target_zone": None,
                "stop_loss": None,
                "reasoning": "30分钟或5分钟数据缺失",
                "risk_level": "高",
            }

        # 大级别方向
        big_sig = m30.signals[-1] if m30.signals else None
        big_direction = "无"
        if big_sig:
            if "买" in big_sig.type:
                big_direction = "买"
            elif "卖" in big_sig.type:
                big_direction = "卖"
            elif "背驰" in m30.trend:
                big_direction = "卖" if "上涨" in m30.trend else "买"

        # 小级别信号
        small_buy = None
        for lv in [m5, m1]:
            if lv and lv.signals:
                for s in reversed(lv.signals):
                    if "买" in s.type:
                        small_buy = f"{lv.level}({s.type})"
                        break
                if small_buy:
                    break

        small_sell = None
        for lv in [m5, m1]:
            if lv and lv.signals:
                for s in reversed(lv.signals):
                    if "卖" in s.type:
                        small_sell = f"{lv.level}({s.type})"
                        break
                if small_sell:
                    break

        # 情形1: 大级别卖点
        if big_direction == "卖":
            if small_buy:
                return {
                    "action": "等待",
                    "confidence": 0.7,
                    "target_zone": None,
                    "stop_loss": None,
                    "reasoning": f"30分钟级别{big_sig.type}@{big_sig.price:.2f}，大方向偏空。{small_buy}出现买点，但只能视为反弹，不可重仓。",
                    "risk_level": "高",
                }
            else:
                zs = m30.zhongshus[-1] if m30.zhongshus else None
                return {
                    "action": "等待/做空",
                    "confidence": 0.8,
                    "target_zone": (zs.range_low, zs.range_high) if zs else None,
                    "stop_loss": big_sig.price * 1.03 if big_sig else None,
                    "reasoning": f"30分钟{big_sig.type}@{big_sig.price:.2f}，5分钟/1分钟均无买点，共振下跌中。",
                    "risk_level": "中",
                }

        # 情形2: 大级别买点
        if big_direction == "买":
            zs = m30.zhongshus[-1] if m30.zhongshus else None
            return {
                "action": "重仓做多",
                "confidence": 0.85,
                "target_zone": (zs.range_high, zs.range_high * 1.05) if zs else None,
                "stop_loss": big_sig.price * 0.97 if big_sig else None,
                "reasoning": f"30分钟{big_sig.type}@{big_sig.price:.2f}确认，大方向转多。可重仓。",
                "risk_level": "低",
            }

        # 情形3: 大级别无信号，中级别有卖点
        m5_sig = m5.signals[-1] if m5.signals else None
        if m5_sig and "卖" in m5_sig.type:
            return {
                "action": "减仓观望",
                "confidence": 0.6,
                "target_zone": None,
                "stop_loss": None,
                "reasoning": f"30分钟无明确信号，5分钟{m5_sig.type}@{m5_sig.price:.2f}，中级别偏空。",
                "risk_level": "中",
            }

        # 情形4: 大级别无信号，中级别有买点
        if m5_sig and "买" in m5_sig.type:
            return {
                "action": "轻仓试多",
                "confidence": 0.6,
                "target_zone": None,
                "stop_loss": m5_sig.price * 0.97,
                "reasoning": f"30分钟无明确信号，5分钟{m5_sig.type}@{m5_sig.price:.2f}，可轻仓试多。",
                "risk_level": "中",
            }

        # 情形5: 全部无信号
        return {
            "action": "等待",
            "confidence": 0.5,
            "target_zone": None,
            "stop_loss": None,
            "reasoning": "多级别均无明确买卖点，趋势不明，观望为主。",
            "risk_level": "中",
        }


# ══════════════════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="缠论分析引擎")
    parser.add_argument("--code", required=True, help="股票代码 (6位), 如 002463")
    parser.add_argument("--level", default="daily", choices=["daily", "30min", "5min", "1min"],
                        help="分析级别")
    parser.add_argument("--days", type=int, default=120, help="日线K线天数")
    parser.add_argument("--strategy", action="store_true", help="运行区间套策略")
    parser.add_argument("--json", action="store_true", help="输出JSON格式")
    parser.add_argument("--interval", action="store_true", help="运行区间套策略")

    args = parser.parse_args()

    if args.strategy or args.interval:
        strategy = IntervalStrategy(args.code)
        result = strategy.run()
        print(result["text"])
        if args.json:
            print("\n--- JSON ---")
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        result = analyze(args.code, level=args.level, days=args.days)
        if result is None:
            print(f"分析失败: {args.code} {args.level}")
            sys.exit(1)

        if args.json:
            print(json.dumps(result_to_dict(result), ensure_ascii=False, indent=2))
        else:
            print(result_to_markdown(result))


if __name__ == "__main__":
    main()