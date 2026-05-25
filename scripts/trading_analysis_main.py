#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import argparse
import json
import math
import re
import sys
import time
from datetime import datetime, timezone, timedelta

import requests


def market_prefix(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return code


def to_float(v):
    try:
        if v is None:
            return None
        if isinstance(v, (int, float)):
            return float(v)
        v = str(v).replace(",", "").strip()
        if v == "":
            return None
        return float(v)
    except Exception:
        return None


def fetch_sina_30m(code: str, datalen: int = 240):
    """
    新浪 30分钟K线。
    不依赖 akshare，适合 GitHub Actions 轻量运行。
    """
    symbol = market_prefix(code)
    url = (
        "https://quotes.sina.cn/cn/api/openapi.php/"
        "CN_MinlineService.getMinlineData"
    )
    params = {
        "symbol": symbol,
        "scale": "30",
        "ma": "no",
        "datalen": str(datalen),
    }

    last_err = None
    for _ in range(3):
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            text = r.text.strip()

            # 正常是 JSON；如果变成 JSONP，也尽量剥离
            if not text.startswith("{"):
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    text = m.group(0)

            data = json.loads(text)
            rows = data.get("result", {}).get("data", [])

            bars = []
            for x in rows:
                if not isinstance(x, dict):
                    continue

                t = x.get("day") or x.get("time") or x.get("date")
                o = to_float(x.get("open"))
                h = to_float(x.get("high"))
                l = to_float(x.get("low"))
                c = to_float(x.get("close"))
                v = to_float(x.get("volume"))

                if all(vv is not None for vv in [o, h, l, c]):
                    bars.append({
                        "time": t,
                        "open": o,
                        "high": h,
                        "low": l,
                        "close": c,
                        "volume": v,
                    })

            if len(bars) >= 30:
                return bars

            last_err = f"数据不足：{len(bars)}"
        except Exception as e:
            last_err = str(e)
            time.sleep(1)

    raise RuntimeError(f"{code} 获取30分钟K线失败：{last_err}")


def pct(a, b):
    if b in (0, None):
        return None
    return (a - b) / b * 100


def near(a, b, tolerance=0.005):
    if a is None or b is None or b == 0:
        return False
    return abs(a - b) / abs(b) <= tolerance


def analyze_one(code: str):
    bars = fetch_sina_30m(code)
    if len(bars) < 65:
        raise RuntimeError(f"{code} 30分钟K线不足，当前只有 {len(bars)} 根")

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
