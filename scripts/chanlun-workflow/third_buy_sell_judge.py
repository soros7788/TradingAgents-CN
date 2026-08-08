#!/usr/bin/env python3
"""
第三类买卖点（三买/三卖）确认与买卖区间几何判定

独立模块，无外部依赖，仅需 pandas。
集成自 Gemini 笔记本 (2026-08-06)

用法:
    from third_buy_sell_judge import evaluate_third_buy_sell
    result = evaluate_third_buy_sell(
        center={"zg": 10.0, "zd": 9.0},
        leave_segment=pullback_df,  # pd.DataFrame 需含 high/low 列
        pullback_segment=pullback_df,
        signal_type="third_buy",    # 或 "third_sell"
        sub_level_resonance=True    # 次级别是否共振确认
    )
"""
from typing import Dict, Optional
import pandas as pd


def evaluate_third_buy_sell(
    center: Dict[str, float],
    leave_segment: pd.DataFrame,
    pullback_segment: pd.DataFrame,
    signal_type: str = 'third_buy',
    sub_level_resonance: bool = True
) -> Dict:
    """
    第三类买卖点严格几何判定。

    参数
    ----
    center : dict
        中枢区间，需包含键 'zg'（中枢高点）和 'zd'（中枢低点）。
    leave_segment : pd.DataFrame
        离开段K线数据，需包含 'high'/'low' 列。
    pullback_segment : pd.DataFrame
        回踩/反弹段K线数据，需包含 'high'/'low' 列。
    signal_type : str
        'third_buy' 或 'third_sell'。
    sub_level_resonance : bool
        次级别是否确认共振（走势必完美）。

    返回
    ----
    dict : {
        'is_confirmed': bool,
        'status': str,
        'invalid_boundary': float | None,
        'optimal_buy_min': float | None,
        'optimal_buy_max': float | None,
        'optimal_sell_min': float | None,
        'optimal_sell_max': float | None,
        'cushion': float,
        'cushion_ratio': float,
        'reason': str
    }
    """
    zg = center['zg']
    zd = center['zd']

    h_leave = float(leave_segment['high'].max())
    l_leave = float(leave_segment['low'].min())
    l_pullback = float(pullback_segment['low'].min())
    h_rebound = float(pullback_segment['high'].max())

    # 次级别走势完备性检查
    is_structurally_complete = len(pullback_segment) >= 3
    is_sub_level_perfect = is_structurally_complete and sub_level_resonance

    result = {
        'is_confirmed': False,
        'status': 'UNKNOWN',
        'invalid_boundary': None,
        'optimal_buy_min': None,
        'optimal_buy_max': None,
        'optimal_sell_min': None,
        'optimal_sell_max': None,
        'cushion': 0.0,
        'cushion_ratio': 0.0,
        'reason': '',
    }

    if signal_type == 'third_buy':
        cushion = round(l_pullback - zg, 4)

        if l_pullback > zg:
            result['is_confirmed'] = True
            result['status'] = 'CONFIRMED_THIRD_BUY'
            result['invalid_boundary'] = zg
            result['optimal_buy_min'] = round(zg + 0.01, 2)
            result['optimal_buy_max'] = round(zg + (h_leave - zg) * 0.382, 2)
            result['cushion'] = cushion
            result['cushion_ratio'] = round(cushion / zg, 4) if zg != 0 else 0.0
            result['reason'] = (
                f"三买确认成功: 回踩低点 ({l_pullback}) > 中枢高点 ZG ({zg})，"
                f"安全缓冲垫为 {cushion}"
            )
            if not is_sub_level_perfect:
                result['reason'] += " (次级别走势未完备，风险偏大)"
        elif zg >= l_pullback >= zd:
            result['status'] = 'REJECTED_CENTER_EXPANSION'
            result['invalid_boundary'] = zg
            result['reason'] = (
                f"三买确认失败: 回踩低点 ({l_pullback}) 进入中枢高低点区间 "
                f"[{zd}, {zg}]，转化为中枢震荡/扩展"
            )
        else:
            result['status'] = 'REJECTED_CENTER_REENTRY'
            result['invalid_boundary'] = zg
            result['reason'] = (
                f"三买确认失败: 回踩低点 ({l_pullback}) 跌破中枢低点 ZD ({zd})，"
                f"重回中枢内部"
            )

    elif signal_type == 'third_sell':
        cushion = round(zd - h_rebound, 4)

        if h_rebound < zd:
            result['is_confirmed'] = True
            result['status'] = 'CONFIRMED_THIRD_SELL'
            result['invalid_boundary'] = zd
            result['optimal_sell_min'] = round(zd - (zd - l_leave) * 0.382, 2)
            result['optimal_sell_max'] = round(zd - 0.01, 2)
            result['cushion'] = cushion
            result['cushion_ratio'] = round(cushion / zd, 4) if zd != 0 else 0.0
            result['reason'] = (
                f"三卖确认成功: 反弹高点 ({h_rebound}) < 中枢低点 ZD ({zd})，"
                f"安全缓冲垫为 {cushion}"
            )
            if not is_sub_level_perfect:
                result['reason'] += " (次级别走势未完备，风险偏大)"
        elif zd <= h_rebound <= zg:
                    result['status'] = 'REJECTED_CENTER_EXPANSION'
                    result['invalid_boundary'] = zd
                    result['reason'] = (
                        f"三卖确认失败: 反弹高点 ({h_rebound}) 进入中枢高低点区间 "
                        f"[{zd}, {zg}]，转化为中枢震荡/扩展"
                    )
        else:
            result['status'] = 'REJECTED_CENTER_REENTRY'
            result['invalid_boundary'] = zd
            result['reason'] = (
                f"三卖确认失败: 反弹高点 ({h_rebound}) 向上突破中枢高点 ZG ({zg})，"
                f"重回中枢内部"
            )

    return result