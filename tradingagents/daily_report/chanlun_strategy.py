"""
区间套交易策略引擎

同时监控多级别（30min/5min/1min）缠论信号，根据"大级别定方向、小级别找买卖点"
原则输出操作建议。

用法:
    from tradingagents.daily_report.chanlun_strategy import ChanlunStrategy
    s = ChanlunStrategy('002463')
    advice = s.run()
    print(advice['text'])
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import akshare as ak
import pandas as pd

from .chanlun_adapter import _get_kline_akshare  # noqa: E402

logger = logging.getLogger(__name__)

# 缠论引擎延迟导入，避免初始化时拉重依赖


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
    action: str           # "等待" / "轻仓试多" / "重仓做多" / "减仓观望" / "做空"
    confidence: float     # 0~1
    target_zone: Optional[Tuple[float, float]]  # 目标价格区间
    stop_loss: Optional[float]
    reasoning: str
    risk_level: str       # "低" / "中" / "高"


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
        self._engine = None  # 延迟初始化

    def _engine_instance(self):
        if self._engine is None:
            import sys
            from pathlib import Path
            _root = Path(__file__).resolve().parent.parent.parent.parent / "stock-chanlun" / "backend"
            if not _root.exists():
                _root = Path("/workspace/stock-chanlun/backend").resolve()
            _path = str(_root)
            if _path not in sys.path:
                sys.path.insert(0, _path)
            from chanlun.engine import ChanlunEngine
            self._engine_class = ChanlunEngine
        return self._engine_class

    def _fetch_and_analyze(self, level: str, period: str, klines: int) -> Optional[Dict]:
        """拉取K线并跑缠论分析，返回简化结果。"""
        try:
            if level == 'daily':
                df = _get_kline_akshare(self.code, days=klines)
            else:
                prefix = 'sh' if self.code.startswith(('6', '9', '688')) else 'sz'
                df = ak.stock_zh_a_minute(symbol=f'{prefix}{self.code}', period=period)
                df.rename(columns={'day': 'date'}, inplace=True)
                df['date'] = pd.to_datetime(df['date'])
                for col in ['open', 'high', 'low', 'close', 'volume']:
                    df[col] = pd.to_numeric(df[col], errors='coerce')
                df = df.tail(klines).copy()

            if df is None or df.empty or len(df) < 30:
                return None

            engine = self._engine_instance()(df)
            result = engine.analyze(level=level)

            # 提取最近一个信号
            latest_sig = None
            if result.signals:
                latest_sig = max(result.signals, key=lambda s: s.datetime)

            zs_low = zs_high = None
            if result.zhongshus:
                zs = result.zhongshus[-1]
                zs_low, zs_high = zs.range_low, zs.range_high

            return {
                'level': level,
                'trend': result.trend,
                'latest_signal': latest_sig,
                'zhongshu_low': zs_low,
                'zhongshu_high': zs_high,
                'current_price': float(df['close'].iloc[-1]),
                'summary': result.summary,
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
                sig = r['latest_signal']
                results[level] = LevelSignal(
                    level=level,
                    trend=r['trend'],
                    latest_signal_type=sig.type if sig else "无",
                    latest_signal_price=sig.price if sig else 0.0,
                    latest_signal_dt=(sig.datetime.strftime('%m-%d %H:%M')
                                      if sig and hasattr(sig.datetime, 'strftime')
                                      else str(sig.datetime) if sig else ""),
                    zhongshu_low=r['zhongshu_low'],
                    zhongshu_high=r['zhongshu_high'],
                    current_price=r['current_price'],
                    summary=r['summary'],
                )
        return results

    def _judge(self, levels: Dict[str, LevelSignal]) -> TradeAdvice:
        """
        区间套核心判断逻辑。
        """
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

        # 提取大级别方向（结合信号类型 + 趋势判定）
        big_direction = self._signal_direction(m30.latest_signal_type, trend=m30.trend)

        # 情形1: 大级别卖点有效（一卖/二卖/三卖）
        if big_direction == "卖":
            # 检查小级别是否有买点
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

        # 情形2: 大级别买点有效（一买/二买/三买）
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

        # 情形3: 大级别无明确信号，中级别有卖点
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

        # 情形4: 大级别无明确信号，中级别有买点
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

        # 情形5: 全部无信号
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
        """根据信号类型 + 趋势综合判断方向。"""
        if '买' in sig_type:
            return "买"
        if '卖' in sig_type:
            return "卖"
        # 无明确买卖信号时，靠趋势辅助判断
        if '背驰' in trend:
            if '上涨背驰' in trend:
                return "卖"  # 上涨背驰 → 等同于卖方向
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
        levels = self.analyze_all_levels()
        advice = self._judge(levels)

        # 获取任意级别的当前价（避免全空时报错）
        current_price = next((lv.current_price for lv in levels.values() if lv), 0.0)

        text_lines = [
            f"=== {self.code} 区间套交易策略 ===",
            f"当前价: {current_price:.2f}",
            "",
            "【各级别状态】",
        ]
        for lv_name in ['30min', '5min', '1min']:
            lv = levels.get(lv_name)
            if lv:
                zs = f" 中枢[{lv.zhongshu_low:.2f}, {lv.zhongshu_high:.2f}]" if lv.zhongshu_low else ""
                sig = f" {lv.latest_signal_type}@{lv.latest_signal_price:.2f}" if lv.latest_signal_type != "无" else " 无信号"
                text_lines.append(f"  {lv_name}: {lv.trend}{sig} ({lv.latest_signal_dt}){zs}")
            else:
                text_lines.append(f"  {lv_name}: 数据缺失")

        text_lines.extend([
            "",
            "【交易建议】",
            f"  操作: {advice.action}",
            f"  置信度: {advice.confidence:.0%}",
            f"  风险: {advice.risk_level}",
        ])
        if advice.target_zone:
            text_lines.append(f"  目标区间: [{advice.target_zone[0]:.2f}, {advice.target_zone[1]:.2f}]")
        if advice.stop_loss:
            text_lines.append(f"  止损: {advice.stop_loss:.2f}")
        text_lines.extend([
            "",
            "【逻辑】",
            f"  {advice.reasoning}",
        ])

        return {
            'text': "\n".join(text_lines),
            'levels': {k: v.__dict__ for k, v in levels.items()},
            'advice': advice.__dict__,
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s = ChanlunStrategy("002463")
    result = s.run()
    print(result['text'])
