#!/usr/bin/env python3
"""
缠论背驰分析器 — 修复版

BUG修复记录:
1. 1min级别在大上涨趋势中误判"一买"
   - 根因: 前段仅15根K线，日内微小回调被当成有效下跌段
   - 修复: 前段最小30根 + 幅度>=0.1% + 大级别方向过滤
   - 日期: 2026-07-14

2. sklearn沙箱常驻BUG (2026-07-28)
   - 根因: TRAE沙箱每次新session是干净环境, pip install不持久
   - 症状: 模型加载失败→回退硬编码→全市场无DL_P>0.8→触发用户规则
   - 修复: import时自动检测并安装scikit-learn==1.7.2
"""

import urllib.request
import ssl
import json
import re
import os
import pickle
import numpy as np

# ============================================================
#  sklearn自动安装 (沙箱环境兼容)
#  TRAE沙箱每次新session不保留pip安装, 需自动补装
# ============================================================
try:
    import sklearn
except ImportError:
    import subprocess, sys
    print("[依赖] sklearn未安装, 正在自动安装 scikit-learn==1.7.2 ...")
    subprocess.check_call([
        sys.executable, "-m", "pip", "install",
        "scikit-learn==1.7.2", "--break-system-packages", "-q"
    ])
    print("[依赖] scikit-learn==1.7.2 安装完成")
    import sklearn

import pandas as pd
from third_buy_sell_judge import evaluate_third_buy_sell

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

# ============================================================
#  深度学习模型加载
#  MLP(128→64→32→16) 训练于11373样本, AUC=0.72
#  替换硬编码 ratio<60 趋势背驰 / ratio<85 盘整背驰
# ============================================================
_dl_model = None
_dl_scaler = None
_dl_loaded = False
_DL_DIR = os.path.dirname(os.path.abspath(__file__))
_DL_MODEL_PATH = os.path.join(_DL_DIR, "dl_model.pkl")
_DL_SCALER_PATH = os.path.join(_DL_DIR, "dl_scaler.pkl")
_DL_TREND_P = 0.6    # P>=0.6 → 趋势背驰
_DL_PAN_P = 0.4       # P>=0.4 → 盘整背驰
_DL_FEATURE_NAMES = [
    "ratio", "pre_pct", "post_pct", "pre_bars_norm", "post_bars_norm",
    "pre_consistency", "post_consistency", "zs_width_pct", "atr_norm",
    "volume_ratio", "dif_slope", "overall_pct", "price_vs_zs",
    "macd_bar_peak", "level_code", "bar_converge",
]

# ============================================================
#  反转概率模型 EP_L (V2)
#  MLP(128→64→32→16) 训练于1724样本, AUC=0.68
#  20维精选特征 (36维→SelectKBest)
#  预测背驰信号后是否发生有效反转
# ============================================================
_ep_model = None
_ep_scaler = None
_ep_meta = None
_ep_loaded = False
_EP_MODEL_PATH = os.path.join(_DL_DIR, "ep_model.pkl")
_EP_SCALER_PATH = os.path.join(_DL_DIR, "ep_scaler.pkl")
_EP_META_PATH = os.path.join(_DL_DIR, "ep_train_meta.pkl")
_EP_TREND_P = 0.6   # P>=0.6 → 高反转概率
_EP_WATCH_P = 0.4    # P>=0.4 → 观察区间


def _load_ep_model():
    """加载EP_L反转概率模型(懒加载)

    BUG修复 (2026-07-28): 同_load_dl_model, _loaded=True在try前设置导致永不重试
    修复: 仅在成功或永久性错误时设_loaded=True
    """
    global _ep_model, _ep_scaler, _ep_meta, _ep_loaded
    if _ep_loaded:
        return _ep_model is not None
    try:
        if not os.path.exists(_EP_MODEL_PATH):
            print(f"[EP模型] 模型文件不存在: {_EP_MODEL_PATH}")
            _ep_loaded = True  # 永久性错误, 不重试
            return False
        if not os.path.exists(_EP_SCALER_PATH):
            print(f"[EP模型] Scaler文件不存在: {_EP_SCALER_PATH}")
            _ep_loaded = True  # 永久性错误, 不重试
            return False
        with open(_EP_MODEL_PATH, 'rb') as f:
            _ep_model = pickle.load(f)
        with open(_EP_SCALER_PATH, 'rb') as f:
            _ep_scaler = pickle.load(f)
        if os.path.exists(_EP_META_PATH):
            with open(_EP_META_PATH, 'rb') as f:
                _ep_meta = pickle.load(f)
        _ep_loaded = True  # 成功加载
        print(f"[EP模型] 加载成功: model={type(_ep_model).__name__}, "
              f"meta_version={_ep_meta.get('version', 'N/A') if _ep_meta else 'N/A'}")
        return True
    except ImportError as e:
        print(f"[EP模型] 依赖缺失(保留重试): {e}")
        return False  # _loaded保持False, 下次调用可重试
    except Exception as e:
        print(f"[EP模型] 加载失败(保留重试): {type(e).__name__}: {e}")
        return False  # _loaded保持False, 下次调用可重试


def _compute_ep_features(ratio, pre_pct, post_pct, pre_bars, post_bars,
                          closes, highs, lows, opens, volumes,
                          pre_s, pre_e, post_s, post_e, dif, bar,
                          zs, atr, level_name, V):
    """计算EP_L 36维完整特征向量 (信号点为post_e)"""
    n = len(closes)
    sig_idx = post_e
    sig_price = closes[sig_idx]

    # ====== DL_P 16维 (复用) ======
    f1 = ratio
    f2 = pre_pct
    f3 = post_pct
    f4 = pre_bars / 50.0
    f5 = post_bars / 50.0
    f6 = _compute_consistency(closes, pre_s, pre_e)
    f7 = _compute_consistency(closes, post_s, post_e)
    zs_price = closes[zs['s']]
    f8 = (zs['zg'] - zs['zd']) / zs_price * 100 if zs_price > 0 else 0
    f9 = atr / zs_price if zs_price > 0 else 0
    pre_vol = sum(V[pre_s:pre_e + 1]) / max(1, pre_e - pre_s + 1) if V and pre_e >= pre_s else 1
    post_vol = sum(V[post_s:post_e + 1]) / max(1, post_e - post_s + 1) if V and post_e >= post_s else 1
    f10 = post_vol / pre_vol if pre_vol > 0 else 1.0
    f11 = _compute_dif_slope(dif, sig_idx)
    lookback = min(60, n)
    f12 = abs(closes[-1] - closes[n - lookback]) / closes[n - lookback] * 100 if n > lookback else 0
    zs_mid = (zs['zg'] + zs['zd']) / 2
    zs_half = (zs['zg'] - zs['zd']) / 2 if (zs['zg'] - zs['zd']) > 0 else 1
    f13 = max(-1.0, min(1.0, (closes[sig_idx] - zs_mid) / zs_half))
    post_bars_list = bar[post_s:post_e + 1] if post_e >= post_s else [0]
    f14 = max(abs(b) for b in post_bars_list) / (atr + 1e-10)
    level_code_map = {"日线": 0, "30min": 1, "5min": 2, "1min": 2}
    f15 = float(level_code_map.get(level_name, 0))
    f16 = _compute_bar_converge(bar, post_s, post_e)

    # ====== EP专属 20维 ======
    # F17: RSI14
    f17 = _calc_rsi(closes[:sig_idx + 1], 14)
    # F18-F20: KDJ
    f18, f19, f20 = _calc_kdj(highs[:sig_idx + 1], lows[:sig_idx + 1], closes[:sig_idx + 1])
    # F21: price_vs_ma5
    ma5 = _calc_ma(closes[:sig_idx + 1], 5)
    f21 = (sig_price - ma5) / ma5 * 100 if ma5 > 0 else 0
    # F22: price_vs_ma20
    ma20 = _calc_ma(closes[:sig_idx + 1], 20)
    f22 = (sig_price - ma20) / ma20 * 100 if ma20 > 0 else 0
    # F23: macd_bar_shrink
    if sig_idx >= 3:
        br = [abs(bar[sig_idx]), abs(bar[sig_idx - 1]), abs(bar[sig_idx - 2])]
        f23 = 1.0 if br[0] < br[1] < br[2] else 0.0
    else:
        f23 = 0.0
    # F24: max_drawdown_10
    dd_s = max(0, sig_idx - 10)
    dd_h = max(closes[dd_s:sig_idx + 1])
    f24 = (dd_h - min(closes[dd_s:sig_idx + 1])) / dd_h * 100 if dd_h > 0 else 0
    # F25: volume_trend_slope
    vw = min(10, sig_idx)
    if vw >= 3 and volumes:
        vy = np.array(volumes[sig_idx - vw + 1:sig_idx + 1], dtype=float)
        vx = np.arange(len(vy), dtype=float)
        vxm, vym = vx.mean(), vy.mean()
        f25 = np.sum((vx - vxm) * (vy - vym)) / (np.sum((vx - vxm) ** 2) + 1e-10) / (vym + 1e-10)
    else:
        f25 = 0.0
    # F26: lower_shadow_ratio
    sig_total = highs[sig_idx] - lows[sig_idx]
    f26 = (min(opens[sig_idx], closes[sig_idx]) - lows[sig_idx]) / sig_total if sig_total > 0 else 0.0
    # F27: consecutive_down_days
    cdd = 0
    for i in range(sig_idx, max(sig_idx - 20, 0), -1):
        if i > 0 and closes[i] < closes[i - 1]:
            cdd += 1
        else:
            break
    f27 = cdd / 20.0
    # F28: bollinger_position
    bb_window = min(20, sig_idx)
    if bb_window >= 5:
        bb_closes = closes[sig_idx - bb_window + 1:sig_idx + 1]
        bb_mean = np.mean(bb_closes)
        bb_std = np.std(bb_closes)
        if bb_std > 0:
            f28 = (sig_price - (bb_mean - 2 * bb_std)) / (4 * bb_std)
            f28 = max(0.0, min(1.0, f28))
        else:
            f28 = 0.5
    else:
        f28 = 0.5
    # F29: bottom_volume_ratio
    avg_vol_20 = np.mean(volumes[max(0, sig_idx - 19):sig_idx + 1]) if volumes and sig_idx >= 19 else (np.mean(volumes[:sig_idx + 1]) if volumes else 1)
    f29 = volumes[sig_idx] / avg_vol_20 if avg_vol_20 > 0 and volumes else 1.0
    # F30: dif_cross_dea
    if sig_idx < len(dif):
        dea = _calc_dea(dif)
        if sig_idx < len(dea):
            f30 = (dif[sig_idx] - dea[sig_idx]) / (atr + 1e-10) if atr > 0 else 0
        else:
            f30 = 0.0
    else:
        f30 = 0.0
    # F31: retracement_from_high
    rh_start = max(0, sig_idx - 60)
    recent_high = max(highs[rh_start:sig_idx + 1])
    f31 = (recent_high - sig_price) / recent_high * 100 if recent_high > 0 else 0
    # F32: candle_pattern
    body = abs(opens[sig_idx] - closes[sig_idx])
    upper_shadow = highs[sig_idx] - max(opens[sig_idx], closes[sig_idx])
    lower_shadow = min(opens[sig_idx], closes[sig_idx]) - lows[sig_idx]
    if sig_total > 0:
        if lower_shadow > body * 2 and upper_shadow < body * 0.5:
            f32 = 1.0
        elif body < sig_total * 0.1:
            f32 = 0.8
        elif lower_shadow > body and upper_shadow < lower_shadow:
            f32 = 0.6
        else:
            f32 = 0.0
    else:
        f32 = 0.0
    # F33: volume_price_diverge
    if sig_idx >= 10 and volumes:
        recent_5_lows = min(lows[sig_idx - 4:sig_idx + 1])
        prev_5_lows = min(lows[max(0, sig_idx - 9):sig_idx - 4])
        recent_5_vol = np.mean(volumes[sig_idx - 4:sig_idx + 1])
        prev_5_vol = np.mean(volumes[max(0, sig_idx - 9):sig_idx - 4])
        if recent_5_lows < prev_5_lows and prev_5_vol > 0:
            f33 = prev_5_vol / recent_5_vol
        else:
            f33 = 1.0
    else:
        f33 = 1.0
    # F34: momentum_5d
    f34 = (closes[sig_idx] - closes[sig_idx - 5]) / closes[sig_idx - 5] * 100 if sig_idx >= 5 else 0.0
    # F35: volatility_squeeze
    if sig_idx >= 20:
        vol_short = np.std([closes[i] / closes[i - 1] - 1 for i in range(sig_idx - 4, sig_idx + 1)])
        vol_long = np.std([closes[i] / closes[i - 1] - 1 for i in range(sig_idx - 19, sig_idx + 1)])
        f35 = vol_short / vol_long if vol_long > 0 else 1.0
    else:
        f35 = 1.0
    # F36: support_distance
    support_start = max(0, sig_idx - 40)
    recent_low = min(lows[support_start:sig_idx + 1])
    f36 = (sig_price - recent_low) / recent_low * 100 if recent_low > 0 else 0

    return [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13, f14, f15, f16,
            f17, f18, f19, f20, f21, f22, f23, f24, f25, f26, f27, f28, f29, f30,
            f31, f32, f33, f34, f35, f36]


def _calc_rsi(closes, period=14):
    if len(closes) < period + 1:
        return 50.0
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(c, 0) for c in changes[-period:]]
    losses = [max(-c, 0) for c in changes[-period:]]
    ag, al = sum(gains) / period, sum(losses) / period
    if al < 1e-10:
        return 100.0
    return 100.0 - 100.0 / (1 + ag / al)


def _calc_kdj(highs, lows, closes, period=9):
    if len(closes) < period:
        return 50.0, 50.0, 50.0
    hh, ll = max(highs[-period:]), min(lows[-period:])
    if hh == ll:
        return 50.0, 50.0, 50.0
    rsv = (closes[-1] - ll) / (hh - ll) * 100
    k = 2 / 3 * 50 + 1 / 3 * rsv
    d = 2 / 3 * 50 + 1 / 3 * k
    return k, d, 3 * k - 2 * d


def _calc_ma(closes, period):
    if len(closes) < period:
        return closes[-1] if closes else 0
    return sum(closes[-period:]) / period


def _calc_dea(dif, period=9):
    dea, k = [], 2 / (period + 1)
    for i, v in enumerate(dif):
        dea.append(v if i == 0 else v * k + dea[-1] * (1 - k))
    return dea


def predict_reversal(ratio, pre_pct, post_pct, pre_bars, post_bars,
                     closes, highs, lows, opens, volumes,
                     pre_s, pre_e, post_s, post_e, dif, bar,
                     zs, atr, level_name, V):
    """
    用EP_L模型预测反转概率

    返回: (rev_type, ep_prob)
      rev_type: "高反转"/"观察"/"低反转"
      ep_prob: 0-1
    """
    if not _load_ep_model():
        # 回退: 基于ratio和RSI的简单规则
        rsi = _calc_rsi(closes[:post_e + 1], 14)
        if ratio < 30 and rsi < 30:
            return "高反转", 0.65
        elif ratio < 60 and rsi < 40:
            return "观察", 0.45
        else:
            return "低反转", 0.25

    feat_all = _compute_ep_features(
        ratio, pre_pct, post_pct, pre_bars, post_bars,
        closes, highs, lows, opens, volumes,
        pre_s, pre_e, post_s, post_e, dif, bar,
        zs, atr, level_name, V
    )
    X_all = np.array([feat_all], dtype=float)
    X_all = np.nan_to_num(X_all, nan=0.0, posinf=200.0, neginf=0.0)

    # 特征选择 (用训练时的掩码)
    if _ep_meta and 'selected_mask' in _ep_meta:
        mask = np.array(_ep_meta['selected_mask'])
        X_selected = X_all[:, mask]
    else:
        X_selected = X_all

    X_scaled = _ep_scaler.transform(X_selected)
    raw_prob = _ep_model.predict_proba(X_scaled)[0, 1]

    # 【BUG修复 2026-07-28】EP_L校准公式过于激进
    #
    # 旧公式: (raw - 0.5) * 2.0
    #   → raw<0.5 → EP=0 (71%信号归零!)
    #   → raw=0.6 → EP=0.2 (本应是有一定反转概率的)
    #   → 0%信号达到0.5阈值, 二买永远无法确认
    #
    # 新公式: 以0.3为锚点的线性拉伸
    #   raw=0.3 → EP=0.1 (极低反转)
    #   raw=0.5 → EP=0.3 (低反转)
    #   raw=0.65 → EP=0.5 (观察阈值)
    #   raw=0.8 → EP=0.75 (高反转)
    #   raw=1.0 → EP=1.0
    prob = max(0.0, min(1.0, (raw_prob - 0.3) / 0.7))

    if prob >= _EP_TREND_P:
        rev_type = "高反转"
    elif prob >= _EP_WATCH_P:
        rev_type = "观察"
    else:
        rev_type = "低反转"

    return rev_type, prob


def _load_dl_model():
    """加载深度学习模型(懒加载)

    BUG修复 (2026-07-26): 原代码 except Exception: pass 静默吞掉所有错误
    症状: sklearn未安装时模型加载失败, 回退到硬编码0.70, 全市场无DL_P>0.8
    修复: 添加错误日志, 不再静默失败

    BUG修复 (2026-07-28): _loaded=True 在try前设置, 导致首次失败后永不重试
    症状: 首次调用时sklearn未装好 → _loaded永久True → 后续即使装好也不重试
    修复: 仅在成功或永久性错误(文件不存在)时设_loaded=True, 瞬时错误保留重试机会
    """
    global _dl_model, _dl_scaler, _dl_loaded
    if _dl_loaded:
        return _dl_model is not None
    try:
        if not os.path.exists(_DL_MODEL_PATH):
            print(f"[DL模型] 模型文件不存在: {_DL_MODEL_PATH}")
            _dl_loaded = True  # 永久性错误, 不重试
            return False
        if not os.path.exists(_DL_SCALER_PATH):
            print(f"[DL模型] Scaler文件不存在: {_DL_SCALER_PATH}")
            _dl_loaded = True  # 永久性错误, 不重试
            return False
        with open(_DL_MODEL_PATH, 'rb') as f:
            _dl_model = pickle.load(f)
        with open(_DL_SCALER_PATH, 'rb') as f:
            _dl_scaler = pickle.load(f)
        _dl_loaded = True  # 成功加载
        print(f"[DL模型] 加载成功: model={type(_dl_model).__name__}, scaler={type(_dl_scaler).__name__}")
        return True
    except ImportError as e:
        print(f"[DL模型] 依赖缺失, 回退到硬编码(保留重试): {e}")
        return False  # _loaded保持False, 下次调用可重试
    except Exception as e:
        print(f"[DL模型] 加载失败, 回退到硬编码(保留重试): {type(e).__name__}: {e}")
        return False  # _loaded保持False, 下次调用可重试


def _compute_dl_features(ratio, pre_pct, post_pct, pre_bars, post_bars,
                         closes, pre_s, pre_e, post_s, post_e, dif, bar,
                         zs, atr, volume_list, level_name):
    """计算16维深度学习特征向量"""
    n = len(closes)

    # F1: ratio
    f1 = ratio

    # F2: pre_pct
    f2 = pre_pct

    # F3: post_pct
    f3 = post_pct

    # F4: pre_bars_norm
    f4 = pre_bars / 50.0

    # F5: post_bars_norm
    f5 = post_bars / 50.0

    # F6: pre_consistency
    f6 = _compute_consistency(closes, pre_s, pre_e)

    # F7: post_consistency
    f7 = _compute_consistency(closes, post_s, post_e)

    # F8: zs_width_pct
    zs_price = closes[zs['s']]
    f8 = (zs['zg'] - zs['zd']) / zs_price * 100 if zs_price > 0 else 0

    # F9: atr_norm
    f9 = atr / zs_price if zs_price > 0 else 0

    # F10: volume_ratio
    if volume_list and pre_e >= pre_s and post_e >= post_s:
        pre_vol = sum(volume_list[pre_s:pre_e + 1]) / max(1, pre_e - pre_s + 1)
        post_vol = sum(volume_list[post_s:post_e + 1]) / max(1, post_e - post_s + 1)
        f10 = post_vol / pre_vol if pre_vol > 0 else 1.0
    else:
        f10 = 1.0

    # F11: dif_slope
    f11 = _compute_dif_slope(dif, post_e)

    # F12: overall_pct
    lookback = min(60, n)
    f12 = abs(closes[-1] - closes[n - lookback]) / closes[n - lookback] * 100

    # F13: price_vs_zs
    zs_mid = (zs['zg'] + zs['zd']) / 2
    zs_half = (zs['zg'] - zs['zd']) / 2 if (zs['zg'] - zs['zd']) > 0 else 1
    f13 = (closes[post_e] - zs_mid) / zs_half
    f13 = max(-1.0, min(1.0, f13))

    # F14: macd_bar_peak
    post_bars_list = bar[post_s:post_e + 1]
    f14 = max(abs(b) for b in post_bars_list) / (atr + 1e-10) if post_bars_list else 0

    # F15: level_code (1min未参与训练, 映射到5min=2作为近似)
    level_code_map = {"日线": 0, "30min": 1, "5min": 2, "1min": 2}
    f15 = float(level_code_map.get(level_name, 0))

    # F16: bar_converge
    f16 = _compute_bar_converge(bar, post_s, post_e)

    return [f1, f2, f3, f4, f5, f6, f7, f8, f9, f10, f11, f12, f13, f14, f15, f16]


def _compute_consistency(closes, s, e):
    if e <= s:
        return 50
    overall_dir = 1 if closes[e] > closes[s] else -1
    cnt = sum(1 for i in range(s + 1, e + 1)
              if (closes[i] - closes[i - 1]) * overall_dir >= 0)
    return cnt / (e - s) * 100


def _compute_dif_slope(dif, end_idx, window=5):
    if end_idx < window:
        return 0
    y = np.array(dif[end_idx - window:end_idx])
    x = np.arange(len(y), dtype=float)
    if len(x) < 2:
        return 0
    x_mean, y_mean = x.mean(), y.mean()
    return np.sum((x - x_mean) * (y - y_mean)) / (np.sum((x - x_mean) ** 2) + 1e-10)


def _compute_bar_converge(bar, s, e):
    if e - s < 4:
        return 1.0
    mid = (s + e) // 2
    first_half = np.mean([abs(b) for b in bar[s:mid + 1]])
    second_half = np.mean([abs(b) for b in bar[mid + 1:e + 1]])
    if first_half < 1e-10:
        return 1.0
    return second_half / first_half


def _compute_atr(highs, lows, closes, period=14):
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i],
                 abs(highs[i] - closes[i - 1]),
                 abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    if len(trs) < period:
        return np.mean(trs) if trs else 0
    return np.mean(trs[-period:])


def predict_beichi(ratio, pre_pct, post_pct, pre_bars, post_bars,
                   closes, pre_s, pre_e, post_s, post_e, dif, bar,
                   zs, volume_list, level_name, atr=1.0):
    """
    用深度学习模型预测背驰概率
    返回: (sig_type, probability)
      sig_type: "趋势背驰"/"盘整背驰"/"无背驰"
      probability: 0-1
    """
    if not _load_dl_model():
        # 模型不可用, 回退到硬编码
        # 【TRAE复核修复】prob语义必须与DL路径一致(背驰概率)
        # 旧代码 min(ratio/100, 1.0) 导致"无背驰"时prob=1.0, 下游strength=5
        if ratio < 60:
            return "趋势背驰", 0.70
        elif ratio < 85:
            return "盘整背驰", 0.50
        else:
            return "无背驰", 0.10

    feat = _compute_dl_features(
        ratio, pre_pct, post_pct, pre_bars, post_bars,
        closes, pre_s, pre_e, post_s, post_e, dif, bar,
        zs, atr, volume_list, level_name
    )
    X = np.array([feat], dtype=float)
    X = np.nan_to_num(X, nan=0.0, posinf=200.0, neginf=0.0)
    X_scaled = _dl_scaler.transform(X)
    raw_prob = _dl_model.predict_proba(X_scaled)[0, 1]

    # ============================================================
    # 校准 V2 (2026-07-26): 替换V1, 解决模型输出严重偏高
    #
    # V1问题: 368只(16.4%)确认, 随机特征56.9%>0.8, 全0特征=0.86
    #   根因: Platt压缩不够 + ratio<60放大公式过激进
    #
    # V2策略: 零点拉伸 + 渐进ratio门控
    #   Step 1: 以0.5为零点线性拉伸
    #     raw=0.50→0.0, raw=0.75→0.5, raw=1.0→1.0
    #     模型bimodal分布(P25=0.15, P50=0.93), 0.5为天然分界
    #   Step 2: 渐进ratio门控 (ratio越小背驰概率越高)
    #     ratio<30:  ×1.00 (高置信, 不衰减)
    #     30-45:     ×0.85 (较高置信)
    #     45-60:     ×0.70 (中等置信)
    #     60-70:     ×0.45 (低置信)
    #     70-85:     ×0.20 (很低)
    #     >=85:      ×0.08 (几乎不可能)
    #
    # 效果验证:
    #   随机特征(raw=0.65): DL_P=0.30 (V1: 0.56) ✓假阳性消除
    #   全0特征(raw=0.86): DL_P=0.73 (V1: 0.86) ✓不再确认
    #   真信号(raw=0.93, ratio=10): DL_P=0.86 (V1: 1.0) ✓仍确认
    #   真信号(raw=0.93, ratio=45): DL_P=0.60 (V1: 1.0) ✓ratio较高不确认
    # ============================================================

    # Step 1: 零点拉伸 (0.5→0, 1.0→1.0)
    prob = max(0.0, min(1.0, (raw_prob - 0.5) * 2.0))

    # Step 2: 渐进ratio门控
    if ratio >= 85:
        prob *= 0.08
    elif ratio >= 70:
        prob *= 0.20
    elif ratio >= 60:
        prob *= 0.45
    elif ratio >= 45:
        prob *= 0.70
    elif ratio >= 30:
        prob *= 0.85
    # ratio < 30: 不衰减 (高置信)

    prob = max(0.0, min(1.0, prob))

    if prob >= _DL_TREND_P:
        sig_type = "趋势背驰"
    elif prob >= _DL_PAN_P:
        sig_type = "盘整背驰"
    else:
        sig_type = "无背驰"

    return sig_type, prob


def _market_prefix(code):
    """根据代码判断市场前缀: 6开头=沪市sh, 0/3开头=深市sz"""
    code = str(code)
    if code.startswith("6"):
        return "sh"
    else:
        return "sz"


def fetch_kline_eastmoney(code, scale="240", datalen=120):
    """东方财富K线API回退 (2026-08-07)
    当新浪API返回空数据时, 使用东方财富作为备用数据源
    scale映射: 240=日线(101), 30=30min(4), 5=5min(2), 1=1min(1)
    """
    code = str(code)
    secid_prefix = "1" if code.startswith("6") else "0"
    secid = f"{secid_prefix}.{code}"

    # scale → klt映射
    klt_map = {"240": "101", "30": "4", "5": "2", "1": "1"}
    klt = klt_map.get(str(scale), "101")

    url = (f"http://push2his.eastmoney.com/api/qt/stock/kline/get?"
           f"secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
           f"&klt={klt}&fqt=1&beg=0&end=20500101&lmt={datalen}")

    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0",
        "Referer": "http://quote.eastmoney.com/"
    })
    try:
        raw = urllib.request.urlopen(req, timeout=15).read()
    except Exception:
        return []
    resp = json.loads(raw.decode('utf-8', errors='replace'))

    klines = resp.get("data", {}).get("klines", [])
    if not klines:
        return []

    # 东方财富格式: "datetime,open,close,high,low,volume,amount"
    # 新浪格式: {"day": "...", "open": "...", "high": "...", "low": "...", "close": "...", "volume": ...}
    result = []
    for line in klines:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        dt_str = parts[0]
        # 日线格式: "2026-08-06", 分钟线格式: "2026-08-06 11:00"
        if ' ' not in dt_str and klt != "101":
            dt_str = dt_str + " 00:00"
        elif ' ' in dt_str and len(dt_str) == 16:
            dt_str = dt_str + ":00"

        result.append({
            "day": dt_str,
            "open": parts[1],
            "high": parts[3],
            "low": parts[4],
            "close": parts[2],
            "volume": float(parts[5]) if parts[5] else 0,
        })

    return result


def fetch_kline_tdx_cache(code, scale="240", datalen=120):
    """TDX MCP缓存读取 (2026-08-07)
    第三层数据源: 当新浪和东方财富均无数据时, 读取TDX预取缓存
    缓存路径: /data/user/work/tdx_cache/{code}_{scale}.json
    数据由MCP工具 tdx_kline 预取并写入, 格式与新浪兼容
    """
    cache_dir = "/data/user/work/tdx_cache"
    cache_file = os.path.join(cache_dir, f"{code}_{scale}.json")
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r') as f:
                cached = json.load(f)
            # 检查缓存时间: 超过6小时的日线缓存视为过期, 分钟线2小时过期
            import time as _time
            cache_age = _time.time() - cached.get("_ts", 0)
            max_age = 3600 * 6 if scale == "240" else 3600 * 2
            if cache_age > max_age:
                return []
            data = cached.get("data", [])
            if data:
                return data[-datalen:]  # 只返回需要的数量
    except Exception:
        pass
    return []


def fetch_kline_sina(code, scale="240", datalen=120):
    """从新浪获取K线数据, 无数据时自动回退东方财富→TDX缓存 (2026-08-07)"""
    prefix = _market_prefix(code)
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/"
           f"json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}"
           f"&scale={scale}&ma=no&datalen={datalen}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
        data = json.loads(raw.decode('utf-8', errors='replace'))
    except Exception:
        data = []

    # 回退1: 新浪返回空数据时, 使用东方财富API
    if not data:
        data = fetch_kline_eastmoney(code, scale, datalen)

    # 回退2: 东方财富也无数据时, 使用TDX缓存
    if not data:
        data = fetch_kline_tdx_cache(code, scale, datalen)

    return data


def fetch_realtime_tencent(code):
    """从腾讯获取实时价格"""
    prefix = _market_prefix(code)
    url = f"http://qt.gtimg.cn/q={prefix}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=10).read()
    parts = raw.decode('gbk', errors='replace').split('~')
    return float(parts[3])


def fetch_tencent_timeline(code):
    """从腾讯获取分时数据(每分钟均价)"""
    prefix = _market_prefix(code)
    url = f"http://web.ifzq.gtimg.cn/appstock/app/minute/query?_var=min_data&code={prefix}{code}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, timeout=10).read()
    text = raw.decode('utf-8', errors='replace')
    match = re.search(r'"data":\[(.*?)\]', text)
    if match:
        return json.loads(f"[{match.group(1)}]")
    return []


def build_1min_from_5min(code):
    """用5min K线 + 腾讯分时均价构建近似1min OHLC"""
    k5 = fetch_kline_sina(code, "5", 48)
    timeline = fetch_tencent_timeline(code)
    if not k5 or not timeline:
        return None

    # 从5min K线数据中提取最新交易日日期 (修复硬编码2026-07-14)
    today_str = ""
    for k in reversed(k5):
        d = k.get('day', '')
        if d and ' ' not in d:
            today_str = d
            break
    if not today_str:
        today_str = k5[-1]['day'] if k5 else ""

    today_k5 = [k for k in k5 if k['day'].startswith(today_str[:10])]
    if not today_k5:
        today_k5 = k5[-48:]

    tencent_map = {}
    for t in timeline:
        parts = str(t).strip().split()
        if len(parts) >= 2:
            tencent_map[parts[0]] = float(parts[1])

    result = []
    for k in today_k5:
        time_str = k['day']
        dp = time_str[:10] if ' ' in time_str else today_str[:10]
        tp = time_str[11:16] if ' ' in time_str else time_str
        if not tp:
            continue

        o, h, l, c = (float(k['open']), float(k['high']),
                      float(k['low']), float(k['close']))
        v = float(k['volume'])

        for i in range(5):
            hour = int(tp[:2])
            minute = int(tp[3:]) + i
            if minute >= 60:
                hour += 1
                minute -= 60
            hm = f"{hour:02d}{minute:02d}"
            dt = f"{dp} {hour:02d}:{minute:02d}"

            tp_price = tencent_map.get(hm)
            if tp_price:
                cl = tp_price
                op = tp_price
                hi = min(max(tp_price, l + (h - l) * 0.8), h)
                lo = max(min(tp_price, l + (h - l) * 0.2), l)
                hi = max(hi, lo + 0.001)
            else:
                cl = (o + c) / 2
                op = cl
                hi = max(cl + 0.001, l + 0.001)
                lo = l

            hi = min(hi, h)
            lo = max(lo, l)
            hi = max(hi, lo + 0.001)
            result.append({
                "time": dt,
                "open": round(op, 3),
                "high": round(hi, 3),
                "low": round(lo, 3),
                "close": round(cl, 3),
                "volume": round(v / 5)
            })

    return result


def calc_ema(vals, period):
    ema, k = [], 2 / (period + 1)
    for i, v in enumerate(vals):
        ema.append(v if i == 0 else v * k + ema[-1] * (1 - k))
    return ema


def calc_macd(closes):
    e12, e26 = calc_ema(closes, 12), calc_ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    dea = calc_ema(dif, 9)
    bar = [(d - a) * 2 for d, a in zip(dif, dea)]
    return dif, dea, bar


def find_zhongshu(highs, lows, min_width=5, min_amp_pct=0.08):
    """
    检测缠论中枢
    ZG = min(high[i:j]), ZD = max(low[i:j]), 要求 ZG > ZD
    去包含后返回
    """
    n = len(highs)
    centers = []
    for i in range(n - min_width + 1):
        best_w, best_zg, best_zd = 0, 0, 0
        for w in range(min_width, min(60, n - i + 1)):
            zg = min(highs[i:i + w])
            zd = max(lows[i:i + w])
            if zg > zd:
                best_w, best_zg, best_zd = w, zg, zd
            else:
                break
        if best_w >= min_width:
            amp = (best_zg - best_zd) / best_zd * 100
            if amp >= min_amp_pct:
                centers.append({
                    "s": i, "e": i + best_w - 1,
                    "zg": best_zg, "zd": best_zd, "w": best_w
                })

    # 去包含: 按宽度降序，依次保留不被包含的
    centers.sort(key=lambda x: (-x['w'], x['s']))
    filtered = []
    for c in centers:
        if not any(f['s'] <= c['s'] and f['e'] >= c['e'] for f in filtered):
            filtered.append(c)
    filtered.sort(key=lambda x: x['s'])
    return filtered


def calc_area(vals, s, e, direction=None):
    """
    计算MACD DIF面积(方向性面积)

    【BUG修复 2026-07-28】
    旧实现: sum(abs(v)) 对所有DIF取绝对值求和
    问题: DIF穿越零轴时, 正负值都被累加, 面积虚高导致ratio异常(如2140%)
    修复: 按趋势方向只计算对应方向的DIF面积
      - direction="down": 只累加DIF<0的绝对值 (底背驰绿柱面积)
      - direction="up":   只累加DIF>0的绝对值 (顶背驰红柱面积)
      - direction=None:   兼容旧逻辑(abs全部), 仅供fallback
    """
    if s >= e or s < 0 or e >= len(vals):
        return 0
    seg = vals[s:e + 1]
    if direction == "down":
        area = sum(abs(v) for v in seg if v < 0)
        # fallback: 如果整段没有负值(极少见), 退回abs全部避免除零
        return area if area > 0 else sum(abs(v) for v in seg)
    elif direction == "up":
        area = sum(abs(v) for v in seg if v > 0)
        return area if area > 0 else sum(abs(v) for v in seg)
    else:
        return sum(abs(v) for v in seg)


def seg_direction(closes, s, e):
    """判断一段走势方向"""
    if e <= s:
        return "flat"
    return "up" if closes[e] > closes[s] else "down"


def is_meaningful_trend(closes, s, e, min_bars=15, min_pct=0.15):
    """
    【BUG修复】检查走势段是否有意义
    - 最少min_bars根K线
    - 价格变动幅度至少min_pct%
    - 趋势一致性: 至少50%K线与整体方向一致
    """
    if e - s + 1 < min_bars:
        return False, 0, f"太短({e-s+1}根)"

    total_pct = abs(closes[e] - closes[s]) / closes[s] * 100
    if total_pct < min_pct:
        return False, total_pct, f"幅度太小({total_pct:.2f}%)"

    overall_dir = 1 if closes[e] > closes[s] else -1
    consistent = sum(
        1 for i in range(s + 1, e + 1)
        if (closes[i] - closes[i - 1]) * overall_dir >= 0
    )
    consistency = consistent / (e - s) * 100
    if consistency < 50:
        return False, total_pct, f"一致性差({consistency:.0f}%)"

    return True, total_pct, f"OK(幅{total_pct:.2f}%, 一致{consistency:.0f}%)"


def analyze_beichi(code, level="日线", price=None, cost=0):
    """
    缠论背驰分析
    返回中枢列表 + 背驰信号列表

    性能修复 (2026-07-26): 添加内存缓存
    问题: check_compliance中detect_entry_level调用4个级别, 之后sell_levels又重复调用同一级别
          每只持仓产生5-8次analyze_beichi, 每次都发网络请求 → 8持仓=40-64次请求 → 卡死
    方案: 同一(code,level)在一次运行中只请求一次API, 后续用缓存
    """
    # 内存缓存: 同一(code,level)不重复请求
    # 注意: 只缓存成功结果, 不缓存错误结果(Codex复核指出的隐患)
    #       网络临时失败返回{"error":...}时, 后续调用应有机会重试
    cache_key = (str(code), level)
    if not hasattr(analyze_beichi, '_cache'):
        analyze_beichi._cache = {}
    if cache_key in analyze_beichi._cache:
        return analyze_beichi._cache[cache_key]

    scale_map = {"日线": "240", "30min": "30", "5min": "5", "1min": "1"}
    min_w_map = {"日线": 5, "30min": 4, "5min": 3, "1min": 3}
    # BUG修复 (2026-07-29): 30min min_amp_pct=0.1%过低 → 4.8元股价下0.01元(1tick)就能形成中枢
    # 问题: 23个中枢中22个宽度仅0.01元, 全是噪音 → 现价偏离1tick就"破位"
    # 修复: 30min从0.1%提高到0.5%, 5min从0.05%提高到0.3%
    #   4.8元 × 0.5% = 0.024元 → 过滤tick噪音, 保留真实中枢
    #   3.4元 × 0.5% = 0.017元 → 三力士同理
    min_amp_map = {"日线": 0.3, "30min": 0.5, "5min": 0.3, "1min": 0.02}
    pre_bars_map = {"日线": 25, "30min": 20, "5min": 20, "1min": 30}  # 修复: 1min从15增至30
    pre_min_pct = {"日线": 1.0, "30min": 0.5, "5min": 0.2, "1min": 0.1}  # 修复: 新增幅度门槛
    post_min_bars = {"日线": 5, "30min": 5, "5min": 3, "1min": 5}  # 修复: 1min从3增至5

    if level == "1min":
        bars = build_1min_from_5min(code)
        if not bars:
            return {"error": "1min data unavailable"}  # 不缓存: 允许后续重试
        C = [b['close'] for b in bars]
        H = [b['high'] for b in bars]
        L = [b['low'] for b in bars]
        O = [b['open'] for b in bars]
        times = [b['time'] for b in bars]
        V = [b['volume'] for b in bars]
        n = len(C)
    else:
        data = fetch_kline_sina(code, scale_map[level], 120)
        if not data:
            return {"error": "no data"}  # 不缓存: 允许后续重试
        C = [float(d['close']) for d in data]
        H = [float(d['high']) for d in data]
        L = [float(d['low']) for d in data]
        O = [float(d['open']) for d in data]
        times = [d['day'] for d in data]
        V = [float(d.get('volume', 0)) for d in data]
        n = len(C)

    dif, dea, bar = calc_macd(C)
    atr = _compute_atr(H, L, C)  # 【DL修复】计算真实ATR供深度学习特征使用

    # TRAE复核修复 (2026-07-26): price=None导致二买/三买检测TypeError
    # 原因: daily_workflow调用analyze_beichi(code, level=level)不传price
    #       二买检测 curr_zs["zd"] <= price <= curr_zs["zg"] → None比较崩溃
    #       被try-except吞掉 → 二买/三买静默失效
    # 修复: price=None时使用最新收盘价
    if price is None:
        price = C[-1] if C else 0

    # 【BUG修复】大级别方向判断 (最近60根/或整体)
    lookback = min(60, n)
    overall_dir = seg_direction(C, n - lookback, n - 1)
    overall_pct = abs(C[-1] - C[n - lookback]) / C[n - lookback] * 100

    zss = find_zhongshu(H, L, min_w_map[level], min_amp_map[level])
    signals = []

    for i_zs, zs in enumerate(zss):
        zs_s, zs_e = zs['s'], zs['e']
        if zs_e >= n - post_min_bars[level]:
            continue

        # 【BUG-10修复 (2026-07-27)】中枢首尾相接导致pre段=0
        #
        # BUG根因:
        #   BUG-2修复用 pre_s = prev_zs["e"] + 1, 假设中枢之间有足够间距
        #   但30min/5min的find_zhongshu用min_amp=0.1%, 产生大量首尾相接的中枢
        #   → prev_zs["e"] + 1 >= zs_s → pre段长度=0或负数
        #   → seg_direction(flat) → 信号过滤条件永远不满足
        #   → 30min/5min级别24/23个中枢却0个信号, 二买永远无法触发
        #
        # 典型案例: 丽珠集团 000513
        #   30min: 24个中枢在30~31.5狭窄区间, 0个信号
        #   5min: 23个中枢, 0个信号
        #   日线: 中枢间距大, 正常产出信号
        #
        # 修复策略:
        #   有前中枢且间距>=min_gap: 用BUG-2逻辑 pre_s = prev_zs["e"] + 1
        #   有前中枢但间距<min_gap(首尾相接): 回退到固定根数 pre_s = zs_s - pre_bars
        #   无前中枢: 保持原逻辑(固定根数)
        if i_zs > 0:
            prev_zs_for_pre = zss[i_zs - 1]
            raw_pre_s = prev_zs_for_pre["e"] + 1
            min_gap = 3  # 前中枢结束后至少隔3根K线才用BUG-2逻辑
            if raw_pre_s + min_gap <= zs_s:
                # 中枢间距足够 → 用BUG-2逻辑
                pre_s = raw_pre_s
            else:
                # 中枢首尾相接 → 回退固定根数, 标注为盘整背驰
                pre_s = max(0, zs_s - pre_bars_map[level])
        else:
            pre_s = max(0, zs_s - pre_bars_map[level])
        pre_e = zs_s - 1
        post_s = zs_e + 1
        post_e = n - 1

        pre_ok, pre_pct, pre_reason = is_meaningful_trend(
            C, pre_s, pre_e,
            min_bars=max(8, pre_bars_map[level] // 2),
            min_pct=pre_min_pct[level]
        )
        post_ok, post_pct, post_reason = is_meaningful_trend(
            C, post_s, post_e,
            min_bars=post_min_bars[level],
            min_pct=pre_min_pct[level] * 0.5
        )

        pre_d = seg_direction(C, pre_s, pre_e)
        post_d = seg_direction(C, post_s, post_e)

        if pre_d != "flat" and post_d != "flat" and pre_d == post_d:
            pre_a = calc_area(dif, pre_s, pre_e, direction=pre_d)
            post_a = calc_area(dif, post_s, post_e, direction=post_d)
            # 【BUG修复续 2026-07-28】pre段方向性面积过小时ratio无意义
            # 当pre段MACD动能极弱(DIF几乎不穿越零轴), 分母趋零导致ratio虚高至数千%
            MIN_AREA = 0.5  # 最小有效方向性DIF面积
            if pre_a < MIN_AREA:
                ratio = 999  # pre段MACD动能不足, 无背驰意义
            else:
                ratio = (post_a / pre_a * 100)

            # 【DL修复】ratio裁剪到训练数据分布范围[10,150]
            ratio_clamped = max(10.0, min(150.0, ratio))

            # 【深度学习】用MLP模型替代硬编码阈值
            dl_sig_type, dl_prob = predict_beichi(
                ratio_clamped, pre_pct, post_pct,
                pre_e - pre_s + 1, post_e - post_s + 1,
                C, pre_s, pre_e, post_s, post_e, dif, bar,
                zs, V, level, atr
            )
            sig_type = dl_sig_type
            direction = "看多" if pre_d == "down" else "看空"
            op = "一买" if pre_d == "down" else "一卖"

            # 【BUG-3修复 (2026-07-26)】价格新低/新高检查
            #
            # BUG根因:
            #   旧代码只比较MACD面积比ratio, 未检查价格是否创新低/新高
            #   底背驰定义: 价格新低 + MACD不新低
            #   顶背驰定义: 价格新高 + MACD不新高
            #   → 即使post段价格高于pre段(非新低), 只要MACD面积小就被判为背驰
            #
            # 修复策略: 不丢弃信号(避免机会成本), 仅校正type
            #   一买: post段最低价 < pre段最低价 → 保持背驰判定
            #         post段最低价 >= pre段最低价 → 降级为"无背驰"(非底背驰)
            #   一卖: post段最高价 > pre段最高价 → 保持背驰判定
            #         post段最高价 <= pre段最高价 → 降级为"无背驰"(非顶背驰)
            price_new_extreme = False
            if pre_d == "down":
                # 一买: 检查价格新低
                pre_low = min(C[pre_s:pre_e + 1])
                post_low = min(C[post_s:post_e + 1])
                price_new_extreme = post_low < pre_low
            else:
                # 一卖: 检查价格新高
                pre_high = max(C[pre_s:pre_e + 1])
                post_high = max(C[post_s:post_e + 1])
                price_new_extreme = post_high > pre_high

            # 【BUG修复 (2026-08-04)】price_new_extreme硬过滤导致DL_P=0
            # 问题: 价格未创新低(二买/类二买) → 直接降级为"无背驰"
            #       日线信号dl_prob=0.93被硬杀死, 下游get_signal_summary过滤后DL_P=0
            # 修复: 改为软惩罚 — 趋势背驰降级为盘整背驰, 不直接杀死
            # 保留盘整背驰级别的信号, 让DL模型做主
            if not price_new_extreme and sig_type != "无背驰":
                if sig_type == "趋势背驰":
                    sig_type = "盘整背驰"  # 降一级, 保留信号

            # 【BUG-1修复 (2026-07-26)】趋势背驰 vs 盘整背驰校正
            #
            # 缠论分类思想(借鉴):
            #   趋势背驰: 下跌趋势中(至少两个中枢依次下移)的背驰
            #     条件: 存在前一个中枢, 且 prev_zg > curr_zg 且 prev_zd > curr_zd
            #   盘整背驰: 单个中枢后的背驰(无下跌趋势)
            #
            # BUG根因: DL模型返回"趋势背驰"时, 未验证是否存在下跌趋势
            #   → 单个中枢后的盘整背驰被误判为趋势背驰
            #   → 下游DL_P偏高, 候选池误纳入盘整背驰信号
            #
            # 修复策略: 不丢弃信号(避免机会成本), 仅校正type
            #   有下跌趋势 → 保持"趋势背驰"
            #   无下跌趋势 + DL返回"趋势背驰" → 降级为"盘整背驰"
            has_downtrend = False
            if i_zs > 0:
                prev_zs_check = zss[i_zs - 1]
                if (prev_zs_check["zg"] > zs["zg"] and
                        prev_zs_check["zd"] > zs["zd"]):
                    has_downtrend = True
            if not has_downtrend and sig_type == "趋势背驰":
                sig_type = "盘整背驰"

            # 【BUG修复】大级别方向过滤
            aligned = (pre_d == overall_dir)

            # 【EP_L】计算反转概率
            try:
                ep_rev_type, ep_prob = predict_reversal(
                    ratio_clamped, pre_pct, post_pct,
                    pre_e - pre_s + 1, post_e - post_s + 1,
                    C, H, L, O, V,
                    pre_s, pre_e, post_s, post_e, dif, bar,
                    zs, atr, level, V
                )
            except Exception as e:
                print(f"[EP预测异常] {type(e).__name__}: {e}")
                import traceback; traceback.print_exc()
                ep_rev_type, ep_prob = "低反转", 0.25

            signals.append({
                "type": sig_type,
                "dir": direction,
                "op": op,
                "ratio": ratio,
                "dl_prob": dl_prob,  # 深度学习背驰概率
                "ep_prob": ep_prob,  # 反转概率 EP_L
                "ep_type": ep_rev_type,  # 反转类型: 高反转/观察/低反转
                "zs": zs,
                "pre_dir": pre_d,
                "post_dir": post_d,
                "pre_ok": pre_ok,
                "post_ok": post_ok,
                "valid": (pre_ok and post_ok and aligned) or dl_prob >= 0.6,  # 【BUG修复】DL模型高置信度(>=0.6)软豁免硬编码条件
                "aligned": aligned,
                "overall_dir": overall_dir,
                "has_downtrend": has_downtrend,  # BUG-1: 下跌趋势标记
                "price_new_extreme": price_new_extreme,  # BUG-3: 价格新低/新高标记
                "pre_range": f"{times[pre_s]}~{times[pre_e]}",
                "post_range": f"{times[post_s]}~{times[post_e]}",
                "pre_reason": pre_reason,
                "post_reason": post_reason,
            })

    # ============================================================
    # 二买/三买信号检测 V3 (2026-07-26): DL_P验证 + 四重条件
    #
    # BUG修复 (Codex审计):
    #   1. 硬编码概率 → 改为DL模型计算真实概率
    #   2. 缺一买前提 → 必须验证存在有效一买且时间在前
    #   3. 三买定义偏差 → 需回踩确认不追高
    #   4. BUG-5修复: 去掉overall_dir=="up"前提
    #
    # 缠论分类思想(借鉴, 非严格符合):
    #   二买: 一买后反弹形成新中枢, 回踩不破前低 → 趋势确认
    #   三买: 二买后新中枢形成, 回踩不破二买中枢上沿 → 趋势加速
    #
    # BUG-5根因:
    #   旧代码要求overall_dir=="up"才检测二买
    #   但一买是下跌趋势末端的信号, 一买后的反弹回调(二买)
    #   发生时, 60根K线的overall_dir可能还是"down"
    #   → 大部分真实二买信号被过滤(隆基绿能典型案例)
    #
    # 修复策略:
    #   去掉overall_dir=="up"前提
    #   改为"存在有效一买信号"作为唯一趋势前提
    #   中枢上移条件保留(区分二买/三买)
    # ============================================================
    
    # 条件0: 必须有有效一买信号(二买/三买的前提)
    bull_signals = [s for s in signals if s["op"] == "一买" and s["valid"]]
    if not bull_signals or len(zss) < 2:
        pass  # 无一买信号, 跳过二买/三买检测
    else:
        # BUG-5修复: 去掉 overall_dir == "up" 前提
        # 一买信号本身已隐含趋势反转, 不需要大级别方向确认
        for i in range(1, len(zss)):
            prev_zs = zss[i - 1]
            curr_zs = zss[i]

            # 中枢上移: 当前中枢下沿 > 前中枢上沿
            if curr_zs["zd"] > prev_zs["zg"] and price > 0:
                # 计算中枢上移的DL特征
                pre_s_ermai = max(0, prev_zs["s"] - pre_bars_map[level])
                pre_e_ermai = prev_zs["e"]
                post_s_ermai = curr_zs["s"]
                post_e_ermai = min(n - 1, curr_zs["e"])

                pre_pct_ermai = abs(C[pre_e_ermai] - C[pre_s_ermai]) / C[pre_s_ermai] * 100 if C[pre_s_ermai] > 0 else 0
                post_pct_ermai = abs(C[post_e_ermai] - C[post_s_ermai]) / C[post_s_ermai] * 100 if C[post_s_ermai] > 0 else 0
                # 计算二买段方向(用于方向性面积计算)
                pre_d_ermai = seg_direction(C, pre_s_ermai, pre_e_ermai)
                post_d_ermai = seg_direction(C, post_s_ermai, post_e_ermai)
                # TRAE修复: 真实计算MACD面积比(替代硬编码50) + 方向性面积
                pre_a_ermai = calc_area(dif, pre_s_ermai, pre_e_ermai, direction=pre_d_ermai)
                post_a_ermai = calc_area(dif, post_s_ermai, post_e_ermai, direction=post_d_ermai)
                # 【BUG修复续 2026-07-28】同样增加最小面积阈值防止ratio虚高
                if pre_a_ermai < 0.5:
                    ratio_ermai = 999
                else:
                    ratio_ermai = (post_a_ermai / pre_a_ermai * 100) if pre_a_ermai > 0 else 999

                # 用DL模型计算真实概率
                try:
                    dl_sig_type, dl_prob_ermai = predict_beichi(
                        max(10, min(150, ratio_ermai)), pre_pct_ermai, post_pct_ermai,
                        pre_e_ermai - pre_s_ermai + 1, post_e_ermai - post_s_ermai + 1,
                        C, pre_s_ermai, pre_e_ermai, post_s_ermai, post_e_ermai,
                        dif, bar, curr_zs, V, level, atr
                    )
                except:
                    dl_prob_ermai = 0.50  # DL不可用时保守估计

                # 二买: 回调不破一买低点(BUG-4修复)
                #
                # BUG-4根因:
                #   旧代码: curr_zs["zd"] <= price <= curr_zs["zg"]
                #   → 检查"回踩中枢区间", 但一买低点与中枢下沿ZD是不同概念
                #   → 价格可能在中枢区间内却已跌破一买低点(趋势仍下跌)
                #   → 价格可能在一买低点上方但不在中枢区间内(错过真实二买)
                #
                # 修复策略:
                #   计算一买低点(一买中枢的最低价)
                #   二买条件改为: price >= one_buy_low (回调不破一买低点)
                #   中枢上移条件保留(趋势确认)
                one_buy_low = None
                one_buy_sig = None
                for sig in bull_signals:
                    sig_zs = sig["zs"]
                    if sig_zs["e"] <= prev_zs["s"]:
                        sig_low = min(C[sig_zs["s"]:sig_zs["e"]+1])
                        if one_buy_low is None or sig_low < one_buy_low:
                            one_buy_low = sig_low
                            one_buy_sig = sig

                if one_buy_low is not None and price >= one_buy_low:
                    # 条件2: 一买信号的时间必须在中枢上移之前
                    one_buy_before = one_buy_sig is not None
                    # 条件3: DL_P >= 0.4 (盘整背驰门槛, 二买要求较低)
                    if one_buy_before and dl_prob_ermai >= 0.4:
                        # EP_L: 二买反转概率
                        # 【BUG修复 2026-07-28】旧代码硬编码pre_bars=30, post_bars=10
                        #   且窗口用curr_zs相对位置, 与实际走势段不匹配
                        #   修复: 用实际中枢间距作为窗口
                        ermai_pre_bars = max(5, pre_e_ermai - pre_s_ermai + 1)
                        ermai_post_bars = max(3, post_e_ermai - post_s_ermai + 1)
                        try:
                            ep_rev_type_2m, ep_prob_2m = predict_reversal(
                                ratio_ermai, pre_pct_ermai, post_pct_ermai,
                                ermai_pre_bars, ermai_post_bars,
                                C, H, L, O, V,
                                pre_s_ermai, pre_e_ermai,
                                post_s_ermai, post_e_ermai,
                                dif, bar, curr_zs, atr, level, V
                            )
                        except:
                            ep_rev_type_2m, ep_prob_2m = "观察", 0.40
                        signals.append({
                            "type": "盘整背驰" if dl_prob_ermai >= _DL_PAN_P else "无背驰",
                            "dir": "看多",
                            "op": "二买",
                            "ratio": ratio_ermai,
                            "dl_prob": dl_prob_ermai,
                            "ep_prob": ep_prob_2m,
                            "ep_type": ep_rev_type_2m,
                            "zs": curr_zs,
                            "pre_dir": "down",
                            "post_dir": "up",
                            "pre_ok": True,
                            "post_ok": True,
                            "valid": dl_prob_ermai >= 0.4 and one_buy_before,
                            "aligned": True,
                            "overall_dir": overall_dir,
                            "one_buy_low": one_buy_low,  # BUG-4: 一买低点
                            "pre_range": f"中枢{i}",
                            "post_range": f"中枢{i+1}",
                            "pre_reason": f"中枢上移+一买低点{one_buy_low:.2f}",
                            "post_reason": f"回调不破一买低点(现价{price:.2f})",
                        })

                # 三买: 连续两个中枢上移 + 突破后回踩不破第二中枢上沿
                if i >= 2:
                    prev2_zs = zss[i - 2]
                    if prev_zs["zd"] > prev2_zs["zg"]:
                        # 回踩确认: 价格在当前中枢上方但有过回调
                        if price > curr_zs["zg"]:
                            # 检查最近5根K线是否有回踩动作(最低价接近curr_zs上沿)
                            recent_lows = L[max(0, n-5):n]
                            has_pullback = any(
                                low <= curr_zs["zg"] * 1.02 for low in recent_lows
                            )

                            if has_pullback:
                                # 三买DL特征 — TRAE修复: 复用二买的真实ratio(同一段走势)
                                ratio_sanmai = ratio_ermai
                                try:
                                    dl_sig_type, dl_prob_sanmai = predict_beichi(
                                        max(10, min(150, ratio_sanmai)),
                                        pre_pct_ermai, post_pct_ermai,
                                        pre_e_ermai - pre_s_ermai + 1,
                                        post_e_ermai - post_s_ermai + 1,
                                        C, pre_s_ermai, pre_e_ermai,
                                        post_s_ermai, post_e_ermai,
                                        dif, bar, curr_zs, V, level, atr
                                    )
                                except:
                                    dl_prob_sanmai = 0.50

                                # 三买要求更高: DL_P >= 0.45
                                if dl_prob_sanmai >= 0.45:
                                    # EP_L: 三买反转概率
                                    try:
                                        ep_rev_type_3m, ep_prob_3m = predict_reversal(
                                            max(10, min(150, ratio_sanmai)),
                                            pre_pct_ermai, post_pct_ermai,
                                            pre_e_ermai - pre_s_ermai + 1,
                                            post_e_ermai - post_s_ermai + 1,
                                            C, H, L, O, V,
                                            pre_s_ermai, pre_e_ermai,
                                            post_s_ermai, post_e_ermai,
                                            dif, bar, curr_zs, atr, level, V
                                        )
                                    except:
                                        ep_rev_type_3m, ep_prob_3m = "观察", 0.40
                                    signals.append({
                                        "type": "盘整背驰" if dl_prob_sanmai >= _DL_PAN_P else "无背驰",
                                        "dir": "看多",
                                        "op": "三买",
                                        "ratio": ratio_sanmai,
                                        "dl_prob": dl_prob_sanmai,
                                        "ep_prob": ep_prob_3m,
                                        "ep_type": ep_rev_type_3m,
                                        "zs": curr_zs,
                                        "pre_dir": "down",
                                        "post_dir": "up",
                                        "pre_ok": True,
                                        "post_ok": True,
                                        "valid": dl_prob_sanmai >= 0.45 and has_pullback,
                                        "aligned": True,
                                        "overall_dir": overall_dir,
                                        "pre_range": f"中枢{i-1}",
                                        "post_range": f"中枢{i+1}",
                                        "pre_reason": "连续上移+回踩确认",
                                        "post_reason": "突破加速",
                                    })

    # P3 (2026-08-02): 为每个信号计算综合置信度
    for s in signals:
        s["confidence"] = compute_confidence_score(s)

    signals.sort(key=lambda x: -x['zs']['e'])

    # BUG修复 (2026-07-29): result未返回H/L → 外部调用者r.get("H",[])返回空
    # 影响: daily_workflow的中枢破位检查用r.get("zss")拿到的是正确的(内部计算),
    #       但任何需要外部重新检查H/L的逻辑都会失败
    result = {
        "code": code,
        "level": level,
        "n": n,
        "times": times,
        "C": C,
        "H": H,  # BUG修复: 补充H/L返回
        "L": L,  # BUG修复: 补充H/L返回
        "zss": zss,
        "signals": signals,
        "price": price,
        "cost": cost,
        "overall_dir": overall_dir,
        "overall_pct": overall_pct,
    }
    analyze_beichi._cache[cache_key] = result
    return result


# ============================================================
# 中阴状态检测 (P1, 2026-08-02)
#
# 中阴定义 (缠论): 背驰信号已出现但新趋势尚未确认的过渡期
#   - 有背驰信号(一买/一卖valid)但无二买/二卖确认
#   - 价格在最后一个中枢区间内震荡
#   - MACD DIF在零轴附近(动能不明)
#
# 操作含义:
#   NotChasing — 不追涨杀跌, 等待趋势确认
#   仓位压制 — 持仓不超过原计划的50%
#
# Gemini PDF建议: 防止背驰后假反转导致的止损触发
# ============================================================

def detect_zhongyin(result):
    """
    检测中阴状态(过渡态)

    参数:
        result: analyze_beichi() 的返回值

    返回: {
        "is_zhongyin": True/False,
        "reason": str,
        "action": "NotChasing" / "仓位压制" / "正常",
        "price_vs_zs": "中枢内" / "中枢上" / "中枢下",
        "has_one_signal": bool,
        "has_two_signal": bool,
        "dif_near_zero": bool,
    }
    """
    if result.get("error") or not result.get("zss"):
        return {
            "is_zhongyin": False, "reason": "无中枢数据",
            "action": "正常", "price_vs_zs": "未知",
            "has_one_signal": False, "has_two_signal": False,
            "dif_near_zero": False,
        }

    signals = result.get("signals", [])
    price = result.get("price", 0)
    zss = result["zss"]
    last_zs = zss[-1]
    zg = last_zs["zg"]
    zd = last_zs["zd"]

    # 条件1: 有背驰信号(一买/一卖valid)但无二买/二卖确认
    has_one = any(
        s.get("op") in ("一买", "一卖") and s.get("valid")
        for s in signals
    )
    has_two = any(
        s.get("op") in ("二买", "二卖") and s.get("valid")
        for s in signals
    )

    # 条件2: 价格相对中枢位置
    in_zs = zd <= price <= zg
    if in_zs:
        price_vs_zs = "中枢内"
    elif price > zg:
        price_vs_zs = "中枢上"
    else:
        price_vs_zs = "中枢下"

    # 条件3: MACD DIF在零轴附近
    # DIF绝对值 < DEA绝对值 * 0.5 → 动能不明
    C = result.get("C", [])
    dif_near_zero = False
    if len(C) >= 30:
        try:
            dif, dea, bar = calc_macd(C)
            last_dif = dif[-1] if dif else 0
            last_dea = dea[-1] if dea else 0
            if last_dea != 0:
                dif_near_zero = abs(last_dif) < abs(last_dea) * 0.5
            else:
                dif_near_zero = abs(last_dif) < 0.01
        except:
            dif_near_zero = False

    # 中阴判定: 有背驰信号但无趋势确认 + 价格在中枢内
    is_zy = has_one and not has_two and in_zs

    if is_zy:
        action = "仓位压制"
        reason = (
            f"中阴: 背驰信号存在但二买/二卖未确认, "
            f"价格在中枢[{zd:.2f},{zg:.2f}]内震荡, DIF近零轴={dif_near_zero}"
        )
    elif has_one and not has_two and not in_zs:
        action = "NotChasing"
        reason = (
            f"背驰信号存在但趋势未确认, 价格{price_vs_zs}, "
            f"不追涨(等待二买/二卖确认)"
        )
    else:
        action = "正常"
        reason = "趋势已确认或无背驰信号"

    return {
        "is_zhongyin": is_zy,
        "reason": reason,
        "action": action,
        "price_vs_zs": price_vs_zs,
        "has_one_signal": has_one,
        "has_two_signal": has_two,
        "dif_near_zero": dif_near_zero,
    }


# ============================================================
# 综合置信度评分 (P3, 2026-08-02)
#
# Gemini PDF建议: 信号输出增加综合置信度
# 替代单纯DL_P阈值, 综合多维度量化信号可靠性
#
# 评分维度 (权重):
#   DL_P 背驰概率      — 0.40 (核心: 背驰结构是否存在)
#   EP_L 反转概率      — 0.30 (验证: 反转是否在发生)
#   ratio归一化        — 0.15 (面积比越低越强)
#   方向对齐(aligned)  — 0.15 (与大级别方向一致)
# ============================================================

def compute_confidence_score(signal):
    """
    计算信号综合置信度

    参数:
        signal: analyze_beichi() 返回的 signals 列表中的单个信号 dict

    返回: float [0, 1]
    """
    # 1. DL_P 背驰概率 (0-1) → 权重0.40
    dl_prob = signal.get("dl_prob", 0)
    dl_score = min(1.0, dl_prob)

    # 2. EP_L 反转概率 (0-1) → 权重0.30
    ep_prob = signal.get("ep_prob", 0)
    ep_score = min(1.0, ep_prob)

    # 3. ratio归一化 → 权重0.15
    # ratio < 20% → 1.0 (强背驰)
    # ratio 20-60% → 线性递减
    # ratio > 60% → 0.2 (弱背驰)
    ratio = signal.get("ratio", 999)
    if ratio < 20:
        ratio_score = 1.0
    elif ratio < 60:
        ratio_score = 1.0 - (ratio - 20) / 40 * 0.8
    else:
        ratio_score = 0.2

    # 4. 方向对齐 → 权重0.15
    aligned = signal.get("aligned", False)
    align_score = 1.0 if aligned else 0.3

    # 加权汇总
    confidence = (
        dl_score * 0.40 +
        ep_score * 0.30 +
        ratio_score * 0.15 +
        align_score * 0.15
    )

    return round(confidence, 4)


# ============================================================
# Second/Third buy/sell 几何判定函数 (2026-08-08)
# 集成自 Gemini 笔记本，用于替代 EP_L 概率判定
# 独立于 pandas，仅需基础数值判断
# ============================================================

def evaluate_second_buy_sell(
    first_point_price: float,
    rebound_high: float,
    pullback_low: float,
    center: dict = None,
    signal_type: str = "second_buy",
    tick_size: float = 0.01,
    # 【Fix边界2: 次级别假突破过滤】2026-08-08
    # 集成 min_width>=5 和 min_amp>=0.08 硬性校验
    # 确保次级别回撤段构成结构完备的次级别中枢
    pullback_klines: int = 0,          # 回撤段K线数量
    pullback_amp_pct: float = 0.0,    # 回撤段振幅百分比
    min_width: int = 5,                # 最小K线数量要求
    min_amp: float = 0.08,             # 最小振幅百分比要求
) -> dict:
    """
    第二类买卖点严格几何判定（简化版，无需DataFrame）。

    参数
    ----
    first_point_price : float
        一买低点(L1buy) 或 一卖高点(H1sell)
    rebound_high : float
        一买后反弹段最高价(H1rebound)
    pullback_low : float
        回踩段最低价(L2buy)
    center : dict, optional
        中枢区间 {zg, zd}，用于检测二三买重叠
    signal_type : str
        'second_buy' 或 'second_sell'
    tick_size : float
        最小价格变动单位，默认0.01
    pullback_klines : int
        回撤段K线数量，用于min_width校验
    pullback_amp_pct : float
        回撤段振幅百分比，用于min_amp校验
    min_width : int
        最小K线数量要求（默认5），防止杂波误识别
    min_amp : float
        最小振幅百分比要求（默认0.08），防止平盘假突破

    返回
    ----
    dict : {
        'is_confirmed': bool,
        'status': str,
        'invalid_boundary': float | None,
        'optimal_buy_min': float | None,
        'optimal_buy_max': float | None,
        'cushion': float,
        'cushion_ratio': float,
        'reason': str
    }
    """
    result = {
        'is_confirmed': False,
        'status': 'UNKNOWN',
        'invalid_boundary': None,
        'optimal_buy_min': None,
        'optimal_buy_max': None,
        'cushion': 0.0,
        'cushion_ratio': 0.0,
        'reason': '',
    }

    if signal_type == 'second_buy':
        l1 = first_point_price
        h1 = rebound_high
        l2 = pullback_low
        cushion = round(l2 - l1, 4)

        # 【Fix边界2: 次级别假突破过滤】2026-08-08
        # min_width>=5: 回撤段至少5根K线，排除杂波
        # min_amp>=0.08: 回撤段振幅至少0.08%，排除平盘
        sub_level_ok = True
        if pullback_klines > 0 and pullback_klines < min_width:
            sub_level_ok = False
            result['status'] = 'REJECTED_SUB_LEVEL_WIDTH'
            result['reason'] = (
                f"二买拒绝(次级别宽度不足): 回撤段{pullback_klines}根K线 < "
                f"min_width={min_width}, 次级别结构未完备"
            )
        if pullback_amp_pct > 0 and pullback_amp_pct < min_amp:
            sub_level_ok = False
            result['status'] = 'REJECTED_SUB_LEVEL_AMP'
            result['reason'] = (
                f"二买拒绝(次级别振幅不足): 振幅{pullback_amp_pct:.4f}% < "
                f"min_amp={min_amp}%, 平盘假突破风险"
            )

        if not sub_level_ok:
            return result

        if l2 > l1:
            result['is_confirmed'] = True
            result['invalid_boundary'] = l1
            result['optimal_buy_min'] = round(l1 * 1.005, 4)
            if h1 > l1:
                result['optimal_buy_max'] = round(l1 + (h1 - l1) * 0.382, 4)
            else:
                result['optimal_buy_max'] = round(l1 + tick_size, 4)
            result['cushion'] = cushion
            result['cushion_ratio'] = round(cushion / l1, 4) if l1 != 0 else 0.0

            # 二三买重叠检测
            if center and l2 > center.get('zg', 9e9):
                result['status'] = 'CONFIRMED_SECOND_THIRD_BUY_OVERLAP'
                result['reason'] = (
                    f"二买确认(二三买重叠): L2buy({l2:.2f})>L1buy({l1:.2f}) "
                    f"且突破ZG({center['zg']:.2f}), 缓冲垫={cushion:.2f}"
                )
            else:
                result['status'] = 'CONFIRMED_SECOND_BUY'
                result['reason'] = (
                    f"二买几何确认: L2buy({l2:.2f})>L1buy({l1:.2f}) "
                    f"缓冲垫={cushion:.2f} 黄金区间"
                    f"[{result['optimal_buy_min']:.2f},{result['optimal_buy_max']:.2f}]"
                )
        else:
            result['status'] = 'REJECTED_NEW_LOW_FAILED'
            result['invalid_boundary'] = l1
            result['reason'] = (
                f"二买失败: L2buy({l2:.2f})<=L1buy({l1:.2f}), "
                f"跌破一买低点, 下行趋势延续"
            )

    elif signal_type == 'second_sell':
        h1 = first_point_price
        l1_pullback = rebound_high  # 一卖后回踩段最低价
        h2 = pullback_low           # 反弹段最高价
        cushion = round(h1 - h2, 4)

        if h2 < h1:
            result['is_confirmed'] = True
            result['invalid_boundary'] = h1
            result['optimal_sell_min'] = round(h1 - (h1 - l1_pullback) * 0.382, 4)
            result['optimal_sell_max'] = round(h1 - tick_size, 4)
            result['cushion'] = cushion
            result['cushion_ratio'] = round(cushion / h1, 4) if h1 != 0 else 0.0
            result['status'] = 'CONFIRMED_SECOND_SELL'
            result['reason'] = (
                f"二卖确认: H2sell({h2:.2f})<H1sell({h1:.2f}), "
                f"缓冲垫={cushion:.2f}"
            )
        else:
            result['status'] = 'REJECTED_NEW_HIGH_FAILED'
            result['invalid_boundary'] = h1
            result['reason'] = (
                f"二卖失败: H2sell({h2:.2f})>=H1sell({h1:.2f}), "
                f"突破一卖高点, 上行趋势延续"
            )

    return result


# ============================================================
# 多级别二买/三买信号检测 V5.2 (2026-08-08)
#
# 替代缠论"次级别同构性", 用几何判定+多级别共振
#
# 【重要: DL模型局限】DL_P模型基于日线特征训练, 对30min/5min数据
#   预测值始终趋近于0(0.00-0.05), 无法用于次级别信号判定.
#   30min/5min级别的信号判定, 实际依赖:
#   - EP_L(反转概率): 有效检测趋势反转
#   - 几何判定: 二买/三买的严格几何确认
#   - 趋势方向回退: 无背驰信号时, 趋势方向up视为反转确认
#   - 长期方案: 需重训DL模型时加入30min级别训练数据
#
# BUG修复 (2026-08-08):
#   Fix A: 中阴状态机 — 一买确认后锁定L1buy, 切换状态, 屏蔽价格新低检查
#   Fix B: 级别错配 — 核心池准入从日线一买切换为30min一买确认
#   Fix C: EP_L缺陷 — 二买确认从EP_L概率改为几何判定(L2buy>L1buy)
#   Fix D: DL模型30min失效 — 次级别评分改用EP_L为主, DL_P*10为辅
#
# 候选池分层 (V5.2, 2026-08-08):
#   核心池: 30min一买确认 + 30min综合评分(EP_L为主)>=0.50 + 日线不在主跌段
#   观察池: 30min一买确认 + 30min综合评分(EP_L为主)>=0.35 + 日线不在主跌段
#   边缘池: 30min一买确认 + 30min综合评分(EP_L为主)>=0.20
# ============================================================

def detect_multilevel_buy_signals(code, price=None, zhongyin_day_count=0):
    """
    多级别二买/三买信号检测 V5.2 (2026-08-08)

    替代缠论"次级别同构性", 用几何判定+EP_L+多级别共振

    【重要: DL模型局限】
    DL_P模型基于日线特征训练, 对30min/5min数据预测值趋近于0.
    30min/5min信号判定实际依赖EP_L(反转概率)和几何判定.
    返回的30min_dl_p/5min_dl_p仅供参考, 不应作为决策依据.

    参数:
        code: 股票代码
        price: 当前价格(可选, None则用最新收盘价)
        zhongyin_day_count: 中阴状态持续天数(外部传入, 用于僵尸态超时检测)

    返回: {
        "code": code,
        "tier": "核心池"/"观察池"/"边缘池"/"无信号",
        "ermai": 二买信号 dict or None,
        "sanmai": 三买信号 dict or None,
        "daily_dl_p": 日线最佳一买DL_P,
        "daily_ep_p": 日线最佳一买EP_L,
        "daily_valid": 日线一买是否valid,
        "30min_dl_p": 30min最佳看多DL_P(近零,仅供参考),
        "30min_ep_p": 30min最佳看多EP_L(有效信号),
        "5min_dl_p": 5min最佳看多DL_P(近零,仅供参考),
        "5min_ep_p": 5min最佳看多EP_L(有效信号),
        "daily_dir": 日线趋势方向,
        "daily_ratio": 日线一买ratio,
        "levels_available": 可用级别列表,
    }
    """
    levels_to_check = ["日线", "30min", "5min"]
    level_results = {}

    for level in levels_to_check:
        try:
            r = analyze_beichi(code, level=level, price=price)
            if "error" not in r:
                level_results[level] = r
        except:
            pass

    daily = level_results.get("日线", {})
    min30 = level_results.get("30min", {})
    min5 = level_results.get("5min", {})

    # 日线一买信号(必须valid)
    daily_signals = daily.get("signals", [])
    daily_one_buys = [s for s in daily_signals if s.get("op") == "一买" and s.get("valid")]
    daily_dir = daily.get("overall_dir", "flat")

    # 日线最佳一买
    daily_best = max(daily_one_buys, key=lambda s: s.get("dl_prob", 0), default=None)
    daily_dl_p = daily_best.get("dl_prob", 0) if daily_best else 0
    daily_ep_p = daily_best.get("ep_prob", 0) if daily_best else 0
    daily_ratio = daily_best.get("ratio", 999) if daily_best else 999
    daily_valid = daily_best is not None and daily_best.get("valid", False)

    # ============================================================
    # 【Fix A: 中阴状态机】2026-08-08
    #
    # 问题: 一买需要价格新低+底背驰, 二买需要回调>一买低点
    #       当一买确认后, daily_valid因新低条件失效 → 二买前提崩塌
    #
    # 修复: 一买确认后进入中阴状态, 锁定L1buy价格和方向快照
    #       在中阴状态下, 二买检测不再依赖daily_valid
    #       退出条件: 价格跌破L1buy → 一买失效, 回到DOWN状态
    #
    # 状态迁移:
    #   DOWN: 寻找一买, 允许价格新低
    #   ZHONGYIN: 锁定L1buy, 屏蔽价格新低检查, 等待二买确认或破位
    #   SECOND_BUY_CONFIRMED: 二买几何确认, 进入上升段
    #   DOWN (回退): 价格跌破L1buy, 一买失效
    # ============================================================
    daily_dir_snapshot = daily_dir  # 一买确认时的方向快照
    daily_price = daily.get("price", 0)
    daily_C = daily.get("C", [])
    last_close = daily_C[-1] if daily_C else daily_price

    # 中阴状态初始化
    zhongyin_active = False       # 中阴状态是否激活
    one_buy_broken = False        # 是否已跌破一买低点
    zhongyin_state = "DOWN"       # 状态机: DOWN / ZHONGYIN / SECOND_BUY_CONFIRMED

    # 激活条件: 日线一买valid + 30min趋势已反转(进入中阴)
    min30_dir_raw = min30.get("overall_dir", "flat")
    if daily_valid and min30_dir_raw in ("up", "flat"):
        zhongyin_active = True
        zhongyin_state = "ZHONGYIN"

    # ============================================================
    # BUG修复 (2026-07-30): 提取一买低点用于加仓风控
    #
    # 问题: 二买加仓后, 若二买无法确认且价格破一买低点 → 重仓大幅回撤
    #       get_dynamic_position_cap需要one_buy_low来检查加仓安全性
    #       run_intraday_scan需要one_buy_low来做破位止损
    #
    # 来源优先级:
    #   1. 日线二买信号中已存储的one_buy_low (analyze_beichi计算)
    #   2. 从日线最佳一买信号的中枢计算最低收盘价
    # ============================================================
    daily_one_buy_low = None
    # 方式1: 从日线二买信号中获取(analyze_beichi已在line 1195存储)
    for s in daily_signals:
        if s.get("op") == "二买" and s.get("valid") and s.get("one_buy_low"):
            obl = s["one_buy_low"]
            if daily_one_buy_low is None or obl < daily_one_buy_low:
                daily_one_buy_low = obl
    # 方式2: 从日线最佳一买信号的中枢计算
    if daily_one_buy_low is None and daily_best:
        sig_zs = daily_best.get("zs", {})
        C_daily = daily.get("C", [])
        if sig_zs and C_daily:
            s_idx = sig_zs.get("s", 0)
            e_idx = sig_zs.get("e", 0)
            if 0 <= s_idx <= e_idx < len(C_daily):
                daily_one_buy_low = min(C_daily[s_idx:e_idx + 1])

    # 中阴状态: 检查是否破位 (价格跌破一买低点 → 一买失效)
    if daily_one_buy_low and daily_one_buy_low > 0 and last_close < daily_one_buy_low:
        one_buy_broken = True
        zhongyin_state = "DOWN"

    # 【5.5复核修复: 内部计算zhongyin_day_count】
    # 当调用方未传递zhongyin_day_count时(默认0), 从daily_C推算
    # 方法: 找到一买低点在daily_C中的位置, 计算至今的K线数
    if zhongyin_day_count == 0 and daily_one_buy_low and daily_one_buy_low > 0:
        if len(daily_C) >= 5:
            # 从后往前找, 找到第一个 <= L1buy 的收盘价位置
            for i in range(len(daily_C) - 1, -1, -1):
                if daily_C[i] <= daily_one_buy_low:
                    zhongyin_day_count = len(daily_C) - i - 1
                    break

    # ============================================================
    # 【Fix边界1: 中阴僵尸态】2026-08-08
    #
    # 问题: 价格在锁定L1buy后进入超长期窄幅横盘, 不破L1buy也不产生二买
    #       系统陷入"中阴僵尸态", 资金被无效占用 → 机会成本极高
    #
    # 修复:
    #   1. zhongyin_timeout: 中阴持续>=20交易日 → 强制降低priority
    #   2. 僵尸态标记: zhongyin_stale=True, 不影响其他股票扫描
    #   3. 恢复机制: 三买出现时自动恢复priority (外部调用方检查)
    # ============================================================
    zhongyin_timeout = False
    zhongyin_stale = False
    ZHONGYIN_DAYS_MAX = 20  # 最大中阴持续天数
    if zhongyin_state == "ZHONGYIN" and zhongyin_day_count >= ZHONGYIN_DAYS_MAX:
        zhongyin_timeout = True
        zhongyin_stale = True
        # 僵尸态: 降级eligible, 但不阻断破位检测
        if not one_buy_broken:
            zhongyin_active = False  # 临时冻结, 等待三买恢复

    # 30min信号(看多+看空)
    min30_signals = min30.get("signals", [])
    min30_buys = [s for s in min30_signals if s.get("dir") == "看多"]
    # 【Trae复核修复 2026-07-28】一鱼两吃缺陷
    # Codex方案: 加权评分 DL_P*0.4 + EP_L*0.6 选一个信号 → 一买和二买共用
    # 问题: 002454 DL=0.035/EP=0.089 和 DL=0.008/EP=0.591 是两个不同信号
    #   加权选中后者 → 30min DL_P=0.008 → 分层门槛0.6永远过不了
    #   一买分层被杀死, 二买也用的是低DL信号
    # 修复: 一买/二买各选各的最优信号
    #   一买分层用: max(DL_P) → 背驰结构最强的信号
    #   二买确认用: max(EP_L) → 反转概率最高的信号
    min30_best_dl = max(min30_buys, key=lambda s: s.get("dl_prob", 0), default=None)   # 一买侧重
    min30_best_ep = max(min30_buys, key=lambda s: s.get("ep_prob", 0), default=None)   # 二买侧重
    min30_dl_p = min30_best_dl.get("dl_prob", 0) if min30_best_dl else 0
    min30_ep_p = min30_best_ep.get("ep_prob", 0) if min30_best_ep else 0

    # 【BUG修复 2026-08-07】趋势反转回退: 30min无背驰信号但趋势已反转
    #
    # 问题: 背驰信号要求pre段和post段同方向(down→down或up→up)
    #       当趋势已反转(down→up)时, pre_d≠post_d, 不会产生新的背驰信号
    #       → min30_ep_p=0 → 二买条件2永远无法满足
    #
    # 修复: 当30min无看多信号但整体趋势已向上(overall_dir=up)时,
    #       认为反转已通过趋势方向确认, 赋予EP_L=0.6
    min30_reversal_by_trend = False
    if min30_ep_p == 0 and min30_dl_p == 0:
        min30_dir_raw = min30.get("overall_dir", "flat")
        if min30_dir_raw == "up":
            min30_ep_p = 0.6
            min30_reversal_by_trend = True

    min30_sells = [s for s in min30_signals if s.get("dir") == "看空"]
    min30_sell_count = len(min30_sells)
    min30_best_sell = max(min30_sells, key=lambda s: s.get("dl_prob", 0), default=None)
    min30_sell_dl_p = min30_best_sell.get("dl_prob", 0) if min30_best_sell else 0

    # 5min信号(看多+看空)
    min5_signals = min5.get("signals", [])
    min5_buys = [s for s in min5_signals if s.get("dir") == "看多"]
    # 【Trae复核修复 2026-07-28】同30min, 一买/二买各选各的
    min5_best_dl = max(min5_buys, key=lambda s: s.get("dl_prob", 0), default=None)   # 一买侧重
    min5_best_ep = max(min5_buys, key=lambda s: s.get("ep_prob", 0), default=None)   # 二买侧重
    min5_dl_p = min5_best_dl.get("dl_prob", 0) if min5_best_dl else 0
    min5_ep_p = min5_best_ep.get("ep_prob", 0) if min5_best_ep else 0

    # 【BUG修复 2026-08-07】同30min, 5min趋势反转回退
    min5_reversal_by_trend = False
    if min5_ep_p == 0 and min5_dl_p == 0:
        min5_dir_raw = min5.get("overall_dir", "flat")
        if min5_dir_raw == "up":
            min5_ep_p = 0.4  # 5min入场确认: 趋势反转
            min5_reversal_by_trend = True

    min5_sells = [s for s in min5_signals if s.get("dir") == "看空"]
    min5_sell_count = len(min5_sells)

    # 趋势矛盾检测: 日线dir vs 30min dir不一致
    min30_dir = min30.get("overall_dir", "flat")
    trend_conflict = (daily_dir != "flat" and min30_dir != "flat" and daily_dir != min30_dir)

    # ============================================================
    # 【Fix B + Fix D: 候选池分层 V5.2】2026-08-08
    # 准入: 30min一买确认 + EP_L/几何确认
    # 注意: DL模型对30min数据失效, 分层评分改用EP_L为主
    # 日线降级为过滤因子: 不在主跌段
    # ============================================================
    # 30min一买确认: 存在valid的一买信号
    min30_first_buy_valid = any(
        s.get("op") == "一买" and s.get("valid")
        for s in min30_signals
    )
    # ============================================================
    # 【Fix边界3修复: 日线过滤从硬拦截改为软过滤】2026-08-08
    #
    # 原问题: daily_dir != "down" 硬拦截当前市场73%的股票
    #   + MACD+MA20硬拦截剩余27% → 候选池几乎为空
    #
    # 修复策略:
    #   1. daily_dir=="down"时: 30min信号足够强(EP_L>=0.3)可豁免
    #   2. MACD+MA20降级为风险标记, 不作硬拦截
    #   3. 保留daily_filter_warning供分层降级参考
    # ============================================================
    daily_filter_ok = True
    daily_filter_warning = False

    if daily_dir == "down":
        # 日线向下: 需要30min信号足够强才能豁免
        # 【Mini复核修复: 增加min30_reversal_by_trend豁免】
        #   沃华医药: daily_dir=down, 30min dir=up(趋势反转), EP_L=0.600
        #   之前因为min30_first_buy_valid=False被拦截
        m30_exempt = min30_first_buy_valid or min30_reversal_by_trend or min30_ep_p >= 0.3
        if m30_exempt and (min30_ep_p >= 0.3 or min30_dl_p >= 0.10):
            daily_filter_ok = True
            daily_filter_warning = True  # 标记高风险, 不拦截
        else:
            daily_filter_ok = False
            # 日线向下且30min信号弱 → 硬拦截(主跌段中继风险高)

    # MACD+MA20降级为辅助标记, 不硬拦截
    if daily_filter_ok and len(daily_C) >= 20:
        daily_ma20 = _calc_ma(daily_C, 20)
        daily_last_close = daily_C[-1]
        try:
            _, _, macd_bar = calc_macd(daily_C)
            if len(macd_bar) >= 2:
                macd_slope_down = macd_bar[-1] < macd_bar[-2] and macd_bar[-1] < 0
            else:
                macd_slope_down = False
        except Exception:
            macd_slope_down = False
        if daily_last_close < daily_ma20 and macd_slope_down:
            daily_filter_warning = True  # 仅标记风险, 不拦截

    # ============================================================
    # 【Fix B修复 + Fix D: 30min分层阈值调整】2026-08-08
    #
    # 原问题: DL模型基于日线特征训练, 对30min数据预测值极低(0.00-0.05)
    #   DL_P阈值0.6/0.8对30min信号永远无法达到
    #
    # 修复: 使用EP_L(反转概率)作为30min主要指标
    #   EP_L模型对30min数据更有效(万科A EP_L=0.618)
    #   DL_P降级为辅助参考, 阈值降低10倍
    #
    # 【Fix D: DL模型30min失效 — 根本原因】
    #   DL_P模型训练时仅使用日线级别特征(MA5/MA10/MA20/MACD日线),
    #   对30min K线数据提取的特征分布完全不同, 模型无法泛化.
    #   30min趋势背驰确实存在, 但DL模型检测不到.
    #   真正有效的30min信号来源:
    #     1. EP_L(反转概率): 正确检测趋势反转
    #     2. 几何判定: 二买/三买严格几何确认(L2buy>L1buy等)
    #     3. 趋势方向回退: overall_dir=up视为反转确认
    #   长期方案: 重训DL模型时加入30min/5min级别训练数据,
    #     或独立训练次级别背驰模型, 彻底解决此问题.
    # ============================================================
    # 30min准入条件: 一买valid 或 趋势反转已确认(整体方向up) 或 EP_L足够强
    #   min30_reversal_by_trend: 30min无看多信号但整体趋势已向上
    #   沃华医药: 30min dir=up, EP_L=0.600(趋势反转), 但无valid一买
    #   万科A: 30min一买valid, EP_L=0.618
    # 【Mini复核修复: 增加min30_ep_p>=0.3准入, 覆盖EP_L>0但无信号标记的情况】
    min30_eligible_for_tier = min30_first_buy_valid or min30_reversal_by_trend or min30_ep_p >= 0.3

    # 30min综合评分: EP_L为主(模型适配30min), DL_P*10为辅(补偿日线模型偏差)
    #   注意: DL_P*10是临时补偿, 长期需重训模型
    min30_score = max(min30_ep_p, min30_dl_p * 10.0)

    tier = "无信号"
    if min30_eligible_for_tier and min30_score >= 0.20 and daily_filter_ok:
        if min30_score >= 0.50 and not daily_filter_warning:
            tier = "核心池"
        elif min30_score >= 0.35:
            tier = "观察池"
        elif min30_score >= 0.20:
            tier = "边缘池"
    # 备选: 日线DL_P极高(>=0.90)但30min无信号 → 边缘池
    # 台华新材: 日线DL_P=0.969, 30min无信号但整体趋势有结构
    if tier == "无信号" and daily_dl_p >= 0.90 and daily_valid:
        tier = "边缘池"

    # ============================================================
    # 【Fix C + Fix A: 二买 V5】2026-08-08
    #
    # 几何判定(L2buy>L1buy)为必要条件, EP_L降级为辅助排序
    # 中阴状态机: 一买确认后不依赖daily_valid, 直接使用几何判定
    # ============================================================
    ermai = None

    # 二买候选人: 中阴状态(一买确认+趋势反转) 或 日线一买valid
    ermai_eligible = zhongyin_active and not one_buy_broken
    if not ermai_eligible and daily_valid:
        ermai_eligible = True

    if ermai_eligible and daily_one_buy_low and daily_one_buy_low > 0:
        # 提取几何数据
        L1 = daily_one_buy_low

        # L2buy: 30min近期最低价(回调段低点)
        min30_C = min30.get("C", [])
        min30_price = min30.get("price", 0)
        L2 = min(min30_C[-20:]) if len(min30_C) >= 20 else min30_price

        # H1rebound: 30min近40根K线最高价(反弹段高点)
        H1 = max(min30_C[-40:]) if len(min30_C) >= 40 else 0

        # 【Fix边界2: 次级别假突破过滤】计算回撤段K线数量和振幅
        # 5.5复核修复: 使用实际回撤段长度(从H1位置到当前), 而非固定20窗口
        pullback_klines = 0
        pullback_amp_pct = 0.0
        if len(min30_C) >= 5:
            # 计算实际回撤段: 从H1(反弹高点)位置到当前
            lookback = min(40, len(min30_C))
            recent_40 = min30_C[-lookback:]
            h1_val = max(recent_40)
            try:
                h1_offset = recent_40.index(h1_val)
                h1_idx = len(min30_C) - lookback + h1_offset
                pullback_segment = min30_C[h1_idx:]
                pullback_klines = len(pullback_segment)
                pullback_high = max(pullback_segment)
                pullback_low_val = min(pullback_segment)
            except (ValueError, IndexError):
                # 回退: 使用最后20根K线
                pullback_segment = min30_C[-20:]
                pullback_klines = len(pullback_segment)
                pullback_high = max(pullback_segment)
                pullback_low_val = min(pullback_segment)
            if pullback_low_val > 0:
                pullback_amp_pct = (pullback_high - pullback_low_val) / pullback_low_val * 100

        # 几何判定(含次级别完备性校验)
        geo_result = evaluate_second_buy_sell(
            first_point_price=L1,
            rebound_high=H1,
            pullback_low=L2,
            signal_type="second_buy",
            pullback_klines=pullback_klines,
            pullback_amp_pct=pullback_amp_pct,
            min_width=5,
            min_amp=0.08,
        )
        geometric_pass = geo_result["is_confirmed"]

        # 5min入场确认
        entry_ok = min5_ep_p >= 0.3 or min5_dl_p >= 0.4 or min5_reversal_by_trend

        if geometric_pass and entry_ok:
            # 综合评分: 几何强度 + EP_L辅助
            cushion = geo_result["cushion"]
            geometric_score = min(1.0, cushion / (L1 * 0.02 + 0.01))
            score = geometric_score * 0.6 + min30_ep_p * 0.25 + min5_ep_p * 0.15

            gold_min = geo_result.get("optimal_buy_min", 0)
            gold_max = geo_result.get("optimal_buy_max", 0)

            # 中阴状态推进
            zhongyin_state = "SECOND_BUY_CONFIRMED"

            ermai = {
                "valid": True,
                "dl_prob": daily_dl_p,
                "ermai_dl_prob": round(score, 4),
                "ep_prob": (min30_ep_p + min5_ep_p) / 2,
                "confirm_method": "几何判定",
                "op": "二买",
                "geometric": {
                    "L1buy": L1,
                    "L2buy": L2,
                    "H1rebound": H1,
                    "cushion": cushion,
                    "cushion_ratio": round(cushion / L1, 4) if L1 else 0,
                    "geometric_pass": True,
                    "status": geo_result["status"],
                    "gold_min": gold_min,
                    "gold_max": gold_max,
                    "invalid_boundary": L1,
                },
                "daily_dl_p": daily_dl_p,
                "daily_ep_p": daily_ep_p,
                "30min_dl_p": min30_dl_p,
                "30min_ep_p": min30_ep_p,
                "5min_dl_p": min5_dl_p,
                "5min_ep_p": min5_ep_p,
                "ratio": daily_ratio,
                "levels_confirmed": 3,
                "reason": (f"二买几何确认: L2buy({L2:.2f})>L1buy({L1:.2f}) "
                          f"缓冲垫={cushion:.2f} 黄金区间[{gold_min:.2f},{gold_max:.2f}] "
                          f"失效边界={L1:.2f} | "
                          f"{'中阴状态' if zhongyin_active else '日线一买valid'}"),
            }

    # ============================================================
    # 三买: V6 (2026-08-08) 严格几何判定
    #
    # 缠论三买定义: 次级别(30min)走势向上突破中枢后,
    #   次级别回踩段最低点 > 中枢高点ZG
    #
    # 几何判定(third_buy_sell_judge.evaluate_third_buy_sell):
    #   1. 找到30min级别最后一个中枢(center={zg, zd})
    #   2. 离开段: 中枢结束后的上涨段(到最高价)
    #   3. 回踩段: 从最高价回落的K线
    #   4. 确认条件: l_pullback > zg (回踩不进入中枢)
    #
    # 修复: 替代旧版"级别共振"逻辑(仅检查方向, 无几何确认)
    #   旧版: daily_dir==up + 30min EP_L >= 0.3 → 三买
    #   新版: 30min中枢+回踩几何确认
    #   影响: 高争民爆(002827)旧版判三买, 新版正确拒绝
    # ============================================================
    sanmai = None
    if daily_dir == "up":
        # 获取30min级别K线数据和中枢
        min30_zss = min30.get("zss", [])
        min30_H_data = min30.get("H", [])
        min30_L_data = min30.get("L", [])
        min30_n = min30.get("n", 0)

        # 必须有至少1个中枢, 且K线数据完整
        if len(min30_zss) >= 1 and len(min30_H_data) >= 10 and len(min30_L_data) >= 10:
            last_center = min30_zss[-1]
            zg = last_center["zg"]
            zd = last_center["zd"]
            center_end = last_center["e"]

            # 离开段: 从中枢结束位置开始, 寻找最高点
            if center_end + 1 < min30_n:
                leave_highs = min30_H_data[center_end + 1:]
                leave_lows = min30_L_data[center_end + 1:]

                if leave_highs:
                    h_leave = max(leave_highs)
                    h_leave_rel_idx = leave_highs.index(h_leave)
                    h_leave_abs = center_end + 1 + h_leave_rel_idx

                    # 必须有离开段(最高点 > ZG)
                    if h_leave > zg:
                        leave_df = pd.DataFrame({
                            "high": min30_H_data[center_end + 1:h_leave_abs + 1],
                            "low": min30_L_data[center_end + 1:h_leave_abs + 1]
                        })

                        # 回踩段: 从最高点之后到最新
                        if h_leave_abs + 1 < min30_n:
                            pullback_df = pd.DataFrame({
                                "high": min30_H_data[h_leave_abs + 1:],
                                "low": min30_L_data[h_leave_abs + 1:]
                            })

                            # 次级别共振: 5min是否有入场信号
                            sub_resonance = (min5_ep_p >= 0.3 or min5_dl_p >= 0.4 or min5_reversal_by_trend)

                            try:
                                geo_result = evaluate_third_buy_sell(
                                    center={"zg": zg, "zd": zd},
                                    leave_segment=leave_df,
                                    pullback_segment=pullback_df,
                                    signal_type="third_buy",
                                    sub_level_resonance=sub_resonance
                                )

                                if geo_result["is_confirmed"]:
                                    # 额外安全过滤: 防止假三买
                                    #
                                    # 1. 回踩段必须至少有3根K线(结构完整性)
                                    #    高争民爆: 回踩仅2根K线, 同为54.16, 无真实回踩
                                    # 2. 缓冲垫比率必须>=0.5%
                                    #    防止ZG极高时微小的价格抖动被误判
                                    # 3. 30min有强卖点时(DL_P>0.5)拒绝三买
                                    #    卖点=主力出货, 三买=接盘
                                    pullback_bars = len(pullback_df)
                                    cushion_ratio = geo_result.get("cushion", 0) / max(zg, 0.01)
                                    sell_conflict = min30_sell_dl_p > 0.5

                                    if pullback_bars < 3:
                                        # 回踩段结构不完整, 拒绝三买
                                        pass
                                    elif cushion_ratio < 0.005:
                                        # 缓冲垫太薄, 价格抖动即可破位
                                        pass
                                    elif sell_conflict:
                                        # 30min有强卖点, 三买=接盘
                                        pass
                                    else:
                                        score = daily_dl_p * 0.5 + min30_score * 0.3 + min5_ep_p * 0.2
                                        sanmai = {
                                            "valid": True,
                                            "dl_prob": daily_dl_p,
                                            "ep_prob": (min30_ep_p + min5_ep_p) / 2,
                                            "confirm_method": "几何判定",
                                            "op": "三买",
                                            "daily_dl_p": daily_dl_p,
                                            "daily_ep_p": daily_ep_p,
                                            "30min_dl_p": min30_dl_p,
                                            "30min_ep_p": min30_ep_p,
                                            "5min_dl_p": min5_dl_p,
                                            "5min_ep_p": min5_ep_p,
                                            "ratio": daily_ratio,
                                            "levels_confirmed": 3,
                                            "geometric": geo_result,
                                            "reason": geo_result["reason"],
                                        }
                            except Exception:
                                # 几何判定异常, 不产生三买信号
                                pass

    # ============================================================
    # 【Fix 3: 二买/三买覆盖tier】2026-08-08
    #
    # 问题: 即使二买/三买几何确认, 因30min DL_P过低导致tier="无信号"
    #   丽珠集团: 二买已确认但tier=无信号(30min DL_P=0.000)
    #   万科A: 30min一买valid但tier=无信号(30min DL_P=0.028)
    #
    # 修复: 二买确认→至少观察池, 三买确认→核心池
    #   tier优先级: 核心池 > 观察池 > 边缘池 > 无信号
    # ============================================================
    _tier_rank = {"核心池": 3, "观察池": 2, "边缘池": 1, "无信号": 0}
    _current_rank = _tier_rank.get(tier, 0)

    if ermai and ermai.get("valid"):
        # 二买确认 → 至少观察池
        # 【Mini复核修复: 若daily_filter_ok=False, 二买最多观察池】
        #   丽珠集团: daily_dir=down, 30min无信号(EP=0, DL=0), 二买确认
        #   但日线过滤已拦截 → 二买只进观察池, 不进核心
        if daily_filter_ok:
            ermai_tier = "核心池" if not daily_filter_warning else "观察池"
        else:
            ermai_tier = "观察池"
        if _tier_rank.get(ermai_tier, 0) > _current_rank:
            tier = ermai_tier
            _current_rank = _tier_rank.get(tier, 0)

    if sanmai and sanmai.get("valid"):
        # 三买确认 → 核心池
        if _tier_rank.get("核心池", 3) > _current_rank:
            tier = "核心池"
            _current_rank = 3

    return {
        "code": code,
        "tier": tier,
        "ermai": ermai,
        "sanmai": sanmai,
        "daily_dl_p": daily_dl_p,
        "daily_ep_p": daily_ep_p,
        "daily_valid": daily_valid,
        "30min_dl_p": min30_dl_p,
        "30min_ep_p": min30_ep_p,
        "5min_dl_p": min5_dl_p,
        "5min_ep_p": min5_ep_p,
        "daily_dir": daily_dir,
        "daily_ratio": daily_ratio,
        "levels_available": list(level_results.keys()),
        # 看空信号信息
        "min30_sell_count": min30_sell_count,
        "min30_sell_dl_p": min30_sell_dl_p,
        "min5_sell_count": min5_sell_count,
        "min30_has_data": len(min30_signals) > 0 or "30min" in level_results,
        "min5_has_data": len(min5_signals) > 0 or "5min" in level_results,
        "min30_reversal_by_trend": min30_reversal_by_trend,
        "min5_reversal_by_trend": min5_reversal_by_trend,
        "trend_conflict": trend_conflict,
        "min30_dir": min30_dir,
        # BUG修复 (2026-07-30): 一买低点, 用于加仓风控和破位止损
        "one_buy_low": daily_one_buy_low,
        # 【Fix A: 中阴状态机】2026-08-08
        "zhongyin_state": zhongyin_state,
        "zhongyin_active": zhongyin_active,
        "one_buy_broken": one_buy_broken,
        # 【Fix边界1: 中阴僵尸态】2026-08-08
        "zhongyin_timeout": zhongyin_timeout,
        "zhongyin_stale": zhongyin_stale,
        "zhongyin_day_count": zhongyin_day_count,
        # 【Fix边界4: 仓位联动】中阴状态时SingleStockCap强制20%
        "single_stock_cap": 0.20 if zhongyin_active else 0.35,
        "zhongyin": detect_zhongyin(daily) if daily else {
            "is_zhongyin": False, "reason": "无日线数据",
            "action": "正常", "price_vs_zs": "未知",
        },
        # 【Fix B: 30min一买确认】
        "min30_first_buy_valid": min30_first_buy_valid,
        "daily_filter_ok": daily_filter_ok,
        "daily_filter_warning": daily_filter_warning,
        # P3 (2026-08-02): 最佳一买信号的综合置信度
        "daily_confidence": daily_best.get("confidence", 0) if daily_best else 0,
    }


def detect_sell_signals(code, cost, close):
    """
    持仓股卖出信号检测 V1 (2026-07-26)

    检测持仓股是否应减仓/清仓。
    与 detect_multilevel_buy_signals 互补: 后者只检测买点, 本函数检测卖点。

    参数:
        code: 股票代码
        cost: 持仓成本
        close: 当前价格

    返回: {
        "should_reduce": True/False,      # 是否建议减仓
        "should_clear": True/False,       # 是否建议清仓
        "reason": "...",
        "pnl_pct": 浮盈比例,
        "sell_signals": [信号列表],
        "risk_level": "高"/"中"/"低",
    }
    """
    pnl_pct = (close - cost) / cost if cost > 0 else 0

    # 调用多级别分析
    ml = detect_multilevel_buy_signals(code, price=close)

    daily_dir = ml.get("daily_dir", "flat")
    min30_dir = ml.get("min30_dir", "flat")
    min30_sell_count = ml.get("min30_sell_count", 0)
    min30_sell_dl_p = ml.get("min30_sell_dl_p", 0)
    min5_sell_count = ml.get("min5_sell_count", 0)
    trend_conflict = ml.get("trend_conflict", False)
    daily_dl_p = ml.get("daily_dl_p", 0)

    sell_signals = []
    should_reduce = False
    should_clear = False
    reasons = []
    risk_level = "低"

    # 规则1: 日线趋势down + 30min看空信号valid → 建议减仓
    if daily_dir == "down" and min30_sell_count > 0:
        should_reduce = True
        reasons.append(f"日线趋势down+30min看空({min30_sell_count}个)")
        risk_level = "中"
        sell_signals.append({
            "level": "30min", "op": "一卖", "dir": "看空",
            "count": min30_sell_count, "dl_p": min30_sell_dl_p,
        })

    # 规则2: 趋势矛盾(日线down vs 30min up) + 浮盈>10% → 建议减仓(反弹可能结束)
    if trend_conflict and daily_dir == "down" and pnl_pct > 0.10:
        should_reduce = True
        reasons.append(f"趋势矛盾(日down/30up)+浮盈{pnl_pct*100:.1f}%")
        risk_level = "中"

    # 规则3: 浮盈大幅回撤(从高点回撤>5%) → 建议减仓
    # 注: 需要历史高点数据, 此处用简化版

    # 规则4: 日线一买DL_P<0.4(弱信号) + 30min全看空 → 清仓
    if daily_dl_p < 0.4 and min30_sell_count >= 3 and pnl_pct > 0.05:
        should_clear = True
        should_reduce = True
        reasons.append(f"日线弱信号(DL_P={daily_dl_p:.2f})+30min密集看空({min30_sell_count}个)")
        risk_level = "高"

    # 规则5: 浮盈<0(亏损) + 日线趋势down → 止损清仓
    if pnl_pct < 0 and daily_dir == "down":
        should_clear = True
        should_reduce = True
        reasons.append(f"已亏损{pnl_pct*100:.1f}%+日线趋势down")
        risk_level = "高"

    # 规则6 (BUG修复 2026-07-30): 破一买低点 → 二买失败, 清仓
    # 核心: 二买加仓后若价格破一买低点, 说明二买确认失败, 趋势仍在下跌
    #       重仓持仓必须立即止损, 防止大幅回撤
    one_buy_low = ml.get("one_buy_low")
    if one_buy_low and one_buy_low > 0 and close < one_buy_low:
        should_clear = True
        should_reduce = True
        pct_below = ((close - one_buy_low) / one_buy_low) * 100
        reasons.append(f"破一买低点(一买低={one_buy_low:.2f}, 现价{close:.2f}, 跌{pct_below:+.1f}%)→二买失败")
        risk_level = "高"

    reason = "; ".join(reasons) if reasons else "无卖出信号"

    return {
        "code": code,
        "should_reduce": should_reduce,
        "should_clear": should_clear,
        "reason": reason,
        "pnl_pct": pnl_pct,
        "sell_signals": sell_signals,
        "risk_level": risk_level,
        "daily_dir": daily_dir,
        "min30_dir": min30_dir,
        "trend_conflict": trend_conflict,
    }


def get_signal_summary(result):
    """
    简明信号摘要 — 每个级别一句话
    返回: {
        "level": 级别,
        "signal": "一买"/"一卖"/"二买"/"二卖"/"三买"/"三卖"/"观望",
        "bias": "趋势背驰"/"盘整背驰"/"无背驰",
        "dir": "看多"/"看空"/"中性",
        "strength": 1-5,
        "one_line": 一句话摘要
    }
    """
    r = result
    if r.get('error'):
        return {"signal": "观望", "bias": "无数据", "dir": "中性",
                "strength": 0, "one_line": f"数据获取失败: {r['error']}"}

    dir_cn = {"up": "上涨", "down": "下跌", "flat": "震荡"}
    overall = dir_cn[r['overall_dir']]
    price = r.get('price', 0)
    cost = r.get('cost', 0)

    # 【深度学习】只取模型判定为背驰的有效信号(非"无背驰")
    valid = [s for s in r['signals']
             if s['valid'] and s.get('type', '') != "无背驰"]

    if valid:
        sig = valid[0]  # 取最强(最近中枢)信号
        # 强度基于深度学习概率
        dl_prob = sig.get('dl_prob', sig['ratio'] / 100.0)
        strength = 1
        if dl_prob >= 0.8:
            strength = 5
        elif dl_prob >= 0.7:
            strength = 4
        elif dl_prob >= 0.6:
            strength = 3
        elif dl_prob >= 0.4:
            strength = 2
        else:
            strength = 1

        pnl = ""
        if price and cost:
            p = ((price / cost) - 1) * 100
            pnl = f" | 浮盈{p:+.2f}%"

        sig_type = sig.get('type', '')
        ep_prob = sig.get('ep_prob', 0)
        ep_type = sig.get('ep_type', '')
        ep_str = f" | EP{ep_prob:.0%}" if ep_prob > 0 else ""
        one = (f"{sig['op']} | {sig_type} | DL{dl_prob:.0%}{ep_str} 面积比{sig['ratio']:.1f}%"
               f" | 大级别{overall}{pnl}")
        return {
            "signal": sig['op'], "bias": sig_type,
            "dir": sig['dir'], "strength": strength, "one_line": one
        }

    # 无有效信号
    zs = r['zss'][-1] if r['zss'] else None
    if zs:
        zg, zd = zs['zg'], zs['zd']
        pos = "中枢上沿" if price > zg else "中枢下沿" if price < zd else "中枢内"
        one = f"观望 | 无背驰 | 价{price}处于{pos}[{zd:.3f},{zg:.3f}]"
    else:
        one = f"观望 | 无背驰 | 无有效中枢"

    return {"signal": "观望", "bias": "无背驰",
            "dir": "中性", "strength": 0, "one_line": one}


def get_action_advice(summaries):
    """
    根据多级别信号汇总，给出操作建议
    优先级: 日线 > 30min > 5min > 1min
    """
    # 多级别共振检测
    bull_levels = [s for s in summaries if s['dir'] == '看多' and s['signal'] in ('一买', '二买', '三买')]
    bear_levels = [s for s in summaries if s['dir'] == '看空' and s['signal'] in ('一卖', '二卖', '三卖')]

    # 高级别(日线/30min)信号
    high_bull = [s for s in summaries[:2] if s['dir'] == '看多' and s['signal'] in ('一买', '二买', '三买')]
    high_bear = [s for s in summaries[:2] if s['dir'] == '看空' and s['signal'] in ('一卖', '二卖', '三卖')]

    if len(bull_levels) >= 2:
        strength = min(s['strength'] for s in bull_levels)
        names = '/'.join('日线' if i == 0 else '30min' if i == 1 else '5min' if i == 2 else '1min'
                         for i, s in enumerate(summaries)
                         if s['dir'] == '看多' and s['signal'] in ('一买', '二买', '三买'))
        return f"【多级别共振看多】{names}同时出现买点 → 建议加仓 | 强度{strength}/5"

    if len(bear_levels) >= 2:
        strength = min(s['strength'] for s in bear_levels)
        names = '/'.join('日线' if i == 0 else '30min' if i == 1 else '5min' if i == 2 else '1min'
                         for i, s in enumerate(summaries)
                         if s['dir'] == '看空' and s['signal'] in ('一卖', '二卖', '三卖'))
        return f"【多级别共振看空】{names}同时出现卖点 → 建议减仓 | 强度{strength}/5"

    if high_bull:
        sig = high_bull[0]
        return f"【高级别看多】{sig['signal']} → 可逢低买入 | 强度{sig['strength']}/5"

    if high_bear:
        sig = high_bear[0]
        return f"【高级别看空】{sig['signal']} → 建议减仓 | 强度{sig['strength']}/5"

    low_bull = [s for s in summaries[2:] if s['signal'] in ('一买', '二买', '三买')]
    low_bear = [s for s in summaries[2:] if s['signal'] in ('一卖', '二卖', '三卖')]
    if low_bull:
        return f"【低级别买点】{low_bull[0]['signal']} → 可轻仓短线 | 强度{low_bull[0]['strength']}/5"
    if low_bear:
        return f"【低级别卖点】{low_bear[0]['signal']} → 短线注意 | 强度{low_bear[0]['strength']}/5"

    return "【无明确信号】所有级别均无背驰 → 持仓观望"


def print_simple_report(result):
    """简明输出 — 一行信号 + 中枢位置"""
    summary = get_signal_summary(result)
    r = result
    if r.get('error'):
        print(f"  {summary['one_line']}")
        return

    level = r['level']
    dir_cn = {"up": "↑", "down": "↓", "flat": "→"}
    arrow = dir_cn.get(r['overall_dir'], "→")

    strength_bar = "█" * summary['strength'] + "░" * (5 - summary['strength'])
    print(f"  {arrow} {level:4s} | {summary['one_line']} | [{strength_bar}]")

    # 中枢关键价位
    if r['zss']:
        zs = r['zss'][-1]
        print(f"        中枢 [{zs['zd']:.3f}, {zs['zg']:.3f}]")


def print_beichi_result(result):
    """完整输出 (保留用于调试)"""
    r = result
    if r.get('error'):
        print(f"  错误: {r['error']}")
        return

    dir_cn = {"up": "上涨", "down": "下跌", "flat": "震荡"}
    print(f"\n  大级别方向(最近60根): {dir_cn[r['overall_dir']]} ({r['overall_pct']:.2f}%)")
    print(f"  → 只检测{dir_cn[r['overall_dir']]}背驰，过滤反向伪信号")

    if r['zss']:
        zs = r['zss'][-1]
        print(f"\n  最新中枢: [{zs['zd']:.3f}, {zs['zg']:.3f}] 宽{zs['w']}bar")

    valid_signals = [s for s in r['signals'] if s['valid']]
    all_signals = [s for s in r['signals'] if s['aligned']]

    if valid_signals:
        print(f"\n  ── 有效背驰信号 ──")
        for sig in valid_signals[:2]:
            ic = "🟢" if sig['dir'] == "看多" else "🔴"
            print(f"  {ic} {sig['type']} | {sig['op']} | "
                  f"{sig['dir']} | 面积比{sig['ratio']:.1f}%")
            print(f"     前段: {sig['pre_range']} ({sig['pre_reason']})")
            print(f"     后段: {sig['post_range']} ({sig['post_reason']})")
    elif all_signals:
        print(f"\n  ── 方向一致信号(但幅度不足) ──")
        for sig in all_signals[:2]:
            print(f"  {sig['type']} | {sig['op']} | 面积比{sig['ratio']:.1f}%")
            if not sig['pre_ok']:
                print(f"    前段无效: {sig['pre_reason']}")
            if not sig['post_ok']:
                print(f"    后段无效: {sig['post_reason']}")
    else:
        print(f"\n  🟡 无有效背驰信号")

    reversed_sig = [s for s in r['signals'] if not s['aligned']]
    if reversed_sig:
        print(f"\n  ── 已过滤的反向伪信号 ──")
        for sig in reversed_sig[:1]:
            print(f"  ✗ {sig['type']} {sig['op']} (方向{sig['pre_dir']}, "
                  f"大级别{sig['overall_dir']} → 已过滤)")


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "simple"
    # mode: "simple" = 简明版, "full" = 完整版, "code" = 只分析指定股票

    print("=" * 60)
    print("  缠论背驰分析器 v2.0 (简明信号版)")
    print("=" * 60)

    holdings = {
        "600006": {"name": "东风股份", "cost": 8.711},
        "600900": {"name": "长江电力", "cost": 27.530},
    }

    # 如果指定了code参数
    if len(sys.argv) > 2:
        target_codes = [c for c in sys.argv[2:] if c in holdings]
    else:
        target_codes = list(holdings.keys())

    for code in target_codes:
        info = holdings[code]
        price = fetch_realtime_tencent(code)
        pnl = ((price / info['cost']) - 1) * 100

        summaries = []
        for level in ["日线", "30min", "5min", "1min"]:
            result = analyze_beichi(code, level, price, info['cost'])
            summaries.append(get_signal_summary(result))

        if mode == "simple":
            print(f"\n  {code} {info['name']}  {price}  {pnl:+.2f}%")
            print(f"  {'─' * 50}")
            for i, s in enumerate(summaries):
                lvl = ["日线", "30min", "5min", "1min"][i]
                dir_arrow = {"看多": "↑", "看空": "↓", "中性": "→"}.get(s['dir'], "→")
                bar = "█" * s['strength'] + "░" * (5 - s['strength'])
                print(f"  {dir_arrow} {lvl:4s} | {s['one_line']} | [{bar}]")

            advice = get_action_advice(summaries)
            print(f"  {'─' * 50}")
            print(f"  ➤ {advice}")
        else:
            print(f"\n{'━' * 58}")
            print(f"  {code} {info['name']}  现价{price}  "
                  f"成本{info['cost']}  收益{pnl:+.2f}%")
            print(f"{'━' * 58}")
            for level in ["日线", "30min", "5min", "1min"]:
                print(f"\n  【{level}】")
                result = analyze_beichi(code, level, price, info['cost'])
                print_beichi_result(result)
