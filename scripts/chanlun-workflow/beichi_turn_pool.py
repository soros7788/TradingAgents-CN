#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高背驰力度池 (High-Divergence Pool) —— 与「两级联立顺势池」并行的第二口径。

定位(实测语义, 经人工复核 v1140 确认):
  本池收的是「大级别顺势向上 + 小级别高 dlp 背驰力度」票 —— 即上升趋势中的
  回调买点 / 类二买 / 中继, 而非"逆势抄底转折底"。
  原因: 双引擎 final_verdict 体系下, 高 dlp 强背驰票被归为 aligned/partial(大级别顺),
        极少判 conflict(逆势); 故原"转折"命名是 semantic mismatch, 此处更名对齐。

本池选取条件(修复后):
  1) dlp > DL_P_MIN (默认 0.618)  —— 硬门槛(背驰力度衰竭, 本质判据), 非方向门槛
  2) final_verdict == "conflict" 降级为软标记(纯加分, 不卡门槛)
  3) price 有效(非 NaN/None/<=0)  —— 可交易门槛, NaN 价票入"仅观察"副池
  注: 原硬 AND(conflict AND 高dlp) 在双引擎 verdict 体系下结构性互斥(交集恒 0), 故放开方向门槛。

数据来源: 单一 dualscan json (WN_SCAN 显式指定, 或 mtime 最新), 不合并历史。
零 sealed 写入: 不碰 level_alignment / recursive_core / interval_engine。
"""
import os
import sys
import json
import glob
from datetime import datetime

REPO_DIR = "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow"
sys.path.insert(0, REPO_DIR)

LOGDIR = os.path.expanduser("~/chan_logs")
DL_P_MIN = float(os.environ.get("DL_P_MIN", "0.618"))
OUT_CODES = os.environ.get("BT_CODES_OUT", "/tmp/beichi_turn_codes.txt")
OUT_JSON = os.environ.get("BT_JSON_OUT", "/tmp/beichi_turn_pool.json")


def grade_tier(dlp, is_conflict=False):
    # P2-3 修复 (2026-10-01): 调用方已保证 dlp > DL_P_MIN(0.618), 旧的无条件 C 分支为死代码;
    #   保留一行防御性兜底, 其余仅区分 A/B。
    #   若兼具方向分歧(原 conflict 语义)则升 A(转折核心), 以保留原 design intent。
    if not dlp or dlp <= DL_P_MIN:
        return "C"
    if dlp >= 1.0:
        return "A"
    return "A" if is_conflict else "B"


def resolve_scan_json():
    explicit = os.environ.get("WN_SCAN", "").strip()
    if explicit:
        p = explicit if os.path.isabs(explicit) else os.path.join(LOGDIR, explicit)
        if not os.path.exists(p):
            raise RuntimeError("指定的 json 不存在: %s" % p)
        return p
    fs = sorted(glob.glob(os.path.join(LOGDIR, "dualscan_*.json")), key=os.path.getmtime)
    if not fs:
        raise RuntimeError("%s 下无 dualscan_*.json" % LOGDIR)
    return fs[-1]


def main():
    # ---- Regime Gate (2026-10-03 v3) ----
    # 池子永远跑，regime 只调阈值/仓位。个股可有独立于指数的趋势。
    from regime_gate import get_regime, pool_config, gen_run_id
    _regime, _detail = get_regime()
    _cfg = pool_config("beichi_turn", _regime)
    _run_id = gen_run_id("beichi_turn")
    _thr_mult = _cfg["threshold_mult"]
    print("[beichi-turn] run_id=%s regime=%s mode=%s thr_mult=%.1f size_mult=%.1f primary=%s %s"
          % (_run_id, _regime, _cfg["mode"], _thr_mult, _cfg["size_mult"], _cfg["primary"], _detail), flush=True)
    # 闭环标注 (v1/v5)
    _meta = {"run_id": _run_id, "engine": "v1+v5", "v5_status": "integrated",
             "market_regime": _regime, "pool_mode": _cfg["mode"],
             "threshold_mult": _thr_mult, "size_mult": _cfg["size_mult"],
             "is_primary": _cfg["primary"]}
    # v3: regime 调阈值 (熊市 1.5x, 震荡 1.2x, 牛市 1.0x)
    global DL_P_MIN
    _dlp_min = DL_P_MIN * _thr_mult
    DL_P_MIN = _dlp_min  # 全局生效 (含 grade_tier 防御性兜底)
    print("[beichi-turn] dlp 阈值: %.3f × %.1f = %.3f" % (0.618, _thr_mult, _dlp_min), flush=True)
    f = resolve_scan_json()
    d = json.load(open(f))
    print("数据来源: %s (ts=%s) —— 仅取单一 json，不合并历史"
          % (os.path.basename(f), d.get("ts")), flush=True)

    picked = []
    untradable = []   # ③ NaN 价/无价票: 仅观察, 不进可交易主池
    for it in (d.get("dual") or []):
        ao = it.get("alignment_overall") or {}
        rs = it.get("recursive_summary") or {}
        la = it.get("level_alignment") or {}
        dlp = it.get("dlp")
        price = it.get("price")
        # ③ NaN 价格治理: 无有效价格的票无法下单, 移入"仅观察"副池
        if price is None or not isinstance(price, (int, float)) or price != price or price <= 0:
            untradable.append({
                "code": it.get("code"), "name": it.get("name"),
                "dlp": dlp, "reason": "price_NaN_or_invalid",
            })
            continue
        # 修复: 原硬 AND(final_verdict==conflict AND dlp>DL_P_MIN) 在当前双引擎
        # verdict 体系下结构性互斥 —— 高 dlp 强背驰票被归为 aligned/partial,
        # conflict 票 dlp 全<阈值。改以 dlp 力度为硬门槛(背驰转折=力度衰竭, 本质判据),
        # conflict(方向分歧)降级为软标记, 保留原 design intent。
        if dlp is None or dlp <= DL_P_MIN:
            continue
        is_conflict = ao.get("final_verdict") == "conflict"
        # ② 防御: 空壳记录(ratio=0 且 dlp 临界)仍保留但标记 C, 不入高优先级
        conflicts = [lv for lv, v in la.items() if (v or {}).get("verdict") == "conflict"]
        picked.append({
            "code": it.get("code"), "name": it.get("name"), "price": price,
            "dlp": dlp, "ratio": it.get("ratio"),
            "r0": rs.get("r0_dir"), "r1": rs.get("r1_dir"), "r2": rs.get("r2_dir"),
            "conflict_layers": conflicts, "explanation": ao.get("explanation"),
            "tier": grade_tier(dlp, is_conflict),
            "is_conflict": is_conflict,
            "tradable": True,
        })
    picked.sort(key=lambda r: (-r["dlp"]))
    # v5 标注 (2026-10-03): 每只加 v5_state (盘整/趋势/无)
    # 2026-10-04 修复: 1)异常不再静默吞,必须打日志; 2)保留方向信息
    try:
        from v5_zhongshu import v5_state
        for r in picked:
            try:
                s, direction = v5_state(r["code"])
                r["v5_state"] = s
                r["v5_direction"] = direction  # 保留方向: 趋势-向上/趋势-向下
            except Exception as e:
                # 异常必须可见,不准静默变"无"
                print("[beichi-turn] v5_state 失败 code=%s err=%s" % (r["code"], e), flush=True)
                r["v5_state"] = "无"
                r["v5_direction"] = ""
                r["v5_error"] = str(e)
    except ImportError as e:
        print("[beichi-turn] v5_zhongshu 导入失败: %s" % e, flush=True)
        for r in picked:
            r["v5_state"] = "无"
            r["v5_direction"] = ""
    # v5 牛市过滤 (2026-10-03): BULL 时踢掉 v5_state=盘整 的 (79% 噪音)
    _v5_filtered_n = 0
    if _regime == "BULL":
        for r in picked:
            if r.get("v5_state") == "盘整":
                r["tradable"] = False
                r["v5_excluded"] = True
                _v5_filtered_n += 1
    # 统计 v5 分布
    from collections import Counter
    _v5_dist = Counter(r.get("v5_state", "无") for r in picked)
    print("[beichi-turn] v5分布: %s" % dict(_v5_dist), flush=True)
    if _v5_filtered_n:
        print("[beichi-turn] 牛市 v5 过滤: 踢掉 %d 只盘整股" % _v5_filtered_n, flush=True)
    ta = sum(1 for r in picked if r["tier"] == "A")
    tb = sum(1 for r in picked if r["tier"] == "B")
    tc = sum(1 for r in picked if r["tier"] == "C")

    with open(OUT_CODES, "w") as fh:
        for r in picked:
            fh.write(str(r["code"]) + "\n")
    json.dump({"baseline_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
               "source": os.path.basename(f), "ts": d.get("ts"), "dlp_min": DL_P_MIN,
               "n": len(picked), "tier_stats": {"A": ta, "B": tb, "C": tc},
               "n_untradable": len(untradable), "untradable_items": untradable,
               "items": picked,
               **_meta},  # v3: engine/v1/v5/regime 闭环标注
              open(OUT_JSON, "w"), ensure_ascii=False, indent=1)

    print("高背驰力度池: %d 只可交易 (dlp>%.3f 硬门槛, conflict 软标记, price 有效)" % (len(picked), DL_P_MIN))
    print("分级统计: A=%d B=%d C=%d" % (ta, tb, tc))
    print("仅观察(无价NaN): %d 只 -> 不入主池" % len(untradable))
    print("基准日: %s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("codes -> %s" % OUT_CODES)
    print("json  -> %s" % OUT_JSON)


if __name__ == "__main__":
    main()
