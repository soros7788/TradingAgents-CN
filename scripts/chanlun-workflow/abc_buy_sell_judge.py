"""
ABC 买卖点补充完全分类

理论背景
--------
缠论传统一二三买卖点需要一个重要前提：走势是"两个中枢及以上"的趋势。
但实际市场中大量反转发生在单中枢盘整背驰之后，传统分类无法完全覆盖。

ABC 补充分类:
  A类反弹买点: 单中枢盘整背驰后首次反弹确认
  B类确认买点: A类低点不被跌破 → 确认反弹有效
  C类反转确认: 中阴化解 → 趋势向上确认

与传统分类映射:
  趋势背驰一买(双中枢) → D类 (保留原有, 不改动)
  盘整背驰一买(单中枢) → A类 (新增)
  二买(回踩不破一买低点) → B类 (A类场景下的二买)
  三买(突破回踩不破中枢) → C类 (A类场景下的三买)

作者: 5.5 (2026-08-09)
"""
from typing import Dict, Optional
import pandas as pd


def evaluate_a_class_buy(
    single_center: Dict[str, float],
    daily_signal: Dict,
    min30_dir: str,
    min30_ep_p: float,
    min30_has_reversal: bool,
    min5_entry_ok: bool,
) -> Dict:
    """
    A类反弹买点判定。

    条件:
      1. 日线存在盘整背驰信号 (sig_type=盘整背驰, has_downtrend=False)
      2. 30min 趋势已反转向上 (overall_dir=up 或 reversal_by_trend)
      3. 30min EP_L >= 0.3 (反转概率足够)
      4. 5min 有入场确认信号

    参数:
      single_center: 单中枢 {zg, zd, s, e}
      daily_signal: 日线盘整背驰信号
      min30_dir: 30min 整体方向
      min30_ep_p: 30min 反转概率
      min30_has_reversal: 30min 是否趋势反转(by trend)
      min5_entry_ok: 5min 入场确认

    返回:
      {
        'is_confirmed': bool,
        'class_type': 'A',
        'center': {zg, zd},
        'a_low': float,        # A类低点 = 中枢低点ZD
        'ep_prob': float,
        'reason': str,
      }
    """
    result = {
        'is_confirmed': False,
        'class_type': 'A',
        'center': single_center,
        'a_low': single_center.get('zd', 0),
        'ep_prob': min30_ep_p,
        'reason': '',
    }

    # 条件1: 盘整背驰信号
    sig_type = daily_signal.get('type', '')
    has_downtrend = daily_signal.get('has_downtrend', True)
    valid = daily_signal.get('valid', False)
    is_pan_bei = (sig_type == '盘整背驰') or (not has_downtrend)

    if not valid or not is_pan_bei:
        result['reason'] = f'A类拒绝: 非盘整背驰(type={sig_type}, downtrend={has_downtrend})'
        return result

    # 条件2: 30min 趋势反转
    dir_ok = min30_dir == 'up' or min30_has_reversal
    if not dir_ok:
        result['reason'] = f'A类拒绝: 30min方向未反转(dir={min30_dir})'
        return result

    # 条件3: EP_L 反转概率
    if min30_ep_p < 0.3:
        result['reason'] = f'A类拒绝: EP_L={min30_ep_p:.3f} < 0.3'
        return result

    # 条件4: 5min 入场确认(软条件, 仅警告)
    if not min5_entry_ok:
        result['reason'] = (f'A类确认(5min未入场): 盘整背驰+30min反转+EP_L={min30_ep_p:.3f} '
                           f'| 5min无入场信号, 风险偏大')
        result['is_confirmed'] = True
        result['warning'] = '5min未入场'
        return result

    result['is_confirmed'] = True
    result['reason'] = (f'A类反弹确认: 盘整背驰+30min反转(dir={min30_dir}) '
                       f'EP_L={min30_ep_p:.3f} 5min入场确认 '
                       f'A低点={result["a_low"]:.2f}')
    return result


def evaluate_b_class_buy(
    a_low: float,
    pullback_low: float,
    pullback_klines: int,
    cushion_ratio: float = 0.005,
    min_klines: int = 3,
) -> Dict:
    """
    B类确认买点判定。

    条件:
      1. A类低点已确认 (a_low > 0)
      2. 回踩段最低点 > A类低点 (不破A低)
      3. 回踩段至少有3根K线(结构完整性)
      4. 缓冲垫比率 >= 0.5%

    参数:
      a_low: A类低点(中枢ZD)
      pullback_low: 回踩段最低点
      pullback_klines: 回踩段K线数
      cushion_ratio: 缓冲垫比率
      min_klines: 最小K线数

    返回:
      {
        'is_confirmed': bool,
        'class_type': 'B',
        'a_low': float,
        'b_low': float,
        'cushion': float,
        'cushion_ratio': float,
        'reason': str,
      }
    """
    result = {
        'is_confirmed': False,
        'class_type': 'B',
        'a_low': a_low,
        'b_low': pullback_low,
        'cushion': 0.0,
        'cushion_ratio': 0.0,
        'reason': '',
    }

    if a_low <= 0:
        result['reason'] = 'B类拒绝: A类低点无效'
        return result

    cushion = pullback_low - a_low
    cr = cushion / a_low if a_low > 0 else 0.0
    result['cushion'] = round(cushion, 4)
    result['cushion_ratio'] = round(cr, 4)

    # 条件1: 回踩不破A低
    if pullback_low <= a_low:
        result['reason'] = (f'B类拒绝: 回踩低点({pullback_low:.2f}) <= A低点({a_low:.2f}) '
                           f'缓冲垫={cushion:.4f}')
        return result

    # 条件2: 结构完整性
    if pullback_klines < min_klines:
        result['reason'] = (f'B类拒绝: 回踩K线({pullback_klines}) < {min_klines} '
                           f'缓冲垫={cushion:.4f}')
        return result

    # 条件3: 缓冲垫
    if cr < cushion_ratio:
        result['reason'] = (f'B类拒绝: 缓冲垫比率={cr:.4f} < {cushion_ratio} '
                           f'太薄易破位')
        return result

    result['is_confirmed'] = True
    result['reason'] = (f'B类确认: 回踩低点({pullback_low:.2f}) > A低点({a_low:.2f}) '
                       f'缓冲垫={cushion:.4f}({cr:.2%}) K线={pullback_klines}')
    return result


def evaluate_c_class_buy(
    b_low: float,
    center_zg: float,
    h_leave: float,
    pullback_low: float,
    pullback_klines: int,
    min30_has_sell_conflict: bool = False,
) -> Dict:
    """
    C类反转确认买点判定。

    条件:
      1. B类确认后价格突破前高/中枢上沿
      2. 回踩不破B类低点(或中枢ZG)
      3. 30min无强卖点冲突

    参数:
      b_low: B类低点
      center_zg: 中枢上沿ZG
      h_leave: 离开段最高点
      pullback_low: 回踩段最低点
      pullback_klines: 回踩段K线数
      min30_has_sell_conflict: 30min是否有强卖点

    返回:
      {
        'is_confirmed': bool,
        'class_type': 'C',
        'b_low': float,
        'invalid_boundary': float,
        'cushion': float,
        'reason': str,
      }
    """
    result = {
        'is_confirmed': False,
        'class_type': 'C',
        'b_low': b_low,
        'invalid_boundary': max(b_low, center_zg),
        'cushion': 0.0,
        'reason': '',
    }

    invalid = max(b_low, center_zg)

    # 条件1: 有离开段(突破)
    if h_leave <= center_zg:
        result['reason'] = f'C类拒绝: 离开段未突破ZG({center_zg:.2f}) h_leave={h_leave:.2f}'
        return result

    # 条件2: 回踩不破失效边界
    if pullback_low <= invalid:
        cushion = pullback_low - invalid
        result['cushion'] = round(cushion, 4)
        result['reason'] = (f'C类拒绝: 回踩({pullback_low:.2f}) <= 失效边界({invalid:.2f}) '
                           f'缓冲垫={cushion:.4f}')
        return result

    # 条件3: 结构完整性
    if pullback_klines < 3:
        result['reason'] = f'C类拒绝: 回踩K线({pullback_klines}) < 3'
        return result

    # 条件4: 卖点冲突(软条件)
    if min30_has_sell_conflict:
        result['reason'] = (f'C类拒绝: 30min有强卖点, 不追高')
        return result

    cushion = pullback_low - invalid
    result['cushion'] = round(cushion, 4)
    result['is_confirmed'] = True
    result['reason'] = (f'C类反转确认: 突破ZG({center_zg:.2f}) H_leave={h_leave:.2f} '
                       f'回踩不破({pullback_low:.2f}) 缓冲垫={cushion:.4f}')
    return result