
#!/usr/bin/env python3
"""fix_cache.py — 用 akshare 增量修复 5m/day 缓存脏行与缺失。

用法: python3 fix_cache.py --codes 002043,002044  (或 --all)
对每只标的:
  - 5m: ak.stock_zh_a_minute(symbol=sz/sh+code, period="5") 全量重拉(最近~1970根)
        与本地缓存按 day 去重合并(保留更早历史)
  - day: ak.stock_zh_a_daily(symbol=sz/sh+code, start_date=..., end_date=...) 增量
        与本地缓存按 date 去重合并
写回 ~/kline_cache_local 与 gdrive 共享盘缓存两份。
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta

import akshare as ak
import pandas as pd

LO = os.path.expanduser("~/kline_cache_local")
GD = "/home/gorgesoros39/gdrive/TradingAgents-CN/kline_cache"

def prefix(code):
    return "sz" if code.startswith(("0", "3")) else "sh"

def read_csv(p):
    if not os.path.exists(p):
        return None
    try:
        return pd.read_csv(p, dtype={"day": str, "date": str})
    except Exception:
        return None

def write_csv(p, df, cols):
    df = df[cols].copy()
    df.to_csv(p, index=False)

def fix_5m(code):
    sym = prefix(code) + code
    try:
        df = ak.stock_zh_a_minute(symbol=sym, period="5", adjust="")
    except Exception as e:
        return f"5m ERR {type(e).__name__}: {str(e)[:80]}"
    if df is None or len(df) == 0:
        return "5m empty"
    df = df.rename(columns={"day": "day"})
    df["day"] = df["day"].astype(str)
    # 需要列: day,open,high,low,close,volume,amount
    cols = ["day", "open", "high", "low", "close", "volume", "amount"]
    df = df[[c for c in cols if c in df.columns]]
    # 丢弃 OHLC 空行
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[df["close"].astype(str).str.strip() != ""]
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("day").drop_duplicates(subset=["day"], keep="last")
    # 合并本地历史
    for root in (LO, GD):
        p = os.path.join(root, f"{code}_5m.csv")
        old = read_csv(p)
        if old is not None and "day" in old.columns:
            old["day"] = old["day"].astype(str)
            merged = pd.concat([old[df.columns], df], ignore_index=True)
        else:
            merged = df
        merged = merged.sort_values("day").drop_duplicates(subset=["day"], keep="last")
        write_csv(p, merged, list(df.columns))
    return f"5m OK {len(df)} rows fresh"

def fix_day(code):
    sym = prefix(code) + code
    end = datetime.now().strftime("%Y%m%d")
    start = (datetime.now() - timedelta(days=45)).strftime("%Y%m%d")
    try:
        df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="")
    except Exception as e:
        return f"day ERR {type(e).__name__}: {str(e)[:80]}"
    if df is None or len(df) == 0:
        return "day empty"
    df["date"] = df["date"].astype(str)
    cols = ["date", "open", "high", "low", "close", "volume", "amount", "outstanding_share", "turnover"]
    df = df[[c for c in cols if c in df.columns]]
    df = df.dropna(subset=["close"])
    df = df.sort_values("date").drop_duplicates(subset=["date"], keep="last")
    for root in (LO, GD):
        p = os.path.join(root, f"{code}_day.csv")
        old = read_csv(p)
        if old is not None and "date" in old.columns:
            old["date"] = old["date"].astype(str)
            # 只追加比本地新的行, 保留本地更早历史(本地 day 从2005年起, akshare 45天增量)
            merged = pd.concat([old, df], ignore_index=True)
        else:
            merged = df
        merged = merged.sort_values("date").drop_duplicates(subset=["date"], keep="last")
        write_csv(p, merged, list(df.columns))
    return f"day OK {len(df)} rows fresh"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    ap.add_argument("--limit", type=int, default=0, help="从账本尾部取最近N只(0=不用)")
    args = ap.parse_args()
    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    elif args.limit:
        led = os.path.expanduser("~/chan_logs/scan_ledger.jsonl")
        seen, out = set(), []
        if os.path.exists(led):
            for line in open(led):
                try:
                    import json
                    d = json.loads(line)
                    c = d.get("code")
                    if c and c not in seen:
                        seen.add(c); out.append(c)
                except Exception:
                    pass
        codes = out[-args.limit:]
    if not codes:
        print("no codes")
        return
    print(f"[fix] {len(codes)} codes -> {codes}", flush=True)
    for code in codes:
        r5 = fix_5m(code)
        print(f"  {code} {r5}", flush=True)
        time.sleep(1.2)  # 限流保护
        rd = fix_day(code)
        print(f"  {code} {rd}", flush=True)
        time.sleep(1.2)
    print("[fix] DONE", flush=True)

if __name__ == "__main__":
    main()
