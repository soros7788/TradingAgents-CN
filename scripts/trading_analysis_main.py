#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TradingAgents-CN Telegram Only
30分钟突破扫描脚本

特点：
1. 不用 Docker
2. 不用本地部署
3. 不用 GitHub Pages
4. 不创建 Issue
5. GitHub Actions 直接跑 Python
6. 结果输出 JSON，由 workflow 推送 Telegram

数据源顺序：
1. Yahoo Finance：适合 GitHub Actions 海外环境，优先
2. 腾讯行情：备用
3. 东方财富：备用
4. 新浪行情：备用

A股代码映射：
000001 -> 000001.SZ
002463 -> 002463.SZ
600000 -> 600000.SS
"""

import argparse
import json
import random
import re
import time
from datetime import datetime, timezone, timedelta

import requests


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
]


def make_headers(extra=None):
    h = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json,text/plain,*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Connection": "close",
    }
    if extra:
        h.update(extra)
    return h


def to_float(v):
    try:
        if v is None:
            return None
        return float(str(v).replace(",", "").strip())
    except Exception:
        return None


def yahoo_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return f"{code}.SS"
    if code.startswith(("0", "2", "3")):
        return f"{code}.SZ"
    return code


def tencent_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "2", "3")):
        return "sz" + code
    if code.startswith(("4", "8")):
        return "bj" + code
    return code


def eastmoney_secid(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return f"1.{code}"
    return f"0.{code}"


def sina_symbol(code: str) -> str:
    code = code.strip()
    if code.startswith("6"):
        return "sh" + code
    return "sz" + code


def fetch_yahoo_30m(code: str):
    """
    Yahoo Finance 30分钟K线。
    适合 GitHub Actions 海外环境。
    """
    symbol = yahoo_symbol(code)

    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    ]

    params = {
        "range": "60d",
        "interval": "30m",
        "includePrePost": "false",
        "events": "history",
    }

    last_err = None

    for url in urls:
        for _ in range(3):
            try:
                r = requests.get(
                    url,
                    params=params,
                    headers=make_headers({"Referer": "https://finance.yahoo.com/"}),
                    timeout=25,
                )
                r.raise_for_status()
                data = r.json()

                chart = data.get("chart", {})
                err = chart.get("error")
                if err:
                    raise RuntimeError(str(err))

                result = chart.get("result")
                if not result:
                    raise RuntimeError("Yahoo result 为空")

                item = result[0]
                timestamps = item.get("timestamp") or []

                indicators = item.get("indicators", {})
                quote_list = indicators.get("quote") or []
                if not quote_list:
                    raise RuntimeError("Yahoo quote 为空")

                quote = quote_list[0]
                opens = quote.get("open") or []
                highs = quote.get("high") or []
                lows = quote.get("low") or []
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []

                bars = []

                for i, ts in enumerate(timestamps):
                    try:
                        o = to_float(opens[i]) if i < len(opens) else None
                        h = to_float(highs[i]) if i < len(highs) else None
                        l = to_float(lows[i]) if i < len(lows) else None
                        c = to_float(closes[i]) if i < len(closes) else None
                        v = to_float(volumes[i]) if i < len(volumes) else None

                        if all(x is not None for x in [o, h, l, c]):
                            bj_time = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=8)
                            bars.append(
                                {
                                    "time": bj_time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "open": o,
                                    "high": h,
                                    "low": l,
                                    "close": c,
                                    "volume": v,
                                    "source": "yahoo",
                                }
                            )
                    except Exception:
                        continue

                if len(bars) >= 65:
                    return bars

                last_err = f"Yahoo返回数据不足：{len(bars)}"

            except Exception as e:
                last_err = str(e)
                time.sleep(1)

    raise RuntimeError(last_err or "Yahoo未知错误")


def fetch_tencent_30m(code: str, lmt: int = 260):
    """
    腾讯 30分钟K线备用。
    """
    symbol = tencent_symbol(code)

    urls = [
        "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
        "https://web3.ifzq.gtimg.cn/appstock/app/kline/mkline",
    ]

    params = {
        "param": f"{symbol},m30,,{lmt}",
    }

    last_err = None

    for url in urls:
        for _ in range(2):
            try:
                r = requests.get(url, params=params, headers=make_headers(), timeout=20)
                r.raise_for_status()
                data = r.json()

                node = data.get("data", {}).get(symbol, {})
                rows = node.get("m30") or node.get("qfqm30") or []

                bars = []

                for row in rows:
                    if not isinstance(row, list) or len(row) < 6:
                        continue

                    t = row[0]
                    o = to_float(row[1])
                    c = to_float(row[2])
                    h = to_float(row[3])
                    l = to_float(row[4])
                    v = to_float(row[5])

                    if all(x is not None for x in [o, h, l, c]):
                        bars.append(
                            {
                                "time": t,
                                "open": o,
                                "high": h,
                                "low": l,
                                "close": c,
                                "volume": v,
                                "source": "tencent",
                            }
                        )

                if len(bars) >= 65:
                    return bars

                last_err = f"腾讯返回数据不足：{len(bars)}"

            except Exception as e:
                last_err = str(e)
                time.sleep(1)

    raise RuntimeError(last_err or "腾讯未知错误")


def fetch_eastmoney_30m(code: str, lmt: int = 260):
    """
    东方财富 30分钟K线备用。
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
            r = requests.get(
                url,
                params=params,
                headers=make_headers({"Referer": "https://quote.eastmoney.com/"}),
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()

            klines = []
            if isinstance(data.get("data"), dict):
                klines = data["data"].get("klines") or []

            bars = []

            for line in klines:
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
                    bars.append(
                        {
                            "time": t,
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": c,
                            "volume": v,
                            "source": "eastmoney",
                        }
                    )

            if len(bars) >= 65:
                return bars

            last_err = f"东方财富返回数据不足：{len(bars)}"

        except Exception as e:
            last_err = str(e)
            time.sleep(1)

    raise RuntimeError(last_err or "东方财富未知错误")


def fetch_sina_30m(code: str, lmt: int = 260):
    """
    新浪 30分钟K线备用。
    """
    symbol = sina_symbol(code)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MinlineService.getMinlineData"

    params = {
        "symbol": symbol,
        "scale": "30",
        "ma": "no",
        "datalen": str(lmt),
    }

    last_err = None

    for _ in range(3):
        try:
            r = requests.get(url, params=params, headers=make_headers(), timeout=20)
            r.raise_for_status()
            text = r.text.strip()

            if not text.startswith("{"):
                m = re.search(r"\{.*\}", text, re.S)
                if m:
                    text = m.group(0)

            data = json.loads(text)
            rows = data.get("result", {}).get("data", []) or []

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
                    bars.append(
                        {
                            "time": t,
                            "open": o,
                            "high": h,
                            "low": l,
                            "close": c,
                            "volume": v,
                            "source": "sina",
                        }
                    )

            if len(bars) >= 65:
                return bars

            last_err = f"新浪返回数据不足：{len(bars)}"

        except Exception as e:
            last_err = str(e)
            time.sleep(1)

    raise RuntimeError(last_err or "新浪未知错误")


def fetch_30m(code: str):
    """
    多数据源容错。
    GitHub Actions 在海外环境，优先 Yahoo。
    """
    errors = []

    for name, fn in [
        ("yahoo", fetch_yahoo_30m),
        ("tencent", fetch_tencent_30m),
        ("eastmoney", fetch_eastmoney_30m),
        ("sina", fetch_sina_30m),
    ]:
        try:
            return fn(code)
        except Exception as e:
            errors.append(f"{name}: {e}")

    raise RuntimeError("全部数据源失败；" + " | ".join(errors))


def pct(a, b):
    if b in (0, None):
        return None
    return (a - b) / b * 100


def near(a, b, tolerance=0.003):
    if a is None or b is None or b == 0:
        return False
    return abs(a - b) / abs(b) <= tolerance


def analyze_one(code: str):
    bars = fetch_30m(code)

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
        "data_source": latest.get("source"),
        "latest_time": latest["time"],
        "last_price": round(last_price, 3),
        "structure_high_60": round(structure_high, 3),
        "breakout_pct": None if breakout_pct is None else round(breakout_pct, 2),
        "high_5": round(high_5, 3),
        "high_20": round(high_20, 3),
        "high_60": round(high_60, 3),
        "low_2": round(low_2, 3),
        "low_5": round(low_5, 3),
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
            errors.append(
                {
                    "code": code,
                    "error": str(e),
                }
            )

    candidates = [
        x
        for x in results
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
