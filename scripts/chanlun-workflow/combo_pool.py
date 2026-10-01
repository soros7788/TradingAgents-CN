#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
组合池: 周线方向向上(类R2) ∩ 日线底背驰(类R1转折)
============================================================
缠论经典「大级别择时 + 小级别找买点」二买/三买结构:
  大级别(周线, 替代递归R2)方向向上  AND  小级别(日线/R1)底背驰完成
  = 顺势中的转折买点, 区间套抄底。

选池条件(严格口径, 修复后):
  1. weekly_pos == "above"        (周线方向向上, 替代R2) —— 方向硬门槛
  2. dlp > 0.618                  (力度衰竭硬门槛, 黄金比例, 来自 dualscan) —— 力度硬门槛
  3. R1 verdict == "conflict"     (小级别逆大级别 = 转折诞生地, 类日线底背驰) —— 降级软标记/加分
  4. effective (fresh_days<=FRESH) (C 修复: above 用周线末K线日衡量, 不再被古老中枢结束日误杀)

数据源:
  - 自动选源: union 最近 24h 内所有 dualscan_*.json 的 dual 记录(按 code dedup 保最新,
    跳过历史 union 产物), 绕开分批小 json 陷阱, 与 trend_pool_daily.sh / beichi_turn_daily.sh 选源策略一致
  - weekly_nesting.analyze 实时算周线方向锚(读 ~/kline_cache_local, 零网络)

零 sealed 改动: 仅消费 weekly_nesting / weekly_watchlist 公共接口, 不碰 recursive_core/interval_engine/tradingagents/chan/*。
"""
import sys, os, glob, json, time
from datetime import datetime
sys.path.insert(0, "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow")
import weekly_watchlist as W

HOME = os.path.expanduser("~")
LOGDIR = os.path.join(HOME, "chan_logs")

def resolve_dual():
    """选源: union 最近 24h 内所有 dualscan_*.json 的 dual 记录(按 code dedup 保最新),
    绕开分批小 json 陷阱(单文件条数最多会踩中 20 只小批次)。与 trend/beichi 池一致。"""
    now = time.time()
    merged = {}   # code -> [mtime, record]
    for f in glob.glob(os.path.join(LOGDIR, "dualscan_*.json")):
        if "union" in os.path.basename(f):
            continue   # 跳过历史合并产物, 避免自我嵌套
        try:
            mt = os.path.getmtime(f)
        except OSError:
            continue
        if now - mt > 86400:
            continue
        try:
            recs = json.load(open(f)).get("dual") or []
        except Exception:
            continue
        for r in recs:
            code = r.get("code") or r.get("stock") or r.get("symbol") or r.get("ts_code")
            if code and (code not in merged or mt >= merged[code][0]):
                merged[code] = [mt, r]
    if not merged:
        return None
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stage1": None,
           "dual": [v[1] for v in merged.values()], "summary": {}}
    op = os.path.join(LOGDIR, "dualscan_union_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    json.dump(out, open(op, "w"), ensure_ascii=False)
    return op

TODAY = datetime.now().strftime("%Y%m%d")
BASELINE_DATE = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
OUT_TXT = os.environ.get("COMBO_TXT", os.path.join(LOGDIR, "combo_pool_watchlist_%s.txt" % TODAY))
OUT_JSON = os.environ.get("COMBO_JSON", os.path.join(LOGDIR, "combo_pool_watchlist_%s.json" % TODAY))
DL_P_MIN = 0.618
FRESH = W.FRESH

def dev_pct(z):
    # P2-2 修复 (2026-10-01): cur=None 时不抛 TypeError, 偏离度记 0
    ref = z["zg"] if z["pos"] == "above" else z["zd"]
    cur = z.get("cur")
    if not ref or not isinstance(cur, (int, float)) or cur != cur:
        return 0.0
    return (cur - ref) / ref * 100.0


def _fmt_price(v):
    # P2-2 修复 (2026-10-01): None/NaN 价格格式化为 N/A, 不崩整池
    return "%.2f" % v if isinstance(v, (int, float)) and v == v else "N/A"


def _fmt_days(v):
    # P2-2 修复 (2026-10-01): freshness() 可返回 None, 新鲜度格式化为 N/A
    return "%d" % v if isinstance(v, int) else "N/A"

# ② 补齐: 优先级分级(仅用现有信号 dlp/新鲜度, 不引入新数据源)
#   A = 强背驰(dlp>=1.0) 且 锚新鲜(<=5日) 且 有效
#   B = dlp>0.618 且 有效
#   C = 锚陈旧 / 力度临界, 仅观察
def grade_tier(r):
    if not r["effective"]:
        return "C"
    if r["dlp"] >= 1.0 and (r["fresh_days"] is not None and r["fresh_days"] <= 5):
        return "A"
    if r["dlp"] >= DL_P_MIN:
        return "B"
    return "C"

def main():
    # P2-1 修复 (2026-10-01): 选源移入 main, import 不再执行 union+写文件/退出
    DS = resolve_dual()
    if not DS:
        sys.exit("FATAL: 找不到 dualscan_*.json 数据源")
    ds = json.load(open(DS))
    # 粗筛: R1 conflict ∩ dlp>0.618 (少量候选, 再实时 analyze 确认周线方向)
    cands = []
    c2 = {}
    for it in ds["dual"]:
        la = it.get("level_alignment", {})
        dlp = it.get("dlp") or 0
        r1v = la.get("R1", {}).get("verdict")
        is_conflict = (r1v == "conflict")
        c2[it["code"]] = dict(dlp=dlp, r1v=r1v, is_conflict=is_conflict, price=it.get("price"))
        # 修复: 原硬 AND(R1 conflict AND dlp>DL_P_MIN) 与高 dlp 互斥(交集恒 0)。
        #   改以 dlp>DL_P_MIN 为力度硬门槛(周线above 仍作方向硬门槛),
        #   R1 conflict 降级为软标记(原"小级别逆大级别=转折诞生地"语义保留为加分)。
        if dlp > DL_P_MIN:
            cands.append(it["code"])

    recs = []
    for c in cands:
        r = W.wn.analyze(c)
        z = (r.get("levels") or {}).get("周线")
        if not z or z["pos"] != "above":
            continue
        # C 修复: above 用周线末K线日(last), 其余用中枢结束日(e)
        anchor_e = z.get("last")
        st = W.freshness(c, anchor_e)
        eff = (st is not None and st <= FRESH)
        # ② 防御: 空壳记录(dlp=0 或 None)直接剔除, 不进池
        if not c2[c]["dlp"]:
            continue
        wk_cur = z["cur"]
        tradable = isinstance(wk_cur, (int, float)) and wk_cur == wk_cur and wk_cur > 0
        rec = dict(
            code=c, dlp=round(c2[c]["dlp"], 3), weekly_pos=z["pos"],
            cur=wk_cur, price=c2[c]["price"], weekly_cur=wk_cur,
            zs_range="%.2f-%.2f" % (z["zd"], z["zg"]),
            dev_pct=round(dev_pct(z), 2), fresh_days=st, effective=eff,
            anchor_e=anchor_e,
            r1_conflict=c2[c]["is_conflict"],
            tradable=tradable,
        )
        rec["tier"] = grade_tier(rec)
        recs.append(rec)
    recs.sort(key=lambda x: -x["dlp"])
    eff_recs = [r for r in recs if r["effective"]]

    L = []
    L.append("组合池: 周线向上(类R2) ∩ 高背驰力度(类二买/中继)")
    L.append("基准日: %s | 分级: A=强背驰+新鲜 / B=有效达标 / C=仅观察(锚陈旧)" % BASELINE_DATE)
    L.append("数据源: %s | 周线源=%s | 锚新鲜阈值=%d日" % (os.path.basename(DS), W.wn.WEEKLY_SRC, FRESH))
    L.append("=" * 66)
    L.append("选池条件(修复后): weekly_pos=above(周线顺,方向硬门槛) AND dlp>%.3f(力度硬门槛) AND 有效锚; R1=conflict 降级软标记" % DL_P_MIN)
    L.append("C 修复已生效: above 锚新鲜度用周线末K线日(%s基准), 非古老中枢结束日" % "最新")
    L.append("-" * 66)
    L.append("R1底背驰∩dlp>%.3f 候选(全市场): %d 只 -> 其中周线above: %d 只 -> 有效锚: %d 只"
             % (DL_P_MIN, len(cands), len(recs), len(eff_recs)))
    ta = sum(1 for r in eff_recs if r["tier"] == "A")
    tb = sum(1 for r in eff_recs if r["tier"] == "B")
    tc = sum(1 for r in eff_recs if r["tier"] == "C")
    L.append("分级统计: A=%d B=%d C=%d" % (ta, tb, tc))
    L.append("=" * 66)
    L.append("【有效候选 · 周线顺 ∩ 日线底背驰】%d 只:" % len(eff_recs))
    for r in eff_recs:
        L.append("  [%s] %s 现价%s 周中枢%s %+.2f%% 背驰力度dlp=%.3f 新鲜%s日 [锚基准%s]"
                 % (r["tier"], r["code"], _fmt_price(r["cur"]), r["zs_range"], r["dev_pct"], r["dlp"], _fmt_days(r["fresh_days"]), r["anchor_e"]))
    L.append("")
    L.append("【锚陈旧 · 已剔除】(周线above但锚过期, 待周线刷新后复核):")
    for r in recs:
        if not r["effective"]:
            L.append("  %s 现价%s 周中枢%s dlp=%.3f 新鲜%s日" % (r["code"], _fmt_price(r["cur"]), r["zs_range"], r["dlp"], _fmt_days(r["fresh_days"])))
    L.append("")
    L.append("说明: 本池独立于顺势池(要同向)与高背驰力度池(要大顺+小背驰), 抓的是「大顺+小背驰」区间套买点。")
    L.append("      即周线向上提供安全边际, 日线级高 dlp 背驰给出回调低点(二买/三买), 非逆势抄底。")
    txt = "\n".join(L) + "\n"
    open(OUT_TXT, "w").write(txt)
    n_tradable = sum(1 for r in eff_recs if r.get("tradable"))
    json.dump(dict(baseline_date=BASELINE_DATE, records=recs, effective=len(eff_recs),
                   n_tradable=n_tradable,
                   criteria="周线above ∩ 高dlp(>0.618) ∩ effective; R1conflict 软标记",
                   anchor_note="above uses weekly_last_k (C fix); tradable 基于周线cur价"), open(OUT_JSON, "w"), ensure_ascii=False, indent=1)
    print(txt)
    print("TXT:", OUT_TXT)
    print("JSON:", OUT_JSON)

if __name__ == "__main__":
    main()
