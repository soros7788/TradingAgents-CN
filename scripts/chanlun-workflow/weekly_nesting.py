#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周线区间套 (Weekly Interval Nesting) —— 替代 5min-R2 的宏观背驰判定

v3 变更（2026-09-20）:
  4. 30min 中枢振幅门槛 min_amp 由 0.5% 下调到 0.3%（经授权）。
     横盘股(振幅<0.5%)此前找不到 30min 中枢，导致「4 级齐全」样本仅 73 只、
     合规率被幸存偏差抬高。下调后 30min 中枢覆盖率上升，4 级齐全样本扩大。
     可经环境变量 WN_30MIN_AMP 回退到 0.5 做 A/B 对照（默认 0.3）。

v2 关键修正（2026-09-19）:
1. 周线主口径改为「由日线聚合」(weekly_from_day)，与日线/30min/5min 同源同复权。
   实测 baostock 周线(qfq) 与 akshare 日线(qfq) 的周线中枢下沿相对差:
   中位 2.14% / P90 14.24% / 最大 40.35% —— 混源会直接制造伪违规, 故禁用混源。
   baostock 仍可通过 WEEKLY_SRC=bs 启用仅作交叉校验。
2. 窗对齐不再截断周线: 周线作为宏观锚, 时间跨度本就远大于低级;
   按 5min 首日(约2个月)截断后周线只剩 ~10 根, 无法形成中枢(min_width=5)。
   现仅对 日线/30min/5min 取共同起点, 周线保留全量并以「包含现价的最后中枢」保证当前性。
3. 数据优先读本地缓存 ~/kline_cache_local/{code}_{day|30m|5m}.csv (零网络, 秒级)。

零修改 beichi_analyzer.py, 零禁区写入。
"""
import os
import sys
import csv
import json
import socket
from datetime import date

socket.setdefaulttimeout(60)

REPO_DIR = "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow"
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if os.path.isdir(REPO_DIR):
    sys.path.insert(0, REPO_DIR)

from beichi_analyzer import find_zhongshu, validate_zhongshu_nesting  # noqa: E402

CACHE = os.path.expanduser("~/kline_cache_local")
LEVELS_ORDER = ["周线", "日线", "30min", "5min"]
# 30min 中枢振幅门槛：经授权由 0.5% 下调到 0.3%（2026-09-20）；
# WN_30MIN_AMP 可回退到 0.5 做 A/B 对照。
LEVEL_AMP = {"日线": 0.3, "30min": float(os.environ.get("WN_30MIN_AMP", "0.3")), "5min": 0.3}
DEFAULT_WEEKLY_AMP = float(os.environ.get("WEEKLY_AMP", "1.0"))
MIN_BARS = 10
WEEKLY_SRC = os.environ.get("WEEKLY_SRC", "day")  # day(默认) | bs


def _prefix(code):
    return "sh" if code.startswith(("6", "9", "688")) else "sz"


# ---------------- 取数 ----------------

def read_cache(code, period):
    """读本地缓存 ~/kline_cache_local/{code}_{period}.csv"""
    p = os.path.join(CACHE, "%s_%s.csv" % (code, period))
    if not os.path.exists(p):
        return None
    d = {"date": [], "high": [], "low": [], "close": []}
    try:
        with open(p) as f:
            for r in csv.DictReader(f):
                dc = r.get("day") or r.get("date")
                if not dc:
                    continue
                try:
                    hi, lo, cl = float(r["high"]), float(r["low"]), float(r["close"])
                except (ValueError, TypeError, KeyError):
                    continue
                d["date"].append(str(dc))
                d["high"].append(hi)
                d["low"].append(lo)
                d["close"].append(cl)
    except Exception:
        return None
    return d if len(d["close"]) >= MIN_BARS else None


def fetch_level_akshare(code, level):
    """日线/30min/5min 走 akshare qfq（缓存缺失时的兜底）"""
    import akshare as ak
    if level == "日线":
        df = ak.stock_zh_a_daily(symbol="%s%s" % (_prefix(code), code), adjust="qfq")
    else:
        df = ak.stock_zh_a_minute(symbol="%s%s" % (_prefix(code), code),
                                  period={"30min": "30", "5min": "5"}[level],
                                  adjust="qfq")
    date_col = "day" if "day" in df.columns else ("date" if "date" in df.columns else df.columns[0])
    d = {"date": [], "high": [], "low": [], "close": []}
    for _, row in df.iterrows():
        d["date"].append(str(row[date_col]))
        d["high"].append(float(row["high"]))
        d["low"].append(float(row["low"]))
        d["close"].append(float(row["close"]))
    return d


def get_level(code, level, use_cache=True):
    d = read_cache(code, {"日线": "day", "30min": "30m", "5min": "5m"}[level]) if use_cache else None
    if d:
        return d, "cache"
    try:
        return fetch_level_akshare(code, level), "akshare"
    except Exception as e:
        print("  %-5s 取数失败: %s: %s" % (level, type(e).__name__, str(e)[:60]), flush=True)
        return None, "fail"


# ---------------- 周线 ----------------

def weekly_from_day(day):
    """由日线聚合周线（同源同复权；按 ISO 周分组）"""
    if not day or not day.get("date"):
        return None
    buckets = {}
    for i, dt in enumerate(day["date"]):
        s = str(dt)[:10]
        try:
            wk = date(int(s[0:4]), int(s[5:7]), int(s[8:10])).isocalendar()[:2]
        except Exception:
            continue
        b = buckets.setdefault(wk, {"hi": None, "lo": None, "cl": None, "dt": s})
        h, l, c = day["high"][i], day["low"][i], day["close"][i]
        b["hi"] = h if b["hi"] is None else max(b["hi"], h)
        b["lo"] = l if b["lo"] is None else min(b["lo"], l)
        b["cl"] = c
        b["dt"] = s
    w = {"date": [], "high": [], "low": [], "close": []}
    for k in sorted(buckets.keys()):
        b = buckets[k]
        if b["hi"] is None or b["lo"] is None or b["cl"] is None:
            continue
        w["date"].append(b["dt"])
        w["high"].append(b["hi"])
        w["low"].append(b["lo"])
        w["close"].append(b["cl"])
    return w if len(w["close"]) >= MIN_BARS else None


def fetch_weekly_bs(code, start="2018-01-01"):
    """baostock 周线（仅交叉校验用，非主口径）"""
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            "%s.%s" % (_prefix(code), code), "date,open,high,low,close,volume,amount",
            start_date=start, frequency="w", adjustflag="2")
        w = {"date": [], "high": [], "low": [], "close": []}
        if rs.error_code == "0":
            while rs.next():
                r = rs.get_row_data()
                try:
                    w["date"].append(r[0])
                    w["high"].append(float(r[2]))
                    w["low"].append(float(r[3]))
                    w["close"].append(float(r[4]))
                except (ValueError, TypeError, IndexError):
                    continue
        return w if len(w["close"]) >= MIN_BARS else None
    finally:
        try:
            bs.logout()
        except Exception:
            pass


# ---------------- 对齐 / 中枢 ----------------

def align_common_window(datasets, full_levels=("周线",)):
    """日线/30min/5min 取共同起点；周线保留全量（详见模块 docstring 第2点）"""
    parts = {k: v for k, v in datasets.items() if k not in full_levels}
    starts = [v["date"][0][:10] for v in parts.values() if v and v.get("date")]
    if not starts:
        return datasets, None, []
    cut = max(starts)
    aligned, notes = {}, []
    for lv, d in datasets.items():
        if not d or not d.get("date"):
            aligned[lv] = d
            continue
        n = min(len(d[k]) for k in ("date", "high", "low", "close"))
        if n < MIN_BARS:
            aligned[lv] = None
            notes.append("%s 样本不足 %d 根" % (lv, n))
            continue
        if lv in full_levels:
            aligned[lv] = {k: v[:n] for k, v in d.items()}
            continue
        keep = [i for i, dt in enumerate(d["date"][:n]) if str(dt)[:10] >= cut]
        if len(keep) < MIN_BARS:
            aligned[lv] = {k: v[:n] for k, v in d.items()}
            notes.append("%s 截断后仅%d根<%d，保留全量" % (lv, len(keep), MIN_BARS))
        else:
            aligned[lv] = {k: [v[i] for i in keep] for k, v in d.items()}
    return aligned, cut, notes


def select_current_zs(data, min_width, min_amp):
    """选取「当前」中枢：优先包含现价的最后一个中枢，否则最后一个 + 陈旧标记"""
    all_zs = find_zhongshu(data["high"], data["low"],
                           min_width=min_width, min_amp_pct=min_amp)
    if not all_zs:
        return None, None, None
    cur = data["close"][-1]
    n = len(data["close"])
    containing = [z for z in all_zs if z["zd"] <= cur <= z["zg"]]
    zs = containing[-1] if containing else all_zs[-1]
    stale = n - 1 - zs["e"]
    dist = 0.0 if containing else (
        min(abs(cur - zs["zg"]), abs(cur - zs["zd"])) / zs["zd"] * 100.0)
    return zs, stale, dist


def _slice_window(data, d0, d1=None, min_bars=12):
    """切 [d0, 末尾]：下界=高级中枢起点，保证低级中枢不早于高级中枢形成。

    不用上界(d1)：高级中枢时间跨度往往很短(几天)，掐断后低级样本不足
    (min_width=5 段常找不到中枢) -> 参与校验的级别变少 -> 合规率虚高。
    """
    if data is None or not data.get("date"):
        return None
    idx = [j for j, dt in enumerate(data["date"]) if str(dt)[:10] >= d0]
    if len(idx) < min_bars:
        return None
    return {k: [v[j] for j in idx] for k, v in data.items()}


def zs_chain(datasets, order, amps, min_width=5, min_bars=12):
    """「时间嵌套」版中枢选取 —— 区间套的正解。

    自上而下：高级别中枢的时间跨度 [d_s, d_e] 作为低级别的搜索窗口，
    低级中枢只在该窗口内寻找。这保证各级中枢**时间上重叠**，
    之后的价格包容校验(validate_zhongshu_nesting)才有意义。
    旧口径是各级别独立找"当前中枢"，时间可错位数月 -> 大量伪违规。
    """
    res, windows = {}, {}
    win = None
    for lv in order:
        data = datasets.get(lv)
        if not data:
            res[lv] = None
            continue
        zs_list = find_zhongshu(data["high"], data["low"],
                                min_width=min_width, min_amp_pct=amps.get(lv, 0.3))
        if not zs_list:
            res[lv] = None
            windows[lv] = (str(data["date"][0])[:10], str(data["date"][-1])[:10])
            win = windows[lv]
            continue
        if win is None:
            # 顶层(周线)：取「最后完成的中枢」= 缠论标准的当前中枢。
            # 不优先"包含现价"——周线中枢宽、跨度高，强制包含现价会选中几年前的旧中枢
            # (实测有效锚从 193 只掉到 104 只)。旧中枢由新鲜度分档(wn_stale.py)筛掉。
            z = zs_list[-1]
        else:
            # 低级：从后往前找第一个「结束时间不早于高级中枢起点」的中枢（时间重叠即可）
            # 不用切片 —— 按高级起点切会把低级砍到不足 5 段，反而找不到中枢。
            z = None
            for cand in reversed(zs_list):
                if str(data["date"][cand["e"]])[:10] >= win[0]:
                    z = cand
                    break
            if z is None:
                res[lv] = None
                windows[lv] = (str(data["date"][0])[:10], str(data["date"][-1])[:10])
                win = windows[lv]
                continue
        res[lv] = z
        windows[lv] = (str(data["date"][z["s"]])[:10], str(data["date"][z["e"]])[:10])
        win = windows[lv]
    return res, windows


# ---------------- 主流程 ----------------

def analyze(code, weekly_amp=DEFAULT_WEEKLY_AMP, use_cache=True, weekly_src=WEEKLY_SRC):
    """返回单只标的的周线区间套判定 dict"""
    ds, srcs = {}, {}
    day, sday = get_level(code, "日线", use_cache)
    if day:
        ds["日线"], srcs["日线"] = day, sday
    for lv in ("30min", "5min"):
        d, s = get_level(code, lv, use_cache)
        if d:
            ds[lv], srcs[lv] = d, s
    if not ds.get("日线"):
        return {"code": code, "error": "日线缺失(周线由日线聚合)"}
    if weekly_src == "bs":
        w = fetch_weekly_bs(code)
        srcs["周线"] = "baostock"
    else:
        w = weekly_from_day(ds["日线"])
        srcs["周线"] = "日线聚合"
    if w:
        ds["周线"] = w
    else:
        return {"code": code, "error": "周线聚合失败"}

    aligned, cut, notes = align_common_window(ds)
    amps = {lv: (weekly_amp if lv == "周线" else LEVEL_AMP.get(lv, 0.3)) for lv in LEVELS_ORDER}
    order = [l for l in LEVELS_ORDER if aligned.get(l)]
    nested = os.environ.get("WN_NESTED", "1") == "1"
    res, detail, windows = {}, {}, {}
    if nested:
        res, windows = zs_chain({k: v for k, v in aligned.items() if v}, order, amps)
        for lv in order:
            zs = res.get(lv)
            if zs:
                detail[lv] = {"bars": 0, "cur": ds[lv]["close"][-1], "zd": zs["zd"], "zg": zs["zg"],
                              "w": zs["w"],
                              "s": windows[lv][0], "e": windows[lv][1],
                              "last": str(ds[lv]["date"][-1])[:10],
                              "stale": None, "dist": 0.0,
                              "pos": ("below" if ds[lv]["close"][-1] < zs["zd"]
                                      else ("above" if ds[lv]["close"][-1] > zs["zg"] else "inside")),
                              "src": srcs.get(lv), "nested": True}
    else:
        for lv in LEVELS_ORDER:
            d = aligned.get(lv)
            if not d:
                res[lv] = None
                continue
            zs, stale, dist = select_current_zs(d, 5, amps[lv])
            res[lv] = zs
            if zs:
                cur = d["close"][-1]
                pos = "below" if cur < zs["zd"] else ("above" if cur > zs["zg"] else "inside")
                detail[lv] = {"bars": len(d["close"]), "cur": cur, "zd": zs["zd"], "zg": zs["zg"],
                              "w": zs["w"], "s": d["date"][zs["s"]], "e": d["date"][zs["e"]],
                              "stale": stale, "dist": dist, "pos": pos, "src": srcs.get(lv)}
    try:
        v = validate_zhongshu_nesting(res, [l for l in LEVELS_ORDER if res.get(l)])
        ok, viol = v.get("ok"), v.get("violations", [])
    except Exception as e:
        ok, viol = None, ["校验异常 %s" % type(e).__name__]
    return {"code": code, "cut": cut, "notes": notes, "nesting_ok": ok,
            "violations": len(viol), "violation_detail": viol, "levels": detail,
            "nested_mode": nested, "windows": windows}


def main():
    codes = sys.argv[1:] or ["600006", "002141", "603256"]
    weekly_amp = float(os.environ.get("WEEKLY_AMP", DEFAULT_WEEKLY_AMP))
    print("周线区间套 v3  标的=%s  周线源=%s  周线amp=%.2f%%  30min_amp=%.2f%%"
          % (codes, WEEKLY_SRC, weekly_amp, LEVEL_AMP["30min"]), flush=True)
    out = []
    for code in codes:
        r = analyze(code, weekly_amp)
        out.append(r)
        print("\n" + "=" * 66)
        print("%s" % code)
        print("=" * 66)
        if r.get("error"):
            print("  %s" % r["error"], flush=True)
            continue
        if r.get("cut"):
            print("  共同时间窗起点(日/30min/5min) = %s   周线保留全量" % r["cut"], flush=True)
        for nt in r.get("notes", []):
            print("  [%s]" % nt, flush=True)
        for lv in LEVELS_ORDER:
            d = r["levels"].get(lv)
            if not d:
                print("  %-5s 无有效中枢/缺失" % lv, flush=True)
                continue
            tag = "" if not d.get("stale") else " [陈旧%d根, 距现价%.1f%%]" % (d["stale"], d["dist"])
            if d.get("nested"):
                print("  %-5s 现价=%.3f  中枢 %.3f–%.3f (宽%d, %s→%s) [%s]"
                      % (lv, d["cur"], d["zd"], d["zg"], d["w"], d["s"], d["e"], d["pos"]), flush=True)
            else:
                print("  %-5s bars=%-6d 现价=%.3f  中枢 %.3f–%.3f (宽%d, %s→%s) [%s]%s"
                      % (lv, d["bars"], d["cur"], d["zd"], d["zg"], d["w"], d["s"], d["e"],
                         d["pos"], tag), flush=True)
        print("  --> 嵌套 ok=%s  违规=%d" % (r["nesting_ok"], r["violations"]), flush=True)
        for x in r.get("violation_detail", []):
            print("      - %s" % x, flush=True)

    if os.environ.get("WN_JSON"):
        json.dump(out, open(os.environ["WN_JSON"], "w"), ensure_ascii=False, indent=1)
        print("\nJSON 落盘: %s" % os.environ["WN_JSON"])


if __name__ == "__main__":
    main()
