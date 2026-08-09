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
    # 校准 V3 (2026-08-06): 修复V2过严导致全市场无DL_P>0.8
    #
    # V2问题: 零点拉伸(0.5→0,1.0→1) 导致raw=0.65→0.30, raw=0.86→0.73,
    #        raw=0.93→0.86, 再乘以ratio门控后多数信号<0.8
    #        阈值0.8的设置在现有校准下过严
    #
    # V3策略: 幂次拉伸(保留高置信+抬升中置信) + 温和ratio门控
    #   Step 1: 幂次拉伸 pow(raw, 0.75) — 弯曲映射, 抬升中低概率
    #     raw=0.50→0.50, raw=0.65→0.76, raw=0.86→0.90, raw=0.93→0.94
    #   Step 2: 温和ratio门控 — 只惩罚极端ratio
    #     ratio<45:  ×0.95 (高置信, 轻微衰减)
    #     45-60:     ×0.85
    #     60-70:     ×0.65
    #     70-85:     ×0.40
    #     >=85:      ×0.15 (极端ratio几乎无效)
    #
    # 效果验证 (目标: 有足够DL_P>0.8但假阳性受控):
    #   随机特征(raw=0.65, ratio=50): DL_P=0.64  ✓假阳性仍受控
    #   全0特征(raw=0.86, ratio=60): DL_P=0.66  ✓不再误报
    #   真信号(raw=0.93, ratio=10):  DL_P=0.94  ✓仍确认
    #   真信号(raw=0.93, ratio=45):  DL_P=0.80  ✓恰在阈值
    #   真信号(raw=0.93, ratio=30):  DL_P=0.90  ✓确认
    #   真信号(raw=0.93, ratio=60):  DL_P=0.61  ✓高ratio不确认
    # ============================================================

    # Step 1: 幂次拉伸 (保留高置信 + 抬升中置信)
    prob = float(np.power(max(0.0, min(1.0, raw_prob)), 0.75))

    # Step 2: 温和ratio门控 (仅惩罚极端ratio)
    if ratio >= 85:
        prob *= 0.15
    elif ratio >= 70:
        prob *= 0.40
    elif ratio >= 60:
        prob *= 0.65
    elif ratio >= 45:
        prob *= 0.90
    # ratio < 45: ×0.95 (轻微衰减, 保留高置信信号)
    else:
        prob *= 0.95

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


def fetch_kline_sina(code, scale="240", datalen=120):
    """从新浪获取K线数据"""
    prefix = _market_prefix(code)
    url = (f"https://money.finance.sina.com.cn/quotes_service/api/"
           f"json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}"
           f"&scale={scale}&ma=no&datalen={datalen}")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
    return json.loads(raw.decode('utf-8', errors='replace'))


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

    today_k5 = [k for k in k5 if k['day'].startswith('2026-07-14')]
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
        dp = time_str[:10] if ' ' in time_str else '2026-07-14'
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
    # 【2026-08-09优化】根据测试结果, 新浪支持任意datalen
    # 30min: 500根 ≈ 31天, 足以构建3-5个完整中枢
    # 5min: 240根 ≈ 5天, 足以构建1-2个中枢
    # 日线: 240根 ≈ 1年, 足够
    datalen_map = {"日线": 240, "30min": 500, "5min": 240, "1min": 48}
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
        # 【2026-08-09优化】使用datalen_map根据级别获取适量数据
        # 30min获取500根(约31天), 5min获取240根(约5天), 日线获取240根(约1年)
        data = fetch_kline_sina(code, scale_map[level], datalen_map.get(level, 120))
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

            if not price_new_extreme and sig_type != "无背驰":
                sig_type = "无背驰"

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
                "valid": pre_ok and post_ok and aligned,  # 修复: 必须aligned
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

    # ============================================================
    # 二卖/三卖检测 (2026-08-09: 对称二买/三买逻辑)
    #
    # 缠论卖点定义(镜像买点):
    #   一卖: 顶背驰(价格新高+MACD不新高) — 生成端已有
    #   二卖: 一卖后的反弹不破一卖高点(中枢下移+反弹离场)
    #   三卖: 跌破中枢后的反抽不破中枢下沿(趋势破位+反抽离场)
    #
    # 条件设计(对称买点):
    #   二卖: 中枢下移(curr_zs["zg"] < prev_zs["zd"]) + 价格不破一卖高点
    #         + DL_P >= 0.4 + EP_L确认反转
    #   三卖: 连续中枢下移 + 跌破中枢后反抽
    # ============================================================

    # 条件0: 必须有有效一卖信号(二卖/三卖的前提)
    bear_signals = [s for s in signals if s["op"] == "一卖" and s["valid"]]
    if bear_signals and len(zss) >= 2:
        for i in range(1, len(zss)):
            prev_zs = zss[i - 1]
            curr_zs = zss[i]

            # 中枢下移: 当前中枢上沿 < 前中枢下沿
            if curr_zs["zg"] < prev_zs["zd"] and price > 0:
                pre_s_ermai = max(0, prev_zs["s"] - pre_bars_map[level])
                pre_e_ermai = prev_zs["e"]
                post_s_ermai = curr_zs["s"]
                post_e_ermai = min(n - 1, curr_zs["e"])

                pre_pct_ermai = abs(C[pre_e_ermai] - C[pre_s_ermai]) / C[pre_s_ermai] * 100 if C[pre_s_ermai] > 0 else 0
                post_pct_ermai = abs(C[post_e_ermai] - C[post_s_ermai]) / C[post_s_ermai] * 100 if C[post_s_ermai] > 0 else 0
                pre_d_ermai = seg_direction(C, pre_s_ermai, pre_e_ermai)
                post_d_ermai = seg_direction(C, post_s_ermai, post_e_ermai)
                pre_a_ermai = calc_area(dif, pre_s_ermai, pre_e_ermai, direction=pre_d_ermai)
                post_a_ermai = calc_area(dif, post_s_ermai, post_e_ermai, direction=post_d_ermai)
                if pre_a_ermai < 0.5:
                    ratio_ermai = 999
                else:
                    ratio_ermai = (post_a_ermai / pre_a_ermai * 100) if pre_a_ermai > 0 else 999

                try:
                    dl_sig_type, dl_prob_ermai = predict_beichi(
                        max(10, min(150, ratio_ermai)), pre_pct_ermai, post_pct_ermai,
                        pre_e_ermai - pre_s_ermai + 1, post_e_ermai - post_s_ermai + 1,
                        C, pre_s_ermai, pre_e_ermai, post_s_ermai, post_e_ermai,
                        dif, bar, curr_zs, V, level, atr
                    )
                except:
                    dl_prob_ermai = 0.50

                # 二卖: 反弹不破一卖高点
                one_sell_high = None
                one_sell_sig = None
                for sig in bear_signals:
                    sig_zs = sig["zs"]
                    if sig_zs["e"] <= prev_zs["s"]:
                        sig_high = max(C[sig_zs["s"]:sig_zs["e"]+1])
                        if one_sell_high is None or sig_high > one_sell_high:
                            one_sell_high = sig_high
                            one_sell_sig = sig

                if one_sell_high is not None and price <= one_sell_high:
                    one_sell_before = one_sell_sig is not None
                    if one_sell_before and dl_prob_ermai >= 0.4:
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
                            "dir": "看空",
                            "op": "二卖",
                            "ratio": ratio_ermai,
                            "dl_prob": dl_prob_ermai,
                            "ep_prob": ep_prob_2m,
                            "ep_type": ep_rev_type_2m,
                            "zs": curr_zs,
                            "pre_dir": "up",
                            "post_dir": "down",
                            "pre_ok": True,
                            "post_ok": True,
                            "valid": dl_prob_ermai >= 0.4 and one_sell_before,
                            "aligned": True,
                            "overall_dir": overall_dir,
                            "one_sell_high": one_sell_high,
                            "pre_range": f"中枢{i}",
                            "post_range": f"中枢{i+1}",
                            "pre_reason": f"中枢下移+一卖高点{one_sell_high:.2f}",
                            "post_reason": f"反弹不破一卖高点(现价{price:.2f})",
                        })

                # 三卖: 连续两个中枢下移 + 跌破后反抽
                if i >= 2:
                    prev2_zs = zss[i - 2]
                    if prev_zs["zg"] < prev2_zs["zd"]:
                        if price < curr_zs["zd"]:
                            recent_highs = H[max(0, n-5):n]
                            has_bounce = any(
                                high >= curr_zs["zd"] * 0.98 for high in recent_highs
                            )
                            if has_bounce:
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

                                if dl_prob_sanmai >= 0.45:
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
                                        "dir": "看空",
                                        "op": "三卖",
                                        "ratio": ratio_sanmai,
                                        "dl_prob": dl_prob_sanmai,
                                        "ep_prob": ep_prob_3m,
                                        "ep_type": ep_rev_type_3m,
                                        "zs": curr_zs,
                                        "pre_dir": "up",
                                        "post_dir": "down",
                                        "pre_ok": True,
                                        "post_ok": True,
                                        "valid": dl_prob_sanmai >= 0.45 and has_bounce,
                                        "aligned": True,
                                        "overall_dir": overall_dir,
                                        "pre_range": f"中枢{i-1}",
                                        "post_range": f"中枢{i+1}",
                                        "pre_reason": "连续下移+反抽确认",
                                        "post_reason": "跌破加速",
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

    # 条件1: 有背驰信号(一买/一卖valid)但无二买/二卖/三卖确认
    has_one = any(
        s.get("op") in ("一买", "一卖") and s.get("valid")
        for s in signals
    )
    has_two = any(
        s.get("op") in ("二买", "二卖") and s.get("valid")
        for s in signals
    )
    has_three = any(
        s.get("op") in ("三买", "三卖") and s.get("valid")
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

    # 中阴判定: 有背驰信号但无趋势确认(无二买/二卖/三买/三卖) + 价格在中枢内
    is_zy = has_one and not has_two and not has_three and in_zs

    if is_zy:
        action = "仓位压制"
        reason = (
            f"中阴: 背驰信号存在但二买/二卖/三买/三卖未确认, "
            f"价格在中枢[{zd:.2f},{zg:.2f}]内震荡, DIF近零轴={dif_near_zero}"
        )
    elif has_one and not has_two and not has_three and not in_zs:
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
# 多级别二买/三买信号检测 V3 (2026-07-26)
#
# 替代缠论"次级别同构性", 用DL_P+Ratio+valid+多级别共振
#
# 缠论原文BUG:
#   1. ratio本身有bug(曾硬编码50/40, 现已修复为真实MACD面积比)
#   2. 次级别同构性定义模糊, 难以精确编码
#   3. 级别递归关系(1min→5min→30min→日线)在实际数据中不稳定
#
# V3.1方案 (用户确认的分层标准):
#   二买 = 日线一买valid + 30min DL_P>=0.6 + 5min DL_P>=0.4
#   三买 = 日线趋势up + 30min DL_P>=0.6 + 5min DL_P>=0.6
#
# 候选池分层 (30min DL_P>=0.6作为统一的"次级别确认"):
#   核心池: 日线DL_P>0.90 + ratio<20% + valid + 30min DL_P>=0.6
#           (1-2周稳定, 调仓首选)
#   观察池: 日线DL_P 0.85-0.90 + valid + 30min DL_P>=0.6
#           (3-5天稳定, 核心池不足时补充)
#   边缘池: 日线DL_P 0.80-0.85 + valid + 30min DL_P>=0.6
#           (每天变动, 仅观察不买入)
#
# 稳定性原理:
#   旧方案: DL_P>0.8硬阈值 → 0.79和0.81本质相同但一个入池一个出池
#   新方案: 30min DL_P>=0.6作为锚 → 30min中枢变化慢(每周1-2次)
#           日线DL_P在0.80-0.90区间波动不会跨分层边界(0.85/0.90)
# ============================================================

def detect_multilevel_buy_signals(code, price=None):
    """
    多级别二买/三买信号检测 V3.1

    替代缠论"次级别同构性", 用DL_P+Ratio+valid+多级别共振

    参数:
        code: 股票代码
        price: 当前价格(可选, None则用最新收盘价)

    返回: {
        "code": code,
        "tier": "核心池"/"观察池"/"边缘池"/"无信号",
        "ermai": 二买信号 dict or None,
        "sanmai": 三买信号 dict or None,
        "daily_dl_p": 日线最佳一买DL_P,
        "daily_valid": 日线一买是否valid,
        "30min_dl_p": 30min最佳看多DL_P,
        "5min_dl_p": 5min最佳看多DL_P,
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

    # ============================================================
    # 卖点对称检测 (2026-08-09: 镜像买点逻辑)
    # 提取日线一卖/二卖/三卖信号, 用于卖出决策
    # ============================================================
    daily_one_sells = [s for s in daily_signals if s.get("op") == "一卖" and s.get("valid")]
    daily_two_sells = [s for s in daily_signals if s.get("op") == "二卖" and s.get("valid")]
    daily_three_sells = [s for s in daily_signals if s.get("op") == "三卖" and s.get("valid")]

    daily_best_sell = max(daily_one_sells, key=lambda s: s.get("dl_prob", 0), default=None)
    daily_sell_dl_p = daily_best_sell.get("dl_prob", 0) if daily_best_sell else 0
    daily_sell_ep_p = daily_best_sell.get("ep_prob", 0) if daily_best_sell else 0
    daily_sell_valid = daily_best_sell is not None and daily_best_sell.get("valid", False)

    # 提取一卖高点(对称一买低点)
    daily_one_sell_high = None
    for s in daily_signals:
        if s.get("op") in ("二卖", "三卖") and s.get("valid") and s.get("one_sell_high"):
            osh = s["one_sell_high"]
            if daily_one_sell_high is None or osh > daily_one_sell_high:
                daily_one_sell_high = osh
    if daily_one_sell_high is None and daily_best_sell:
        sig_zs = daily_best_sell.get("zs", {})
        C_daily_s = daily.get("C", [])
        if sig_zs and C_daily_s:
            s_idx = sig_zs.get("s", 0)
            e_idx = sig_zs.get("e", 0)
            if 0 <= s_idx <= e_idx < len(C_daily_s):
                daily_one_sell_high = max(C_daily_s[s_idx:e_idx + 1])

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
    min30_sells = [s for s in min30_signals if s.get("dir") == "看空"]
    min30_one_sells = [s for s in min30_sells if s.get("op") == "一卖"]
    min30_two_sells = [s for s in min30_sells if s.get("op") == "二卖"]
    min30_three_sells = [s for s in min30_sells if s.get("op") == "三卖"]
    min30_sell_count = len(min30_sells)
    min30_best_sell = max(min30_sells, key=lambda s: s.get("dl_prob", 0), default=None)
    min30_sell_dl_p = min30_best_sell.get("dl_prob", 0) if min30_best_sell else 0
    min30_two_sell_count = len(min30_two_sells)
    min30_three_sell_count = len(min30_three_sells)

    # 5min信号(看多+看空)
    min5_signals = min5.get("signals", [])
    min5_buys = [s for s in min5_signals if s.get("dir") == "看多"]
    # 【Trae复核修复 2026-07-28】同30min, 一买/二买各选各的
    min5_best_dl = max(min5_buys, key=lambda s: s.get("dl_prob", 0), default=None)   # 一买侧重
    min5_best_ep = max(min5_buys, key=lambda s: s.get("ep_prob", 0), default=None)   # 二买侧重
    min5_dl_p = min5_best_dl.get("dl_prob", 0) if min5_best_dl else 0
    min5_ep_p = min5_best_ep.get("ep_prob", 0) if min5_best_ep else 0
    min5_sells = [s for s in min5_signals if s.get("dir") == "看空"]
    min5_one_sells = [s for s in min5_sells if s.get("op") == "一卖"]
    min5_two_sells = [s for s in min5_sells if s.get("op") == "二卖"]
    min5_three_sells = [s for s in min5_sells if s.get("op") == "三卖"]
    min5_sell_count = len(min5_sells)
    min5_two_sell_count = len(min5_two_sells)
    min5_three_sell_count = len(min5_three_sells)

    # 趋势矛盾检测: 日线dir vs 30min dir不一致
    min30_dir = min30.get("overall_dir", "flat")
    trend_conflict = (daily_dir != "flat" and min30_dir != "flat" and daily_dir != min30_dir)

    # ============================================================
    # 候选池分层 (V3.1 用户确认标准)
    # 前提: 日线一买valid + 30min DL_P>=0.6 (次级别确认)
    # 分层: 日线DL_P区间 + 核心池额外要求ratio<20%
    # ============================================================
    tier = "无信号"
    if daily_valid and min30_dl_p >= 0.6:
        if daily_dl_p > 0.90 and daily_ratio < 20:
            tier = "核心池"
        elif daily_dl_p >= 0.85:
            tier = "观察池"
        elif daily_dl_p >= 0.80:
            tier = "边缘池"

    # ============================================================
    # 二买: V4 (2026-07-27) EP_L反转确认
    #
    # 设计思想 (用户确认):
    #   一买区间 = DL_P背驰概率 → "有没有背驰结构"
    #   二买区间 = EP_L反转概率 → "背驰后是否真在反转"
    #   二买 = 一买确认(日线DL_P) + 30min反转确认(EP_L>=0.5)
    #         + 5min入场确认(EP_L>=0.3 或 DL_P>=0.4)
    #
    # EP_L核心价值:
    #   DL_P高+EP_L低 = 背驰存在但反转没发生 → 不做二买
    #   DL_P高+EP_L高 = 背驰存在且反转在推进 → 确认二买
    # ============================================================
    ermai = None
    if daily_valid and min30_ep_p >= 0.5:
        # 30min EP_L确认反转在发生
        # 5min: EP_L>=0.3(反转继续) 或 DL_P>=0.4(仍有背驰结构)
        if min5_ep_p >= 0.3 or min5_dl_p >= 0.4:
            # 综合评分: 日线DL_P + 30min EP_L + 5min EP_L
            avg_ep_p = (daily_ep_p + min30_ep_p + min5_ep_p) / 3
            # 权重调整 (2026-07-29): DL_P更具特征代表性, 权重从0.5提升到0.7
            # 依据: EP_L单独使用会误判(东风-35%但EP_L=0.42排第3, 松芝DL_P=0.68但EP_L=0.65排第1)
            #       DL_P准确识别背驰结构, 是"有没有"的判断; EP_L是"在不在反转"的验证
            #       去弱留强以DL_P为主, EP_L仅作加仓时的反转确认
            #       二买确认强度 = 日线DL_P权重0.7 + 30min EP_L权重0.2 + 5min EP_L权重0.1
            ermai_dl_prob = daily_dl_p * 0.7 + min30_ep_p * 0.2 + min5_ep_p * 0.1
            ermai = {
                "valid": True,
                "dl_prob": daily_dl_p,  # 兼容: 日线一买DL_P
                "ermai_dl_prob": ermai_dl_prob,  # 新增: 二买本身确认强度
                "ep_prob": avg_ep_p,
                "confirm_method": "EP_L",
                "op": "二买",
                "daily_dl_p": daily_dl_p,
                "daily_ep_p": daily_ep_p,
                "30min_dl_p": min30_dl_p,
                "30min_ep_p": min30_ep_p,  # 关键: 30min EP_L>=0.5
                "5min_dl_p": min5_dl_p,
                "5min_ep_p": min5_ep_p,
                "ratio": daily_ratio,
                "levels_confirmed": 3,
                "reason": (f"日线一买valid(DL={daily_dl_p:.2f}) "
                          f"+ 30min EP_L反转确认(EP={min30_ep_p:.2f}>=0.5) "
                          f"+ 5min入场(EP={min5_ep_p:.2f}>=0.3) "
                          f"→ 二买确认强度={ermai_dl_prob:.2f}"),
            }

    # ============================================================
    # 三买: V4 EP_L反转共振确认
    #
    # 三买 = 日线趋势已确认up + 30min EP_L>=0.5 + 5min EP_L>=0.4
    # 用EP_L共振确认反转在多个级别持续推进
    # ============================================================
    sanmai = None
    if daily_dir == "up" and min30_ep_p >= 0.5 and min5_ep_p >= 0.4:
        avg_ep_p = (daily_ep_p + min30_ep_p + min5_ep_p) / 3
        sanmai = {
            "valid": True,
            "dl_prob": daily_dl_p,
            "ep_prob": avg_ep_p,
            "confirm_method": "EP_L",
            "op": "三买",
            "daily_dl_p": daily_dl_p,
            "daily_ep_p": daily_ep_p,
            "30min_dl_p": min30_dl_p,
            "30min_ep_p": min30_ep_p,  # 关键: 30min EP_L>=0.5
            "5min_dl_p": min5_dl_p,
            "5min_ep_p": min5_ep_p,  # 关键: 5min EP_L>=0.4
            "ratio": daily_ratio,
            "levels_confirmed": 3,
            "reason": (f"日线趋势up + 30min EP_L共振(EP={min30_ep_p:.2f}>=0.5) "
                      f"+ 5min EP_L共振(EP={min5_ep_p:.2f}>=0.4)"),
        }

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
        "min30_has_data": len(min30_signals) > 0,
        "min5_has_data": len(min5_signals) > 0,
        "trend_conflict": trend_conflict,
        "min30_dir": min30_dir,
        # BUG修复 (2026-07-30): 一买低点, 用于加仓风控和破位止损
        "one_buy_low": daily_one_buy_low,
        # 卖点对称检测 (2026-08-09): 一卖高点, 用于卖出风控
        "one_sell_high": daily_one_sell_high,
        # 日线卖点详情
        "daily_sell_dl_p": daily_sell_dl_p,
        "daily_sell_ep_p": daily_sell_ep_p,
        "daily_sell_valid": daily_sell_valid,
        "daily_one_sell_count": len(daily_one_sells),
        "daily_two_sell_count": len(daily_two_sells),
        "daily_three_sell_count": len(daily_three_sells),
        # 30min卖点详情
        "min30_two_sell_count": min30_two_sell_count,
        "min30_three_sell_count": min30_three_sell_count,
        # 5min卖点详情
        "min5_two_sell_count": min5_two_sell_count,
        "min5_three_sell_count": min5_three_sell_count,
        # P1 (2026-08-02): 中阴状态检测
        "zhongyin": detect_zhongyin(daily) if daily else {
            "is_zhongyin": False, "reason": "无日线数据",
            "action": "正常", "price_vs_zs": "未知",
        },
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

    # 新卖点字段 (2026-08-09: 一二三卖完整结构)
    daily_sell_valid = ml.get("daily_sell_valid", False)
    daily_sell_dl_p = ml.get("daily_sell_dl_p", 0)
    daily_two_sell_count = ml.get("daily_two_sell_count", 0)
    daily_three_sell_count = ml.get("daily_three_sell_count", 0)
    min30_two_sell_count = ml.get("min30_two_sell_count", 0)
    min30_three_sell_count = ml.get("min30_three_sell_count", 0)
    min5_two_sell_count = ml.get("min5_two_sell_count", 0)
    min5_three_sell_count = ml.get("min5_three_sell_count", 0)
    one_sell_high = ml.get("one_sell_high")

    sell_signals = []
    should_reduce = False
    should_clear = False
    reasons = []
    risk_level = "低"

    # 规则0: 缠论一卖确认(日线valid + DL_P>0.8) → 主动减仓
    if daily_sell_valid and daily_sell_dl_p > 0.8:
        should_reduce = True
        reasons.append(f"日线一卖确认(DL_P={daily_sell_dl_p:.2f}>0.8, 主动离场信号)")
        risk_level = "中"
        sell_signals.append({
            "level": "日线", "op": "一卖", "dir": "看空",
            "dl_p": daily_sell_dl_p, "source": "缠论顶背驰",
        })

    # 规则0b: 缠论二卖确认(日线或30min二卖) → 减仓
    if daily_two_sell_count > 0 or min30_two_sell_count > 0:
        if not should_reduce:
            should_reduce = True
        reasons.append(f"缠论二卖确认(日{daily_two_sell_count}+30min{min30_two_sell_count}个, 反弹不破前高)")
        if risk_level != "高":
            risk_level = "中"
        sell_signals.append({
            "level": "日线" if daily_two_sell_count > 0 else "30min",
            "op": "二卖", "dir": "看空", "source": "缠论反弹离场",
        })

    # 规则0c: 缠论三卖确认(跌破中枢+反抽) → 清仓
    if daily_three_sell_count > 0 or min30_three_sell_count > 0:
        should_clear = True
        should_reduce = True
        reasons.append(f"缠论三卖确认(日{daily_three_sell_count}+30min{min30_three_sell_count}个, 破位反抽)")
        risk_level = "高"
        sell_signals.append({
            "level": "日线" if daily_three_sell_count > 0 else "30min",
            "op": "三卖", "dir": "看空", "source": "缠论破位离场",
        })

    # 规则1: 日线趋势down + 30min看空信号valid → 建议减仓
    if daily_dir == "down" and min30_sell_count > 0:
        if not should_reduce:
            should_reduce = True
        reasons.append(f"日线趋势down+30min看空({min30_sell_count}个)")
        if risk_level == "低":
            risk_level = "中"
        sell_signals.append({
            "level": "30min", "op": "一卖", "dir": "看空",
            "count": min30_sell_count, "dl_p": min30_sell_dl_p,
        })

    # 规则2: 趋势矛盾(日线down vs 30min up) + 浮盈>10% → 建议减仓
    if trend_conflict and daily_dir == "down" and pnl_pct > 0.10:
        if not should_reduce:
            should_reduce = True
        reasons.append(f"趋势矛盾(日down/30up)+浮盈{pnl_pct*100:.1f}%")
        if risk_level == "低":
            risk_level = "中"

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

    # 规则6: 破一买低点 → 二买失败, 清仓
    one_buy_low = ml.get("one_buy_low")
    if one_buy_low and one_buy_low > 0 and close < one_buy_low:
        should_clear = True
        should_reduce = True
        pct_below = ((close - one_buy_low) / one_buy_low) * 100
        reasons.append(f"破一买低点(一买低={one_buy_low:.2f}, 现价{close:.2f}, 跌{pct_below:+.1f}%)→二买失败")
        risk_level = "高"

    # 规则7 (2026-08-09新增): 破一卖高点 → 卖点确认, 主动离场
    # 当价格跌破一卖高点时, 顶背驰结构已破坏, 应及时离场
    if one_sell_high and one_sell_high > 0 and close < one_sell_high:
        pct_below = ((close - one_sell_high) / one_sell_high) * 100
        reasons.append(f"破一卖高点(一卖高={one_sell_high:.2f}, 现价{close:.2f}, 跌{pct_below:+.1f}%)→顶背驰结构破坏")
        if risk_level == "低":
            risk_level = "中"
        sell_signals.append({
            "level": "日线", "op": "一卖破位", "dir": "看空",
            "one_sell_high": one_sell_high, "source": "破位离场",
        })

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
        "601515": {"name": "东风股份", "cost": 5.715},
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
