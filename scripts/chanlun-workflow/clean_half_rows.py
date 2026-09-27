
#!/usr/bin/env python3
"""clean_half_rows.py — 删除 5m/1m 缓存中 OHLC 为空的半空行, 保留有效行。

只删半空行(open/high/low/close 任一为空), 不做网络请求。
用法: python3 clean_half_rows.py --codes 002032,002043 [--tf 5m,1m] [--all]
"""
import argparse
import csv
import os
import sys

LO = os.path.expanduser("~/kline_cache_local")
GD = "/home/gorgesoros39/gdrive/TradingAgents-CN/kline_cache"

def clean_file(p):
    if not os.path.exists(p):
        return "MISS"
    with open(p) as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return "EMPTY"
    cols = list(rows[0].keys())
    kept, removed = [], 0
    for r in rows:
        o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
        ok = all(x is not None and str(x).strip() != "" for x in (o, h, l, c))
        if ok:
            kept.append(r)
        else:
            removed += 1
    if removed == 0:
        return f"clean ({len(kept)} rows, 0 removed)"
    with open(p, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(kept)
    return f"cleaned {removed} half-rows, kept {len(kept)}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    ap.add_argument("--tf", default="5m,1m")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    tfs = [t.strip() for t in args.tf.split(",") if t.strip()]
    if args.all:
        codes = sorted({os.path.basename(f)[:6] for f in os.listdir(LO) if f.endswith("_5m.csv")})
    elif args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        print("need --codes or --all")
        return
    print(f"[clean] {len(codes)} codes x tf={tfs}", flush=True)
    for code in codes:
        for tf in tfs:
            for root in (LO, GD):
                r = clean_file(os.path.join(root, f"{code}_{tf}.csv"))
                print(f"  {code}_{tf} [{os.path.basename(root)}] {r}", flush=True)
    print("[clean] DONE", flush=True)

if __name__ == "__main__":
    main()
