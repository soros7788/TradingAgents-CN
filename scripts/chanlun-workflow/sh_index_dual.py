"""上证综指缠论双系统分析: ZigZag摆点 + MACD柱面积背驰"""
import pandas as pd
import numpy as np
import os, sys

CACHE = os.path.expanduser("~/TradingAgents-CN/kline_cache")
W = 3  # 末端未确认根数(重绘区)

def load(tf):
    p = os.path.join(CACHE, "sh000001_%s.csv" % tf)
    df = pd.read_csv(p)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values("date").reset_index(drop=True)

def macd_hist(close, fast=12, slow=26, sig=9):
    ef = close.ewm(span=fast, adjust=False).mean()
    es = close.ewm(span=slow, adjust=False).mean()
    dif = ef - es
    dea = dif.ewm(span=sig, adjust=False).mean()
    return (dif - dea) * 2

def zigzag(df, dev_pct=1.0):
    """返回摆点列表 [(idx, 类型H/L, 价格, 日期)]，跳过末W根"""
    n = len(df)
    hi = df["high"].values; lo = df["low"].values
    pivots = []
    last_pv = 0; last_pt = None  # None=待定
    # 简化ZigZag: 用dev_pct过滤
    i = 1
    cur_h = hi[0]; cur_h_i = 0
    cur_l = lo[0]; cur_l_i = 0
    trend = 0  # 1=寻H, -1=寻L
    while i < n - W:
        if trend >= 0:
            if hi[i] > cur_h:
                cur_h = hi[i]; cur_h_i = i
            elif (cur_h - lo[i]) / cur_h * 100 >= dev_pct:
                pivots.append((cur_h_i, "H", cur_h, df.loc[cur_h_i, "date"]))
                trend = -1
                cur_l = lo[i]; cur_l_i = i
        if trend <= 0:
            if lo[i] < cur_l:
                cur_l = lo[i]; cur_l_i = i
            elif (hi[i] - cur_l) / cur_l * 100 >= dev_pct:
                pivots.append((cur_l_i, "L", cur_l, df.loc[cur_l_i, "date"]))
                trend = 1
                cur_h = hi[i]; cur_h_i = i
        i += 1
    return pivots

def divergence(df, pivots, hist):
    """比较相邻同类摆点间的MACD柱面积"""
    if len(pivots) < 4:
        return "数据不足"
    # 取最后两个同类摆点
    hs = [p for p in pivots if p[1] == "H"][-2:]
    ls = [p for p in pivots if p[1] == "L"][-2:]
    res = []
    if len(ls) == 2:
        (i1, _, p1, _), (i2, _, p2, _) = ls
        a1 = abs(hist[i1:i2].sum()) if i2 > i1 else 0
        # 上一段L到L的面积
        prev_ls = [p for p in pivots if p[1] == "L"][-3:-1]
        if len(prev_ls) == 2:
            (j1, _, _, _), (j2, _, _, _) = prev_ls
            a0 = abs(hist[j1:j2].sum()) if j2 > j1 else 0
            if p2 < p1 and a1 < a0 * 0.8:
                res.append("底背驰(力度减弱)")
            elif p2 < p1:
                res.append("新低但力度同步")
    if len(hs) == 2:
        (i1, _, p1, _), (i2, _, p2, _) = hs
        prev_hs = [p for p in pivots if p[1] == "H"][-3:-1]
        if len(prev_hs) == 2:
            (j1, _, _, _), (j2, _, _, _) = prev_hs
            a1 = abs(hist[i1:i2].sum()) if i2 > i1 else 0
            a0 = abs(hist[j1:j2].sum()) if j2 > j1 else 0
            if p2 > p1 and a1 < a0 * 0.8:
                res.append("顶背驰(力度减弱)")
            elif p2 > p1:
                res.append("新高但力度同步")
    return "；".join(res) if res else "无背驰(力度同步)"

def analyze(tf, dev):
    df = load(tf)
    hist = macd_hist(df["close"])
    piv = zigzag(df, dev)
    div = divergence(df, piv, hist)
    last = df.iloc[-1]
    last_piv = piv[-1] if piv else None
    # 找中枢: 简单用最近3个摆点的价格重叠区
    zhongshu = "None"
    if len(piv) >= 4:
        recent = piv[-4:]
        hh = min(p[2] for p in recent if p[1] == "H")
        ll = max(p[2] for p in recent if p[1] == "L")
        if hh > ll:
            zhongshu = "%.2f-%.2f" % (ll, hh)
    return {
        "tf": tf, "close": last["close"],
        "pivots": len(piv),
        "last_piv": "%s@%.2f (%s)" % (last_piv[1], last_piv[2], str(last_piv[3])[:16]) if last_piv else "None",
        "zhongshu": zhongshu, "divergence": div,
    }

if __name__ == "__main__":
    print("=== 上证综指缠论双系统 (数据截至09-28收盘) ===", flush=True)
    for tf, dev in [("30m", 1.5), ("5m", 0.8), ("1m", 0.5)]:
        try:
            r = analyze(tf, dev)
            print("[%s] 收%.2f | 摆点%d | 末摆%s | 中枢%s | 背驰: %s" % (
                r["tf"], r["close"], r["pivots"], r["last_piv"], r["zhongshu"], r["divergence"]), flush=True)
        except Exception as e:
            print("[%s] 失败: %s" % (tf, str(e)[:80]), flush=True)
    print("注: 末%d根为未确认区(ZigZag重绘)" % W, flush=True)
