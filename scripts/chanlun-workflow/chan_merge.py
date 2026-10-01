#!/usr/bin/env python3
"""chan-merge.py — 合并双机 meet-in 账本 + 候选清单输出.

流程:
  1. 从 VM-B 拉 scan_ledger.jsonl (可经 SSH 远程读)
  2. 去重: 同 code 可能被 VM-A + VM-B 都扫到 (不应该但账本可能有残留)
  3. 按 gate 过滤: 保留 PASS, BLOCKED 作为观察池
  4. 输出: JSON 候选清单 + 控制台摘要

用法:
  python3 chan-merge.py --ledger ~/chan_logs/scan_ledger.jsonl
  python3 chan-merge.py --ledger VM-B:~/chan_logs/scan_ledger.jsonl \
                        --ssh-host katelolita7788@34.4.105.158 \
                        --ssh-key ~/.ssh/hermes_key --out ~/chan_logs/candidates.json
"""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from collections import defaultdict
from pathlib import Path
from datetime import datetime

PASS_GATES = {"PASS"}
BLOCKED_GATES = {"BLOCKED", "TIMEOUT", "UNKNOWN", "ERROR"}

def _ssh_cmd(host, key=None):
    cmd = ["ssh"]
    if key: cmd += ["-i", os.path.expanduser(key)]
    # P1-8 (2026-10-01): no → accept-new（治理要求；TOOLS.md 已有约定）
    cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new", host]
    return cmd

def read_ledger(ledger, ssh_host=None, ssh_key=None):
    """读账本 — 本地或远程 SSH."""
    if ssh_host:
        proc = subprocess.run(
            _ssh_cmd(ssh_host, ssh_key) + [f"cat {ledger} 2>/dev/null"],
            capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise RuntimeError(f"SSH 读账本失败: {proc.stderr.strip()[:200]}")
        lines = proc.stdout.strip().splitlines()
    else:
        p = Path(os.path.expanduser(ledger))
        if not p.exists():
            raise FileNotFoundError(f"账本不存在: {p}")
        lines = p.read_text().splitlines()
    records = []
    for ln in lines:
        ln = ln.strip()
        if not ln: continue
        try: records.append(json.loads(ln))
        except json.JSONDecodeError: continue
    return records

def dedupe(records):
    """同 code 去重.

    2026-09-23 修复 (WORKBUDDY):
    旧逻辑在 PASS_GATES 子集内取最新 -> 更晚写入的非 PASS 行
    (改判 BLOCKED / 被 JEV 拒为 PASS_JEVN) 被整体忽略, 陈旧 PASS 残留候选池.
    新逻辑: 每 code 按 ts 取【唯一最新一条】, 再交由 classify() 按 gate 分类.
    同时统计 stale_superseded: 曾有 PASS 记录、但被更晚的非 PASS 行覆盖的 code 数.
    """
    by_code: dict[str, list] = defaultdict(list)
    for r in records:
        c = r.get("code", "")
        if not c:
            continue
        by_code[c].append(r)
    out = {}
    stale_superseded = 0
    for code, rs in by_code.items():
        sorted_rs = sorted(rs, key=lambda x: x.get("ts", ""))
        latest = sorted_rs[-1]
        older_pass = [r for r in sorted_rs[:-1] if r.get("gate") in PASS_GATES]
        if older_pass and latest.get("gate") not in PASS_GATES:
            stale_superseded += 1
        out[code] = latest
    return out, stale_superseded

def classify(records):
    """按 gate 分类."""
    buckets = defaultdict(list)
    for code, r in records.items():
        g = r.get("gate", "UNKNOWN")
        buckets[g].append(r)
    # PASS 里优先选有价格/信号信息的
    for g in buckets:
        buckets[g].sort(key=lambda x: x.get("ts", ""))
    return dict(buckets)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", default="~/chan_logs/scan_ledger.jsonl")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-key", default=None)
    ap.add_argument("--out", default=None, help="输出 JSON 路径 (默认 stdout)")
    ap.add_argument("--date", default=None, help="扫描日 YYYYMMDD：只保留 ts 日期 >= 该日的记录（日期过滤，2026-09-27）")
    args = ap.parse_args()

    t0 = time.time()
    records = read_ledger(args.ledger, args.ssh_host, args.ssh_key)
    # 日期过滤 (2026-09-27)：只保留 ts 日期 >= 扫描日的记录，陈旧记录不再混入 merged
    date_filtered_out = 0
    if args.date:
        try:
            dstr = datetime.strptime(args.date, "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            print(f"错误: --date 须为 YYYYMMDD 格式，收到: {args.date}", file=sys.stderr)
            return 2
        before = len(records)
        records = [r for r in records
                   if isinstance(r.get("ts"), str) and len(r["ts"]) >= 10 and r["ts"][:10] >= dstr]
        date_filtered_out = before - len(records)
    deduped, stale_superseded = dedupe(records)
    buckets = classify(deduped)

    total = len(records)
    uniq = len(deduped)

    # F3 (2026-09-23 WORKBUDDY): 观察池 — 被 JEV 二次门禁降级的 PASS_JEVN
    # 与判中性的 NEUTRAL, 不再静默消失, 列入 watch_candidates 供下游审计/展示.
    watch = buckets.get("PASS_JEVN", []) + buckets.get("NEUTRAL", [])
    result = {
        "meta": {
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source": args.ledger,
            "total_records": total,
            "unique_codes": uniq,
            "stale_superseded": stale_superseded,
            "date_filtered_out": date_filtered_out,
            "watch_count": len(watch),
            "vm_A_count": sum(1 for r in deduped.values() if r.get("vm") == "A"),
            "vm_B_count": sum(1 for r in deduped.values() if r.get("vm") == "B"),
            "elapsed_s": round(time.time() - t0, 2),
        },
        "gates": {g: len(rs) for g, rs in buckets.items()},
        "pass_candidates": buckets.get("PASS", []),
        "blocked_observations": buckets.get("BLOCKED", []),
        "watch_candidates": watch,  # F3: 观察池, 不再静默丢弃
    }

    out_str = json.dumps(result, ensure_ascii=False, indent=2)
    if args.out:
        with open(os.path.expanduser(args.out), "w") as f:
            f.write(out_str)
        print(f"📄 输出 → {args.out}  ({len(buckets.get('PASS', []))} PASS 候选)", flush=True)
    else:
        print(out_str)

    # 控制台摘要
    m = result["meta"]
    print(f"\n{'='*60}")
    print(f"chan-merge 摘要")
    print(f"{'='*60}")
    print(f"源: {m['source']}")
    print(f"原始记录: {m['total_records']}  去重后: {m['unique_codes']}")
    print(f"stale_superseded (曾被PASS、后被更晚非PASS覆盖): {m['stale_superseded']}")
    print(f"日期过滤剔除 (ts 早于扫描日): {m['date_filtered_out']}")
    print(f"watch_candidates (观察池 PASS_JEVN+NEUTRAL): {m['watch_count']}")
    print(f"VM-A 写入: {m['vm_A_count']}  VM-B 写入: {m['vm_B_count']}")
    for g, n in result["gates"].items():
        print(f"  {g}: {n}")
    passes = result["pass_candidates"]
    if passes:
        print(f"\n★ PASS 候选 ({len(passes)} 只):")
        for r in passes[:20]:
            print(f"  {r['code']}  vm={r['vm']}  ts={r.get('ts','')}")
        if len(passes) > 20:
            print(f"  ... 还有 {len(passes)-20} 只")
    print(f"{'='*60}", flush=True)

if __name__ == "__main__":
    sys.exit(main() or 0)
