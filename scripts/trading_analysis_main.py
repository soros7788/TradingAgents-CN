#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import re
import time
from datetime import datetime, timezone, timedelta

import requests


HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://quote.eastmoney.com/",
}


def eastmoney_secid(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return f"1.{code}"   # 上海
    return f"0.{code}"       # 深圳


def to_float(v):
    try:
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def fetch_eastmoney_30m(code: str, lmt: int = 260):
    """
    东方财富 30分钟K线。
    GitHub Actions 可直接跑，不依赖 akshare，不需要 Docker。
    """
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    params = {
        "secid": eastmoney_secid(code),
        "klt": "30",
        "fqt": "1",
        "lmt": str(lmt),
        "end": "20500101",
        "iscca": "1",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "ut": "fa5fd1943c7b386f172d6893dbfba10b",
    }

    last_err = None

    for _ in range(3):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            data = r.json()

            klines = (
                data.get("data", {}).get("klines")
                if isinstance(data.get("data"), dict)
                else []
            )

            bars = []

            for line in klines or []:
                parts = str(line).split(",")

                if len(parts) < 6:
                    continue

                t = parts[0]
                o = to_float(parts[1])
                c = to_float(parts[2])
                h = to_float(parts[3])
                l = to_float(parts[4])
                v = to_float(parts[5])

                if all(x is not None for x in [o, h, l, c]):
                    bars.append({
                        "time": t,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    })

            if len(bars) >= 65:
                return bars

            last_err = f"东方财富返回数据不足：{len(bars)}"
        except Exception as e:
            last_err = str(e)
            time.sleep(1)

    raise RuntimeError(f"{code} 获取30分钟K线失败：{last_err}")


def pct(a, b):
    if b in (0, None):
        return None
    return (a - b) / b * 100


def near(a, b, tolerance=0.003):
    if a is None or b is None or b == 0:
        return False
    return abs(a - b) / abs(b) <= tolerance


def analyze_one(code: str):
    bars = fetch_eastmoney_30m(code)

    latest = bars[-1]
    hist = bars[:-1]

    last_price = latest["close"]

    high_5 = max(x["high"] for x in hist[-5:])
    high_20 = max(x["high"] for x in hist[-20:])
    high_60 = max(x["high"] for x in hist[-60:])

    low_2 = min(x["low"] for x in hist[-2:])
    low_5 = min(x["low"] for x in hist[-5:])

    structure_high = high_60
    breakout_pct = pct(last_price, structure_high)

    cond_a = near(high_5, high_20, 0.003) and near(high_20, high_60, 0.003)
    cond_b = low_2 >= low_5
    cond_d = last_price > structure_high

    score = 0
    if cond_a:
        score += 30
    if cond_b:
        score += 20
    if cond_d:
        score += 50

    if cond_d and cond_a and cond_b:
        signal = "BREAKOUT"
        decision = "触发突破，进入重点观察"
    elif cond_a and cond_b:
        signal = "WATCH"
        decision = "结构压缩，等待突破"
    elif cond_d:
        signal = "WEAK_BREAKOUT"
        decision = "价格突破，但结构条件不完整"
    else:
        signal = "NONE"
        decision = "暂未触发"

    return {
        "code": code,
        "latest_time": latest["time"],
        "last_price": round(last_price, 3),
        "structure_high_60": round(structure_high, 3),
        "breakout_pct": None if breakout_pct is None else round(breakout_pct, 2),
        "conditions": {
            "A_high_resonance_5_20_60": cond_a,
            "B_low_not_breaking_2_vs_5": cond_b,
            "D_price_above_structure_high": cond_d,
        },
        "score": score,
        "signal": signal,
        "decision": decision,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stocks", default="000001,000006,002463")
    parser.add_argument("--analysis_type", default="quick")
    parser.add_argument("--output", default="json")
    args = parser.parse_args()

    stocks = [x.strip() for x in args.stocks.split(",") if x.strip()]

    bj = datetime.now(timezone.utc) + timedelta(hours=8)

    results = []
    errors = []

    for code in stocks:
        try:
            results.append(analyze_one(code))
        except Exception as e:
            errors.append({
                "code": code,
                "error": str(e),
            })

    candidates = [
        x for x in results
        if x["signal"] in ("BREAKOUT", "WATCH", "WEAK_BREAKOUT")
    ]

    candidates.sort(key=lambda x: x["score"], reverse=True)

    output = {
        "title": "TradingAgents-CN 30分钟突破扫描",
        "beijing_time": bj.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_type": args.analysis_type,
        "stocks": stocks,
        "summary": {
            "total": len(stocks),
            "ok": len(results),
            "errors": len(errors),
            "candidates": len(candidates),
        },
        "candidates": candidates,
        "all_results": results,
        "errors": errors,
        "rule": {
            "A": "近5/20/60根30分钟K线高点共振",
            "B": "近2根低点不低于近5根低点",
            "D": "当前价突破近60根结构高点",
        },
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
