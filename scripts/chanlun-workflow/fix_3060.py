
#!/usr/bin/env python3
"""fix_3060.py — 用 akshare 重拉 30m/60m, 直接写 {code}_30m.csv/{code}_60m.csv, 过滤坏行/半空行, local+gdrive 双写。
用法: python3 fix_3060.py --codes 002042,002043,002046
"""
import argparse, os, sys, time, json
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

def clean_df(df):
    # 统一列名 day
    if "date" in df.columns and "day" not in df.columns:
        df = df.rename(columns={"date": "day"})
    df["day"] = df["day"].astype(str)
    for c in ("open", "high", "low", "close", "volume"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) == 0:
        return df
    mask = (df["high"] >= df[["open", "close"]].max(axis=1) - 1e-9) & \
           (df["low"] <= df[["open", "close"]].min(axis=1) + 1e-9) & \
           (df["high"] >= df["low"])
    return df[mask]

def fix_tf(code, tf):
    sym = prefix(code) + code
    try:
        df = ak.stock_zh_a_minute(symbol=sym, period=tf, adjust="")
    except Exception as e:
        return f"{tf} ERR {type(e).__name__}: {str(e)[:80]}"
    if df is None or len(df) == 0:
        return f"{tf} empty"
    cols = ["day", "open", "high", "low", "close", "volume", "amount"]
    df = df[[c for c in cols if c in df.columns]]
    df = clean_df(df)
    df = df.sort_values("day").drop_duplicates(subset=["day"], keep="last")
    suffix = tf + "m"
    for root in (LO, GD):
        p = os.path.join(root, f"{code}_{suffix}.csv")
        old = read_csv(p)
        if old is not None and len(old):
            oldc = clean_df(old)
            oldc = oldc[[c for c in df.columns if c in oldc.columns]]
            merged = pd.concat([oldc, df], ignore_index=True)
            merged = merged.sort_values("day").drop_duplicates(subset=["day"], keep="last")
        else:
            merged = df
        merged.to_csv(p, index=False)
    return f"{tf} OK fresh={len(df)} total={len(merged)}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None)
    args = ap.parse_args()
    codes = [c.strip() for c in (args.codes or "").split(",") if c.strip()]
    if not codes:
        print("no codes"); return
    print(f"[fix3060] {len(codes)} codes -> {codes}", flush=True)
    for code in codes:
        for tf in ("30", "60"):
            r = fix_tf(code, tf)
            print(f"  {code} {r}", flush=True)
            time.sleep(1.0)
    print("[fix3060] DONE", flush=True)

if __name__ == "__main__":
    main()
