#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周线区间套 Watchlist 覆盖层 —— 串入工作流（每日/每次 dualscan 后跑）

定位：full_scan 全市场 2800 只 + baostock 慢，周线判定不适合做全市场闸门，
      只适合做「两级联立 watchlist 覆盖层」——在 dualscan 已筛出的双 pass 标的
      (递归 R0+R1 双 pass = present_r_levels>=2) 上，
      叠加周线宏观方向锚 + 时间嵌套合规，输出可操作的观察名单。

B-a2 口径：越界(violations) 按方向重分类 —— 低级别朝周线位置方向的突破视为
      顺势确认(加分)，反向越界才是真违规(反转预警)；方向未定(中枢内)保守计入逆势。
      强信号门槛 = 4级齐全 且 逆势越界=0。原 violations 字段保留作对照。
      本层重分类，不改 beichi_analyzer.validate_zhongshu_nesting。

复用：
  - weekly_nesting.analyze  （单只判定，与批量/单只版口径一致）
  - weekly_nesting_batch.pass_codes_lianli2  （两级联立 2/2 标的）
  - wn_stale 的新鲜度分档逻辑（周线中枢结束日 vs 日线最后日期，相差交易日数）

输入（优先级）：
  1) argv 显式代码列表（若首参是文件则读文件每行一个代码）
  2) 环境变量 WNLIST 指向的代码文件
  3) 默认：两级联立 2/2 标的

输出：
  - JSON 落盘（WNL_OUT，默认 /tmp/weekly_watchlist.json）：全量逐只明细
  - 文本报告：按周线方向分层（下方/中枢内/上方）+ 新鲜度 + 嵌套合规 + 持仓股重点标注

零禁区写入，零 beichi_analyzer 修改。
"""
import os
import sys
import csv
import json
import glob
import datetime

REPO_DIR = "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow"
sys.path.insert(0, REPO_DIR)
import weekly_nesting as wn  # noqa: E402

# batch 模块按文件路径加载（不放 /tmp 进 sys.path，避免旧版 weekly_nesting 遮蔽 repo 版）
BATCH_PATH = os.environ.get("WN_BATCH",
                            os.path.join(REPO_DIR, "weekly_nesting_batch.py"))


def _load_batch():
    import importlib.util
    spec = importlib.util.spec_from_file_location("weekly_nesting_batch", BATCH_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

CACHE = os.path.expanduser("~/kline_cache_local")
FRESH = int(os.environ.get("WN_FRESH", "20"))           # 有效锚新鲜度阈值(交易日)
ONLY_FRESH = os.environ.get("WN_ONLY_FRESH", "1") == "1"  # 默认只列有效锚
OUT_JSON = os.environ.get("WNL_OUT", "/tmp/weekly_watchlist.json")
OUT_TXT = os.environ.get("WNL_TXT", "/tmp/weekly_watchlist.txt")
# 可选：直接在一份批量 JSON(如 wn_lianli2_v6.json) 上做覆盖层，跳过重复 analyze
WN_IN = os.environ.get("WN_IN")

# 持仓/观察股备注（手动维护）
HOLDINGS = {
    "600006": "东风股份·持仓100股·成本8.711",
    "002141": "贤丰控股·已清仓·回补区6.11-6.20",
    "603256": "宏和科技·观察·136.3->148.79",
}


def classify_violations(vd, weekly_pos):
    """B-a2: 违规方向化重分类。

    原始 violations 来自 beichi_analyzer.validate_zhongshu_nesting —— 纯几何包含校验:
      低级别中枢下沿低于高级别下沿 -> zd_breach(向下越界)
      低级别中枢上沿高于高级别上沿 -> zg_breach(向上越界)
    它不区分方向, 导致「低级别顺着周线位置方向突破」也被判违规 —— 而那恰恰是趋势确认。

    重分类规则(以周线位置 weekly_pos 为基准):
      - above + zg_breach(向上越界) -> 顺势: 趋势确认, 加分
      - below + zd_breach(向下越界) -> 顺势: 趋势确认, 加分
      - above + zd_breach / below + zg_breach -> 逆势: 真违规, 反转预警
      - inside / 无锚 -> 保守计入逆势(方向未定, 越界不能归因为趋势确认)

    返回 (顺势数, 逆势数)。原 violations 字段保留不动, 便于对照。
    """
    trend = adverse = 0
    for v in (vd or []):
        if not isinstance(v, dict):
            continue
        t = v.get("type")
        if t == "zg_breach":
            up = True
        elif t == "zd_breach":
            up = False
        else:
            adverse += 1
            continue
        if weekly_pos == "above":
            trend += 1 if up else 0
            adverse += 0 if up else 1
        elif weekly_pos == "below":
            trend += 0 if up else 1
            adverse += 1 if up else 0
        else:
            adverse += 1
    return trend, adverse


def pass_codes():
    try:
        return _load_batch().pass_codes_lianli2()
    except Exception as e:
        print("  [warn] pass_codes_lianli2 失败: %s" % e, flush=True)
        return []


def load_codes():
    argv = sys.argv[1:]
    if argv:
        first = argv[0]
        if os.path.exists(first):
            with open(first) as f:
                return [l.strip() for l in f if l.strip()]
        return [a.strip() for a in argv if a.strip()]
    wl = os.environ.get("WNLIST")
    if wl and os.path.exists(wl):
        with open(wl) as f:
            return [l.strip() for l in f if l.strip()]
    return pass_codes()


def day_dates(code):
    p = os.path.join(CACHE, "%s_day.csv" % code)
    out = []
    if not os.path.exists(p):
        return out
    with open(p) as f:
        for r in csv.DictReader(f):
            d = (r.get("date") or r.get("day") or "")[:10]
            if d:
                out.append(d)
    return out


def freshness(code, zs_e):
    """周线中枢结束日之后又走了多少根日线（交易日近似）"""
    ds = day_dates(code)
    if not ds or not zs_e:
        return None
    try:
        i = ds.index(str(zs_e)[:10])
    except ValueError:
        cand = [k for k, d in enumerate(ds) if d <= str(zs_e)[:10]]
        i = max(cand) if cand else 0
    return len(ds) - 1 - i


def dev_pct(z):
    """现价 vs 周线中枢的偏离（上方看 zg，下方/中枢内看 zd）"""
    ref = z["zg"] if z["pos"] == "above" else z["zd"]
    if not ref:
        return 0.0
    return (z["cur"] - ref) / ref * 100.0


def main():
    codes = load_codes()
    # 持仓/观察股永远纳入覆盖层（即使不在 dualscan 2/2 名单），且始终实时判定
    codes = sorted(set(codes) | set(HOLDINGS.keys()))
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    print("周线区间套 Watchlist 覆盖层  %s" % stamp, flush=True)
    print("  标的=%d  30min_amp=%.2f%%  周线源=%s  有效锚阈值=%d日  ONLY_FRESH=%s"
          % (len(codes), wn.LEVEL_AMP["30min"], wn.WEEKLY_SRC, FRESH, ONLY_FRESH), flush=True)

    recs = []
    reuse = None
    if WN_IN and os.path.exists(WN_IN):
        try:
            reuse = json.load(open(WN_IN))
            print("  复用批量结果: %s (%d 只)" % (WN_IN, len(reuse)), flush=True)
        except Exception as e:
            print("  [warn] WN_IN 读取失败, 回退实时 analyze: %s" % e, flush=True)
    for i, c in enumerate(codes):
        if i % 50 == 0 and len(codes) > 50:
            print("    进度 %d/%d" % (i, len(codes)), flush=True)
        if reuse is not None and c in reuse:
            r = reuse[c]
        elif c in HOLDINGS or reuse is None:
            # 持仓/观察股始终实时判定；无 WN_IN 时全部实时
            try:
                r = wn.analyze(c)
            except Exception as e:
                r = {"code": c, "error": "%s: %s" % (type(e).__name__, str(e)[:80])}
        else:
            r = {"code": c, "error": "批量结果缺失"}
        z = (r.get("levels") or {}).get("周线")
        # C 修复: above 票股价已涨离周线中枢, 中枢结束日(s="e")天然古老;
        #   方向锚有效性取决于周线数据是否刷新, 故改用周线末K线日("last")衡量新鲜度.
        #   below/inside 仍用中枢结束日(中枢较近期, 合理). 零 sealed 改动.
        anchor_e = (z.get("last") if (z and z.get("pos") == "above") else (z["e"] if z else None))
        st = freshness(c, anchor_e) if z else None
        rec = {
            "code": c,
            "error": r.get("error"),
            "weekly_pos": z["pos"] if z else None,
            "cur": z["cur"] if z else None,
            "zd": z["zd"] if z else None,
            "zg": z["zg"] if z else None,
            "zs_range": ("%.2f-%.2f" % (z["zd"], z["zg"])) if z else None,
            "zs_e": z["e"] if z else None,
            "anchor_e": anchor_e if z else None,  # C: above=周线末日(last), 其余=中枢结束日(e)
            "dev_pct": round(dev_pct(z), 2) if z else None,
            "fresh_days": st,
            "effective": (st is not None and st <= FRESH),
            "nesting_ok": r.get("nesting_ok"),
            "violations": r.get("violations"),  # 原始: 几何包含越界总数(不分方向)
            "violation_detail": r.get("violation_detail"),
            "levels_present": sorted((r.get("levels") or {}).keys()),
            "holding": HOLDINGS.get(c),
            "nested_mode": r.get("nested_mode"),
        }
        # B-a2: 违规方向化 —— 顺势越界=趋势确认(加分), 逆势越界=真违规(反转预警)
        _t, _a = classify_violations(rec.get("violation_detail"), rec.get("weekly_pos"))
        rec["viol_trend"] = _t
        rec["viol_adverse"] = _a
        recs.append(rec)

    # 分层
    eff = [x for x in recs if x["effective"]]
    stale_or_none = [x for x in recs if not x["effective"]]
    by_pos = {"below": [], "inside": [], "above": []}
    for x in eff:
        if x["weekly_pos"] in by_pos:
            by_pos[x["weekly_pos"]].append(x)
    # 下方按深度破位(负偏离)排序；上方按升破幅度排序；中枢内按新鲜度
    by_pos["below"].sort(key=lambda x: (x["dev_pct"] if x["dev_pct"] is not None else 0))
    by_pos["above"].sort(key=lambda x: -(x["dev_pct"] if x["dev_pct"] is not None else 0))
    by_pos["inside"].sort(key=lambda x: (x["fresh_days"] if x["fresh_days"] is not None else 999))

    # 统计
    pc = {k: len(v) for k, v in by_pos.items()}
    lv4 = [x for x in recs if len(x["levels_present"]) == 4]
    # B-a2: 强信号门槛改为「0 逆势越界」(顺势越界视为趋势确认, 不再扣分)
    ok0 = [x for x in eff if (x.get("viol_adverse") or 0) == 0 and len(x["levels_present"]) == 4]
    # 旧口径留存对照: 0 几何越界(实测在上方/下方组恒为 0)
    ok0_raw = [x for x in eff if (x.get("violations") or 0) == 0 and len(x["levels_present"]) == 4]
    hold = [x for x in recs if x["holding"]]

    # 落盘 JSON
    json.dump({"stamp": stamp, "total": len(recs), "effective": len(eff),
               "fresh_days_threshold": FRESH, "records": recs},
              open(OUT_JSON, "w"), ensure_ascii=False, indent=1)

    # 文本报告
    L = []
    L.append("周线区间套 Watchlist 覆盖层  %s" % stamp)
    L.append("标的=%d  30min_amp=%.2f%%  周线源=%s  有效锚阈值=%d交易日"
             % (len(recs), wn.LEVEL_AMP["30min"], wn.WEEKLY_SRC, FRESH))
    L.append("=" * 70)
    L.append("有效周线锚(<= %d日): %d 只   方向: 下方%d / 中枢内%d / 上方%d"
             % (FRESH, len(eff), pc["below"], pc["inside"], pc["above"]))
    L.append("4级齐全: %d 只   4级齐全∩0逆势越界: %d 只   (旧口径∩0几何越界: %d 只)   失效/无锚: %d 只"
             % (len(lv4), len(ok0), len(ok0_raw), len(stale_or_none)))
    L.append("=" * 70)

    def fmt(x):
        pos_cn = {"below": "下方", "inside": "中枢内", "above": "上方"}.get(x["weekly_pos"], "-")
        note = ("  [%s]" % x["holding"]) if x["holding"] else ""
        v = x["violations"]
        if v == 0:
            viol = "合规"
        else:
            viol = "越界%d·顺%d逆%d" % (v, x.get("viol_trend") or 0, x.get("viol_adverse") or 0)
        return ("  %s 现价%.2f 周中枢%s %+.2f%%  %s  新鲜%d日  %s%s"
                % (x["code"], x["cur"], x["zs_range"], x["dev_pct"],
                   pos_cn, x["fresh_days"], viol, note))

    L.append("")
    L.append("【下方 · 宏观空头延续，反弹即卖点】(按深度破位排序)")
    for x in by_pos["below"]:
        L.append(fmt(x))
    L.append("")
    L.append("【中枢内 · 方向待定，等突破】")
    for x in by_pos["inside"]:
        L.append(fmt(x))
    L.append("")
    L.append("【上方 · 宏观多头，回踩不破可跟】")
    for x in by_pos["above"]:
        L.append(fmt(x))
    L.append("")
    L.append("【持仓/观察股重点】")
    if hold:
        for x in hold:
            L.append(fmt(x))
    else:
        L.append("  (无)")
    L.append("")
    L.append("【4级齐全 ∩ 0逆势越界 · 顺势强信号】%d 只: %s"
             % (len(ok0), [x["code"] for x in ok0][:60]))
    L.append("  (旧口径 ∩0几何越界 %d 只: %s)"
             % (len(ok0_raw), [x["code"] for x in ok0_raw][:20]))
    L.append("")
    L.append("说明: 周线只当方向锚，不单独作买卖闸门；日线确认 + 30min/5min 加分。"
             "失效锚(>%d日)样本已剔除，避免旧中枢制造伪信号。" % FRESH)
    L.append("口径(B-a2): 越界按方向重分类 —— 低级别朝周线位置方向突破=顺势确认(顺n, 加分); "
             "反向越界=真违规(逆m, 反转预警)。方向未定(中枢内)的越界保守计入逆势。"
             "强信号门槛=4级齐全 且 逆势越界=0。原 violations 字段保留作对照。")
    txt = "\n".join(L)
    open(OUT_TXT, "w").write(txt + "\n")

    # 终端摘要
    print(txt, flush=True)
    print("JSON: %s" % OUT_JSON, flush=True)
    print("TXT : %s" % OUT_TXT, flush=True)


if __name__ == "__main__":
    main()
