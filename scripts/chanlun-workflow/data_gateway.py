#!/usr/bin/env python3
"""数据源统一网关 CLI (2026-10-10)
用法:
  python3 data_gateway.py --src eastmoney --code 000001 --tf 5m
  python3 data_gateway.py --src sina --code 000001 --tf 5m
  python3 data_gateway.py --src baostock --code 000001 --tf day
输出: JSON {"data": [...], "count": N} 或 {"error": "..."}
业务代码只调网关，不直调 akshare/Sina，换源只需改 --src。
"""
import argparse, json, sys
from datetime import datetime, timedelta, timezone

SH_TZ = timezone(timedelta(hours=8))

def fetch_eastmoney_minute(code, tf):
    import akshare as ak
    import pandas as pd
    prefix = "sh" if code.startswith("6") else "sz"
    period_map = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "1m": "1"}
    df = ak.stock_zh_a_minute(symbol=f"{prefix}{code}",
                              period=period_map.get(tf, "5"), adjust="qfq")
    if df is None or len(df) == 0:
        return None
    # 统一列名: day, open, high, low, close, volume, amount
    return df

def fetch_sina_minute(code, tf):
    import urllib.request, pandas as pd
    prefix = "sh" if code.startswith("6") else "sz"
    scale = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(tf, 5)
    url = (f"https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{code}&scale={scale}&ma=no&datalen=1970")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        j = json.loads(resp.read().decode("utf-8", "ignore"))
    data = j.get("result", {}).get("data", [])
    if not data:
        return None
    df = pd.DataFrame(data)
    df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S")
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["amount"] = pd.to_numeric(df.get("amount", 0), errors="coerce")
    return df[["day", "open", "high", "low", "close", "volume", "amount"]]

def fetch_eastmoney_day(code):
    import akshare as ak
    prefix = "sh" if code.startswith("6") else "sz"
    df = ak.stock_zh_a_daily(symbol=f"{prefix}{code}", adjust="qfq")
    if df is None or len(df) == 0:
        return None
    return df

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, choices=["eastmoney", "sina", "baostock"])
    ap.add_argument("--code", required=True)
    ap.add_argument("--tf", required=True,
                    choices=["day", "5m", "15m", "30m", "60m", "1m"])
    a = ap.parse_args()

    try:
        df = None
        if a.tf == "day":
            if a.src == "eastmoney":
                df = fetch_eastmoney_day(a.code)
            elif a.src == "baostock":
                print(json.dumps({"error": "baostock day not yet implemented"}))
                sys.exit(1)
            else:
                print(json.dumps({"error": f"sina day not supported"}))
                sys.exit(1)
        else:
            if a.src == "eastmoney":
                df = fetch_eastmoney_minute(a.code, a.tf)
            elif a.src == "sina":
                df = fetch_sina_minute(a.code, a.tf)
            else:
                print(json.dumps({"error": f"baostock minute not supported"}))
                sys.exit(1)

        if df is None or len(df) == 0:
            print(json.dumps({"error": "empty_result", "src": a.src,
                              "code": a.code, "tf": a.tf}))
            sys.exit(1)

        # 统一输出: records
        recs = df.to_dict(orient="records")
        # 转 str 防 JSON 序列化问题
        out = []
        for r in recs:
            out.append({k: (str(v) if not isinstance(v, (int, float)) else v)
                        for k, v in r.items()})
        print(json.dumps({"data": out, "count": len(out),
                          "src": a.src, "code": a.code, "tf": a.tf},
                         ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"error": f"{type(e).__name__}: {e}",
                          "src": a.src, "code": a.code, "tf": a.tf}))
        sys.exit(1)

if __name__ == "__main__":
    main()
