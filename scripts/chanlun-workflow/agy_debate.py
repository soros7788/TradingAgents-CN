#!/usr/bin/env python3
"""Agy 多角色辩论：读 grade_pool_plan 草稿，对 PASS 标的做四角色评审.

用法:
    python3 agy_debate.py --pool-dir ~/chan_logs --date 20261007 --out-dir ~/chan_logs

输入: {pool-dir}/trend_pool_graded_{date}.json (grade_pool_plan 产物)
输出: {out-dir}/agy_debate_{date}.json
"""
import argparse
import glob
import json
import os
import subprocess
import sys

AGY_BIN = "/home/katelolita7788/.local/bin/agy"
MODEL = os.environ.get("AGY_DEBATE_MODEL", "claude-sonnet-5-5-low")
TIMEOUT = int(os.environ.get("AGY_DEBATE_TIMEOUT", "120"))
# 只辩论前 N 只，避免额度爆炸
MAX_DEBATE = int(os.environ.get("AGY_DEBATE_MAX", "10"))

TECH_PROMPT = """你是缠论技术分析师。只基于以下技术指标发言，不许编造基本面。
标的:{code} {name} 价格:{price} 递归A:{r1} 门禁:{gate} dlp:{dlp}
输出:1.多头依据(2条) 2.空头依据(2条) 3.结论:看多/看空/中性"""

RISK_PROMPT = """你是风控官。只基于风控指标发言。
标的:{code} {name} 价格:{price} v5:{v5state} 分级:{tier} 门禁:{gate}
输出:1.风险点(2条) 2.仓位建议:正常/减半/观望"""

SENTI_PROMPT = """你是市场情绪观察员。只谈大盘环境。
标的:{code} {name} 门禁:{gate} 分级:{tier}
输出:1.环境适合激进/谨慎/观望 2.情绪影响:正面/负面/中性"""

TRADER_PROMPT = """你是交易员。综合三方意见做最终裁决。
技术:{tech}
风控:{risk}
情绪:{senti}
标的:{code} {name} 原分级:{tier} 门禁:{gate}
输出:1.裁决:保留/降级/剔除 2.标注语(一句话)"""


def agy_ask(prompt):
    cmd = [AGY_BIN, "--model", MODEL, "-p", prompt]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return "[timeout]"
    except FileNotFoundError:
        return "[agy not found, skip]"
    return (r.stdout or r.stderr or "[empty]").strip()[:2000]


def debate_one(rec):
    code = rec.get("code", "")
    ctx = dict(
        code=code,
        name=rec.get("name", ""),
        price=rec.get("price", ""),
        r1=rec.get("r1_dir", rec.get("recursive_direction", "unknown")),
        gate=rec.get("gate", "PASS"),
        dlp=rec.get("dlp", 0.0),
        v5state=rec.get("v5_state", "无"),
        tier=rec.get("tier", "B"),
    )
    tech = agy_ask(TECH_PROMPT.format(**ctx))
    risk = agy_ask(RISK_PROMPT.format(**ctx))
    senti = agy_ask(SENTI_PROMPT.format(**ctx))
    trader = agy_ask(TRADER_PROMPT.format(tech=tech, risk=risk, senti=senti, **ctx))
    return {"code": code, "tech": tech, "risk": risk,
            "senti": senti, "trader": trader}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool-dir", required=True)
    ap.add_argument("--date", required=True)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()

    # 找 grade_pool_plan 产物
    pat = os.path.join(a.pool_dir, f"trend_pool_graded_{a.date}.json")
    files = glob.glob(pat)
    if not files:
        print(f"[agy-debate] 找不到 {pat}，跳过", flush=True)
        return 0
    src = files[0]
    d = json.load(open(src, encoding="utf-8"))

    # 取 PASS 候选（items 或 records）
    items = d.get("items") or d.get("records") or []
    cands = [r for r in items if r.get("gate", "PASS") == "PASS"][:MAX_DEBATE]
    print(f"[agy-debate] 输入 {os.path.basename(src)}，候选 {len(cands)} 只 "
          f"(上限 {MAX_DEBATE})，模型 {MODEL}", flush=True)

    results = []
    for i, rec in enumerate(cands, 1):
        print(f"[agy-debate] [{i}/{len(cands)}] {rec.get('code')}", flush=True)
        try:
            results.append(debate_one(rec))
        except Exception as e:
            print(f"[agy-debate] {rec.get('code')} 失败: {e}", flush=True)
            results.append({"code": rec.get("code"), "error": str(e)})

    out = {"date": a.date, "model": MODEL, "source": os.path.basename(src),
           "n": len(results), "debates": results}
    opath = os.path.join(a.out_dir, f"agy_debate_{a.date}.json")
    tmp = opath + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    os.replace(tmp, opath)
    print(f"[agy-debate] 输出 {opath}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
