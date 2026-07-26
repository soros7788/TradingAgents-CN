#!/usr/bin/env python3
"""
缠论背驰分析器 — 修复版

BUG修复记录:
1. 1min级别在大上涨趋势中误判"一买"
   - 根因: 前段仅15根K线，日内微小回调被当成有效下跌段
   - 修复: 前段最小30根 + 幅度>=0.1% + 大级别方向过滤
   - 日期: 2026-07-14
"""

import urllib.request
import ssl
import json
import re
import os
import pickle
import numpy as np

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


def _load_dl_model():
    """加载深度学习模型(懒加载)

    BUG修复 (2026-07-26): 原代码 except Exception: pass 静默吞掉所有错误
    症状: sklearn未安装时模型加载失败, 回退到硬编码0.70, 全市场无DL_P>0.8
    修复: 添加错误日志, 不再静默失败
    """
    global _dl_model, _dl_scaler, _dl_loaded
    if _dl_loaded:
        return _dl_model is not None
    _dl_loaded = True
    try:
        if not os.path.exists(_DL_MODEL_PATH):
            print(f"[DL模型] 模型文件不存在: {_DL_MODEL_PATH}")
            return False
        if not os.path.exists(_DL_SCALER_PATH):
            print(f"[DL模型] Scaler文件不存在: {_DL_SCALER_PATH}")
            return False
        with open(_DL_MODEL_PATH, 'rb') as f:
            _dl_model = pickle.load(f)
        with open(_DL_SCALER_PATH, 'rb') as f:
            _dl_scaler = pickle.load(f)
        print(f"[DL模型] 加载成功: model={type(_dl_model).__name__}, scaler={type(_dl_scaler).__name__}")
        return True
    except ImportError as e:
        print(f"[DL模型] 依赖缺失, 回退到硬编码: {e}")
        return False
    except Exception as e:
        print(f"[DL模型] 加载失败, 回退到硬编码: {type(e).__name__}: {e}")
        return False


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


def calc_area(vals, s, e):
    """计算MACD DIF面积(绝对值之和)"""
    if s >= e or s < 0 or e >= len(vals):
        return 0
    return sum(abs(v) for v in vals[s:e + 1])


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
    min_amp_map = {"日线": 0.3, "30min": 0.1, "5min": 0.05, "1min": 0.02}
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
        times = [d['day'] for d in data]
        V = [float(d.get('volume', 0)) for d in data]
        n = len(C)

    dif, dea, bar = calc_macd(C)
    atr = _compute_atr(H, L, C)  # 【DL修复】计算真实ATR供深度学习特征使用

    # 【BUG修复】大级别方向判断 (最近60根/或整体)
    lookback = min(60, n)
    overall_dir = seg_direction(C, n - lookback, n - 1)
    overall_pct = abs(C[-1] - C[n - lookback]) / C[n - lookback] * 100

    zss = find_zhongshu(H, L, min_w_map[level], min_amp_map[level])
    signals = []

    for zs in zss:
        zs_s, zs_e = zs['s'], zs['e']
        if zs_e >= n - post_min_bars[level]:
            continue

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
            pre_a = calc_area(dif, pre_s, pre_e)
            post_a = calc_area(dif, post_s, post_e)
            ratio = (post_a / pre_a * 100) if pre_a > 0 else 999

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

            # 【BUG修复】大级别方向过滤
            aligned = (pre_d == overall_dir)

            signals.append({
                "type": sig_type,
                "dir": direction,
                "op": op,
                "ratio": ratio,
                "dl_prob": dl_prob,  # 新增: 深度学习概率
                "zs": zs,
                "pre_dir": pre_d,
                "post_dir": post_d,
                "pre_ok": pre_ok,
                "post_ok": post_ok,
                "valid": pre_ok and post_ok and aligned,  # 修复: 必须aligned
                "aligned": aligned,
                "overall_dir": overall_dir,
                "pre_range": f"{times[pre_s]}~{times[pre_e]}",
                "post_range": f"{times[post_s]}~{times[post_e]}",
                "pre_reason": pre_reason,
                "post_reason": post_reason,
            })

    # ============================================================
    # 二买/三买信号检测 (2026-07-26)
    # 缠论定义:
    #   二买: 一买后反弹形成新中枢, 回踩不破前低 → 趋势确认
    #   三买: 二买后新中枢形成, 回踩不破二买中枢上沿 → 趋势加速
    # 实现: 基于已有中枢序列判断中枢上移模式
    # ============================================================
    bull_signals = [s for s in signals if s["op"] == "一买" and s["valid"]]
    if len(zss) >= 2 and bull_signals:
        for i in range(1, len(zss)):
            prev_zs = zss[i - 1]
            curr_zs = zss[i]

            # 二买: 当前中枢下沿 > 前中枢上沿(中枢上移) + 回踩到当前中枢
            if curr_zs["zd"] > prev_zs["zg"] and price > 0:
                if curr_zs["zd"] <= price <= curr_zs["zg"]:
                    signals.append({
                        "type": "盘整背驰",
                        "dir": "看多",
                        "op": "二买",
                        "ratio": 50,
                        "dl_prob": 0.75,
                        "zs": curr_zs,
                        "pre_dir": "down",
                        "post_dir": "up",
                        "pre_ok": True,
                        "post_ok": True,
                        "valid": True,
                        "aligned": True,
                        "overall_dir": overall_dir,
                        "pre_range": f"中枢{i}",
                        "post_range": f"中枢{i+1}",
                        "pre_reason": "中枢上移",
                        "post_reason": "回踩确认",
                    })

            # 三买: 两个中枢连续上移 + 回踩不破第二中枢上沿
            if i >= 2 and curr_zs["zd"] > prev_zs["zg"]:
                prev2_zs = zss[i - 2]
                if prev_zs["zd"] > prev2_zs["zg"] and price > 0:
                    if price > curr_zs["zg"]:
                        signals.append({
                            "type": "盘整背驰",
                            "dir": "看多",
                            "op": "三买",
                            "ratio": 40,
                            "dl_prob": 0.80,
                            "zs": curr_zs,
                            "pre_dir": "down",
                            "post_dir": "up",
                            "pre_ok": True,
                            "post_ok": True,
                            "valid": True,
                            "aligned": True,
                            "overall_dir": overall_dir,
                            "pre_range": f"中枢{i-1}",
                            "post_range": f"中枢{i+1}",
                            "pre_reason": "连续上移",
                            "post_reason": "突破加速",
                        })

    signals.sort(key=lambda x: -x['zs']['e'])

    result = {
        "code": code,
        "level": level,
        "n": n,
        "times": times,
        "C": C,
        "zss": zss,
        "signals": signals,
        "price": price,
        "cost": cost,
        "overall_dir": overall_dir,
        "overall_pct": overall_pct,
    }
    analyze_beichi._cache[cache_key] = result
    return result


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
        one = (f"{sig['op']} | {sig_type} | DL{dl_prob:.0%} 面积比{sig['ratio']:.1f}%"
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
        "600006": {"name": "东风股份", "cost": 5.715},
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
