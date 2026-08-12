"""
区间套交易策略引擎

使用自包含的缠论引擎 (scripts/chanlun_engine.py)，不再依赖 stock-chanlun 外部包。

核心逻辑: "大级别定方向，小级别找买卖点"
- 30分钟级别定大方向
- 5分钟级别找中级别买卖点
- 1分钟级别找精确入场点

用法:
    from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy
    s = ChanlunStrategy('002463')
    advice = s.run()
    print(advice['text'])
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

# 将 scripts 目录加入 path
_ROOT = Path(__file__).resolve().parent.parent.parent
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from chanlun_engine import analyze as _chanlun_analyze  # noqa: E402
from chanlun_engine import IntervalStrategy, ChanlunResult  # noqa: E402


@dataclass
class LevelSignal:
    """单个级别的信号摘要"""
    level: str
    trend: str
    latest_signal_type: str
    latest_signal_price: float
    latest_signal_dt: str
    zhongshu_low: Optional[float]
    zhongshu_high: Optional[float]
    current_price: float
    summary: str


@dataclass
class TradeAdvice:
    """交易建议"""
    action: str
    confidence: float
    target_zone: Optional[Tuple[float, float]]
    stop_loss: Optional[float]
    reasoning: str
    risk_level: str


class ChanlunStrategy:
    """
    区间套交易策略引擎。

    核心逻辑:
        1. 30min 级别定大方向
        2. 5min 级别找中级别买卖点
        3. 1min 级别找精确入场点
        4. 大级别卖点区域，小级别买点只作短线/观望
        5. 大级别买点区域，小级别买点可重仓
    """

    def __init__(self, code: str):
        self.code = code.zfill(6)

    def _fetch_and_analyze(self, level: str, period: str, klines: int) -> Optional[Dict]:
        """拉取K线并跑缠论分析，返回简化结果。"""
        try:
            result = _chanlun_analyze(self.code, level=level)
            if result is None:
                return None

            latest_sig = result.signals[-1] if result.signals else None
            zs_low = zs_high = None
            if result.zhongshus:
                zs = result.zhongshus[-1]
                zs_low, zs_high = zs.range_low, zs.range_high

            sig_price = 0.0
            sig_type = "无"
            sig_dt = ""
            if latest_sig:
                sig_price = latest_sig.price
                sig_type = latest_sig.type
                sig_dt = latest_sig.datetime.strftime("%m-%d %H:%M") if latest_sig.datetime else ""

            return {
                'level': level,
                'trend': result.trend,
                'latest_signal': latest_sig,
                'zhongshu_low': zs_low,
                'zhongshu_high': zs_high,
                'current_price': result.current_price,
                'summary': result.summary,
                'sig_type': sig_type,
                'sig_price': sig_price,
                'sig_dt': sig_dt,
            }
        except Exception as e:
            logger.warning("%s 级别分析失败: %s", level, e)
            return None

    def analyze_all_levels(self) -> Dict[str, LevelSignal]:
        """同时分析三个级别。"""
        configs = [
            ('30min', '30', 120),
            ('5min', '5', 1000),
            ('1min', '1', 2000),
        ]
        results = {}
        for level, period, klines in configs:
            r = self._fetch_and_analyze(level, period, klines)
            if r:
                results[level] = LevelSignal(
                    level=level,
                    trend=r['trend'],
                    latest_signal_type=r['sig_type'],
                    latest_signal_price=r['sig_price'],
                    latest_signal_dt=r['sig_dt'],
                    zhongshu_low=r['zhongshu_low'],
                    zhongshu_high=r['zhongshu_high'],
                    current_price=r['current_price'],
                    summary=r['summary'],
                )
        return results

    def _judge(self, levels: Dict[str, LevelSignal]) -> TradeAdvice:
        """区间套核心判断逻辑。"""
        m30 = levels.get('30min')
        m5 = levels.get('5min')
        m1 = levels.get('1min')

        if not m30 or not m5:
            return TradeAdvice(
                action="数据不足",
                confidence=0.0,
                target_zone=None,
                stop_loss=None,
                reasoning="30分钟或5分钟数据缺失，无法判断",
                risk_level="高",
            )

        big_direction = self._signal_direction(m30.latest_signal_type, trend=m30.trend)

        if big_direction == "卖":
            small_buy = self._has_buy_signal(m5, m1)
            if small_buy:
                return TradeAdvice(
                    action="等待",
                    confidence=0.7,
                    target_zone=None,
                    stop_loss=None,
                    reasoning=(
                        f"30分钟级别 {m30.latest_signal_type}@{m30.latest_signal_price:.2f} 有效，"
                        f"大方向偏空。{small_buy}级别出现买点，但只能视为反弹，不可重仓。"
                        f"等待30分钟或5分钟底背驰确认后再买回。"
                    ),
                    risk_level="高",
                )
            else:
                return TradeAdvice(
                    action="等待/做空",
                    confidence=0.8,
                    target_zone=(m30.zhongshu_low, m30.zhongshu_high) if m30.zhongshu_low else None,
                    stop_loss=m30.latest_signal_price * 1.03 if m30.latest_signal_price else None,
                    reasoning=(
                        f"30分钟 {m30.latest_signal_type}@{m30.latest_signal_price:.2f} 有效，"
                        f"5分钟/1分钟均无买点，共振下跌中。目标 {m30.zhongshu_low or '更低位置'}。"
                    ),
                    risk_level="中",
                )

        if big_direction == "买":
            return TradeAdvice(
                action="重仓做多",
                confidence=0.85,
                target_zone=(m30.zhongshu_high, m30.zhongshu_high * 1.05) if m30.zhongshu_high else None,
                stop_loss=m30.latest_signal_price * 0.97 if m30.latest_signal_price else None,
                reasoning=(
                    f"30分钟 {m30.latest_signal_type}@{m30.latest_signal_price:.2f} 确认，"
                    f"大方向转多。可重仓，止损设于一买下方3%。"
                ),
                risk_level="低",
            )

        if self._signal_direction(m5.latest_signal_type, trend=m5.trend) == "卖":
            return TradeAdvice(
                action="减仓观望",
                confidence=0.6,
                target_zone=None,
                stop_loss=None,
                reasoning=(
                    f"30分钟无明确信号，5分钟 {m5.latest_signal_type}@{m5.latest_signal_price:.2f} 出现，"
                    f"中级别偏空，建议减仓或观望。"
                ),
                risk_level="中",
            )

        if self._signal_direction(m5.latest_signal_type, trend=m5.trend) == "买":
            return TradeAdvice(
                action="轻仓试多",
                confidence=0.6,
                target_zone=(m5.zhongshu_high, m5.zhongshu_high * 1.03) if m5.zhongshu_high else None,
                stop_loss=m5.latest_signal_price * 0.97 if m5.latest_signal_price else None,
                reasoning=(
                    f"30分钟无明确信号，5分钟 {m5.latest_signal_type}@{m5.latest_signal_price:.2f} 出现，"
                    f"可轻仓试多，严格止损。"
                ),
                risk_level="中",
            )

        return TradeAdvice(
            action="等待",
            confidence=0.5,
            target_zone=None,
            stop_loss=None,
            reasoning="多级别均无明确买卖点，趋势不明，观望为主。",
            risk_level="中",
        )

    @staticmethod
    def _signal_direction(sig_type: str, trend: str = "") -> str:
        if '买' in sig_type:
            return "买"
        if '卖' in sig_type:
            return "卖"
        if '背驰' in trend:
            if '上涨背驰' in trend:
                return "卖"
            if '下跌背驰' in trend:
                return "买"
        return "无"

    @staticmethod
    def _has_buy_signal(m5: Optional[LevelSignal], m1: Optional[LevelSignal]) -> str:
        if m5 and '买' in m5.latest_signal_type:
            return "5分钟"
        if m1 and '买' in m1.latest_signal_type:
            return "1分钟"
        if m1 and '背驰' in m1.summary and '底' in m1.summary:
            return "1分钟（底背驰）"
        return ""

    def run(self) -> Dict[str, str]:
        """运行完整分析并返回可读的文本/HTML。"""
        strategy = IntervalStrategy(self.code)
        return strategy.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = ChanlunStrategy("002463")
    result = s.run()
    print(result['text'])