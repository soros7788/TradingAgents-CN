#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""周线区间套 批量版 v2 —— 对所有「两级联立」标的跑周线宏观判定。

口径与 repo 单只版 weekly_nesting.py 完全一致（直接复用其 analyze），
保证单只与批量结果可互证。

两级联立 = 默认按「递归 R0+R1 双 pass」: alignment_overall.present_r_levels >= 2
          (至少 2 个递归层算出方向)。
          原「两层全对齐(aligned_r_levels==2)」口径实测恒为 0, 已降为可选项
          (LIANLI_MODE=align2 回退)。

数据来源: 单一 dualscan json (WN_SCAN 显式指定, 或 mtime 最新), 不合并历史 json,
          避免陈旧/混合数据源污染当日结论。

联立口径: 默认 pass2 (双pass = present_r_levels >= 2, 即至少 2 个递归层算出方向),
          对齐用户强制标准「递归 R0+R1 双 pass」; 可用 LIANLI_MODE=align2 回退到
          原「两层全对齐(aligned_r_levels==2)」口径 —— 该口径实测恒为 0。
"""
import os
import sys
import json
import glob

REPO_DIR = "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow"
sys.path.insert(0, REPO_DIR)
import weekly_nesting as wn  # noqa: E402

LOGDIR = os.path.expanduser("~/chan_logs")
WEEKLY_AMP = float(os.environ.get("WEEKLY_AMP", "1.0"))
OUT = os.environ.get("WN_OUT", "/tmp/wn_lianli2_v2.json")
# 联立口径开关: pass2 = 双pass(默认, 用户强制标准); align2 = 原两层全对齐口径(恒为 0)
LIANLI_MODE = os.environ.get("LIANLI_MODE", "pass2").strip().lower()


def _resolve_scan_json():
    """解析唯一输入 dualscan json。

    治理要求: 拒绝跨历史 json 合并(会引入陈旧/混合数据源)。
    优先取 WN_SCAN 显式指定的 json; 否则取 LOGDIR 下 mtime 最新的一份。
    """
    explicit = os.environ.get("WN_SCAN", "").strip()
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(LOGDIR, explicit)
        if not os.path.exists(p):
            raise RuntimeError("WN_SCAN 指定的 json 不存在: %s" % p)
        return p
    fs = sorted(glob.glob(os.path.join(LOGDIR, "dualscan_*.json")), key=os.path.getmtime)
    if not fs:
        raise RuntimeError("%s 下无 dualscan_*.json" % LOGDIR)
    return fs[-1]


def pass_codes_lianli2():
    f = _resolve_scan_json()
    d = json.load(open(f))
    print("  数据来源: %s (ts=%s) —— 仅取单一 json，不合并历史"
          % (os.path.basename(f), d.get("ts")), flush=True)
    print("  联立口径: %s" % ("双pass(present_r_levels>=2)" if LIANLI_MODE == "pass2"
                          else "两层全对齐(aligned_r_levels==2)"), flush=True)
    out = []
    tiers = {"aligned2": 0, "aligned1": 0, "aligned0": 0}
    for it in (d.get("dual") or []):
        c = it.get("code")
        if not c:
            continue
        ao = it.get("alignment_overall") or {}
        if LIANLI_MODE == "pass2":
            ok = (ao.get("present_r_levels") or 0) >= 2
        else:
            ok = ao.get("aligned_r_levels") == 2
        if not ok:
            continue
        out.append(c)
        arl = ao.get("aligned_r_levels") or 0
        if arl >= 2:
            tiers["aligned2"] += 1
        elif arl == 1:
            tiers["aligned1"] += 1
        else:
            tiers["aligned0"] += 1
    print("  分级: 两层全对齐=%d  单层对齐=%d  未对齐=%d"
          % (tiers["aligned2"], tiers["aligned1"], tiers["aligned0"]), flush=True)
    return sorted(out)


def main():
    codes = pass_codes_lianli2()
    print("两级联立(2/2 层对齐) 标的数量: %d  30min_amp=%.2f%%  WEEKLY_AMP=%.2f%%"
          % (len(codes), wn.LEVEL_AMP["30min"], WEEKLY_AMP), flush=True)
    results = {}
    for i, c in enumerate(codes):
        if i % 50 == 0:
            print("    进度 %d/%d" % (i, len(codes)), flush=True)
        try:
            r = wn.analyze(c, weekly_amp=WEEKLY_AMP)
        except Exception as e:
            r = {"code": c, "error": "%s: %s" % (type(e).__name__, str(e)[:80])}
        results[c] = r
    json.dump(results, open(OUT, "w"), ensure_ascii=False, indent=1)

    ok = [c for c, r in results.items() if r.get("nesting_ok") is True]
    bad = [c for c, r in results.items() if r.get("nesting_ok") is False]
    err = [c for c, r in results.items() if r.get("error")]
    lv = results and list(results.values())[0]
    pos = {}
    for c, r in results.items():
        d = (r.get("levels") or {}).get("周线")
        if d:
            pos[d.get("pos")] = pos.get(d.get("pos"), 0) + 1
    print("完成: 总%d  嵌套合规%d  违规%d  错误%d" % (len(results), len(ok), len(bad), len(err)))
    print("周线位置分布: %s" % pos)
    # 级别缺失统计
    miss = {"周线": 0, "日线": 0, "30min": 0, "5min": 0}
    for c, r in results.items():
        for k in miss:
            if not (r.get("levels") or {}).get(k):
                miss[k] += 1
    print("各级无有效中枢数: %s" % miss)
    print("落盘: %s" % OUT)


if __name__ == "__main__":
    main()
