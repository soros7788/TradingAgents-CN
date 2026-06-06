#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
TradingAgents-CN Telegram Only
可配置周期的突破扫描脚本

特点：
1. 不用 Docker
2. 不用本地部署
3. 不用 GitHub Pages
4. 不创建 Issue
5. GitHub Actions 直接跑 Python
6. 结果输出 JSON，由 workflow 推送 Telegram

默认配置偏向提高信号命中率：
- 周期：15分钟
- 信号模式：balanced

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
from zoneinfo import ZoneInfo

import requests


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
]

PERIOD_CONFIG = {
    "15m": {"label": "15分钟", "yahoo_interval": "15m", "tencent_key": "m15", "eastmoney_klt": "15", "sina_scale": "15"},
    "30m": {"label": "30分钟", "yahoo_interval": "30m", "tencent_key": "m30", "eastmoney_klt": "30", "sina_scale": "30"},
    "60m": {"label": "60分钟", "yahoo_interval": "60m", "tencent_key": "m60", "eastmoney_klt": "60", "sina_scale": "60"},
}

SIGNAL_MODES = {
    "strict": {"high_tolerance": 0.003, "low_floor_ratio": 1.000, "breakout_buffer": 0.003},
    "balanced": {"high_tolerance": 0.008, "low_floor_ratio": 0.997, "breakout_buffer": 0.0015},
    "relaxed": {"high_tolerance": 0.015, "low_floor_ratio": 0.995, "breakout_buffer": 0.0000},
}

DAILY_FILTER_MODES = {
    "off": {"label": "关闭"},
    "trend": {"label": "趋势过滤"},
    "momentum": {"label": "动量过滤"},
}

CN_TZ = ZoneInfo("Asia/Shanghai")


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


def _period_cfg(period: str) -> dict:
    period = (period or "15m").strip().lower()
    if period not in PERIOD_CONFIG:
        raise ValueError(f"不支持的周期: {period}，可选值: {', '.join(PERIOD_CONFIG)}")
    return PERIOD_CONFIG[period]


def _source_rows_to_bars(rows, source_name: str):
    bars = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        t = row.get("time")
        o = to_float(row.get("open"))
        h = to_float(row.get("high"))
        l = to_float(row.get("low"))
        c = to_float(row.get("close"))
        v = to_float(row.get("volume"))
        if all(x is not None for x in [o, h, l, c]):
            bars.append(
                {
                    "time": t,
                    "open": o,
                    "high": h,
                    "low": l,
                    "close": c,
                    "volume": v,
                    "source": source_name,
                }
            )
    return bars


def parse_bar_time(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        dt = None
        for fmt in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %H:%M:%S%z",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d %H:%M",
        ):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except Exception:
                continue
        if dt is None:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=CN_TZ)
    return dt.astimezone(CN_TZ)


def market_phase(now=None):
    now = now or datetime.now(CN_TZ)
    weekday = now.weekday()
    if weekday >= 5:
        return "closed"
    minutes = now.hour * 60 + now.minute
    if 9 * 60 + 30 <= minutes < 11 * 60 + 30:
        return "intraday"
    if 13 * 60 <= minutes < 15 * 60:
        return "intraday"
    if 15 * 60 + 10 <= minutes <= 23 * 60 + 59:
        return "close"
    return "closed"


def resolve_scan_phase(value=None, now=None):
    value = (value or "auto").strip().lower()
    if value in ("intraday", "close"):
        return value
    return market_phase(now)


def is_session_valid(phase: str, now=None):
    now = now or datetime.now(CN_TZ)
    weekday = now.weekday()
    if weekday >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    if phase == "intraday":
        return (9 * 60 + 30 <= minutes < 11 * 60 + 30) or (13 * 60 <= minutes < 15 * 60)
    if phase == "close":
        return 15 * 60 + 10 <= minutes <= 23 * 60 + 59
    return False


def calc_bar_delay_minutes(bar_time, now=None):
    dt = parse_bar_time(bar_time)
    if dt is None:
        return None
    now = now or datetime.now(CN_TZ)
    return max(0.0, round((now - dt).total_seconds() / 60.0, 1))


def fetch_yahoo_kline(code: str, period: str):
    cfg = _period_cfg(period)
    symbol = yahoo_symbol(code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    ]
    params = {
        "range": "60d",
        "interval": cfg["yahoo_interval"],
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
                rows = []
                for i, ts in enumerate(timestamps):
                    o = to_float(opens[i]) if i < len(opens) else None
                    h = to_float(highs[i]) if i < len(highs) else None
                    l = to_float(lows[i]) if i < len(lows) else None
                    c = to_float(closes[i]) if i < len(closes) else None
                    v = to_float(volumes[i]) if i < len(volumes) else None
                    if all(x is not None for x in [o, h, l, c]):
                        bj_time = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=8)
                        rows.append({"time": bj_time.strftime("%Y-%m-%d %H:%M:%S"), "open": o, "high": h, "low": l, "close": c, "volume": v})
                bars = _source_rows_to_bars(rows, "yahoo")
                if len(bars) >= 65:
                    return bars
                last_err = f"Yahoo返回数据不足：{len(bars)}"
            except Exception as e:
                last_err = str(e)
                time.sleep(1)
    raise RuntimeError(last_err or "Yahoo未知错误")


def fetch_yahoo_daily(code: str):
    symbol = yahoo_symbol(code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}",
        f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}",
    ]
    params = {
        "range": "1y",
        "interval": "1d",
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
                    raise RuntimeError("Yahoo日线result为空")
                item = result[0]
                timestamps = item.get("timestamp") or []
                indicators = item.get("indicators", {})
                quote_list = indicators.get("quote") or []
                if not quote_list:
                    raise RuntimeError("Yahoo日线quote为空")
                quote = quote_list[0]
                opens = quote.get("open") or []
                highs = quote.get("high") or []
                lows = quote.get("low") or []
                closes = quote.get("close") or []
                volumes = quote.get("volume") or []
                rows = []
                for i, ts in enumerate(timestamps):
                    o = to_float(opens[i]) if i < len(opens) else None
                    h = to_float(highs[i]) if i < len(highs) else None
                    l = to_float(lows[i]) if i < len(lows) else None
                    c = to_float(closes[i]) if i < len(closes) else None
                    v = to_float(volumes[i]) if i < len(volumes) else None
                    if all(x is not None for x in [o, h, l, c]):
                        bj_time = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=8)
                        rows.append({"time": bj_time.strftime("%Y-%m-%d"), "open": o, "high": h, "low": l, "close": c, "volume": v})
                bars = _source_rows_to_bars(rows, "yahoo_daily")
                if len(bars) >= 60:
                    return bars
                last_err = f"Yahoo日线返回数据不足：{len(bars)}"
            except Exception as e:
                last_err = str(e)
                time.sleep(1)
    raise RuntimeError(last_err or "Yahoo日线未知错误")


def fetch_tencent_kline(code: str, period: str, lmt: int = 260):
    cfg = _period_cfg(period)
    symbol = tencent_symbol(code)
    urls = [
        "https://web.ifzq.gtimg.cn/appstock/app/kline/mkline",
        "https://web3.ifzq.gtimg.cn/appstock/app/kline/mkline",
    ]
    params = {"param": f"{symbol},{cfg['tencent_key']},,{lmt}"}
    last_err = None
    for url in urls:
        for _ in range(2):
            try:
                r = requests.get(url, params=params, headers=make_headers(), timeout=20)
                r.raise_for_status()
                data = r.json()
                node = data.get("data", {}).get(symbol, {})
                rows = node.get(cfg["tencent_key"]) or []
                bars = _source_rows_to_bars(
                    [
                        {"time": row[0], "open": row[1], "close": row[2], "high": row[3], "low": row[4], "volume": row[5]}
                        for row in rows
                        if isinstance(row, list) and len(row) >= 6
                    ],
                    "tencent",
                )
                if len(bars) >= 65:
                    return bars
                last_err = f"腾讯返回数据不足：{len(bars)}"
            except Exception as e:
                last_err = str(e)
                time.sleep(1)
    raise RuntimeError(last_err or "腾讯未知错误")


def fetch_eastmoney_kline(code: str, period: str, lmt: int = 260):
    cfg = _period_cfg(period)
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": eastmoney_secid(code),
        "klt": cfg["eastmoney_klt"],
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
            r = requests.get(url, params=params, headers=make_headers({"Referer": "https://quote.eastmoney.com/"}), timeout=20)
            r.raise_for_status()
            data = r.json()
            klines = []
            if isinstance(data.get("data"), dict):
                klines = data["data"].get("klines") or []
            rows = []
            for line in klines:
                parts = str(line).split(",")
                if len(parts) < 6:
                    continue
                rows.append({"time": parts[0], "open": parts[1], "close": parts[2], "high": parts[3], "low": parts[4], "volume": parts[5]})
            bars = _source_rows_to_bars(rows, "eastmoney")
            if len(bars) >= 65:
                return bars
            last_err = f"东方财富返回数据不足：{len(bars)}"
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(last_err or "东方财富未知错误")


def fetch_sina_kline(code: str, period: str, lmt: int = 260):
    cfg = _period_cfg(period)
    symbol = sina_symbol(code)
    url = "https://quotes.sina.cn/cn/api/openapi.php/CN_MinlineService.getMinlineData"
    params = {"symbol": symbol, "scale": cfg["sina_scale"], "ma": "no", "datalen": str(lmt)}
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
            bars = _source_rows_to_bars(
                [
                    {"time": x.get("day") or x.get("time") or x.get("date"), "open": x.get("open"), "close": x.get("close"), "high": x.get("high"), "low": x.get("low"), "volume": x.get("volume")}
                    for x in rows
                    if isinstance(x, dict)
                ],
                "sina",
            )
            if len(bars) >= 65:
                return bars
            last_err = f"新浪返回数据不足：{len(bars)}"
        except Exception as e:
            last_err = str(e)
            time.sleep(1)
    raise RuntimeError(last_err or "新浪未知错误")


def fetch_kline(code: str, period: str):
    errors = []
    for name, fn in [
        ("yahoo", fetch_yahoo_kline),
        ("tencent", fetch_tencent_kline),
        ("eastmoney", fetch_eastmoney_kline),
        ("sina", fetch_sina_kline),
    ]:
        try:
            return fn(code, period)
        except Exception as e:
            errors.append(f"{name}: {e}")
    raise RuntimeError("全部数据源失败；" + " | ".join(errors))


def compute_daily_filter(code: str, mode: str):
    mode = (mode or "off").strip().lower()
    if mode not in DAILY_FILTER_MODES:
        raise ValueError(f"不支持的日线过滤模式: {mode}，可选值: {', '.join(DAILY_FILTER_MODES)}")
    if mode == "off":
        return {
            "mode": mode,
            "mode_label": DAILY_FILTER_MODES[mode]["label"],
            "status": "off",
            "passed": True,
            "reason": "日线过滤关闭",
        }

    try:
        bars = fetch_yahoo_daily(code)
    except Exception as e:
        return {
            "mode": mode,
            "mode_label": DAILY_FILTER_MODES[mode]["label"],
            "status": "unknown",
            "passed": True,
            "reason": f"日线数据获取失败: {e}",
        }

    closes = [b["close"] for b in bars if b.get("close") is not None]
    highs = [b["high"] for b in bars if b.get("high") is not None]
    if len(closes) < 60 or len(highs) < 20:
        return {
            "mode": mode,
            "mode_label": DAILY_FILTER_MODES[mode]["label"],
            "status": "unknown",
            "passed": True,
            "reason": f"日线样本不足: {len(closes)}",
        }

    last_close = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    prev_ma20 = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else ma20
    prev_ma60 = sum(closes[-61:-1]) / 60 if len(closes) >= 61 else ma60
    high20 = max(highs[-20:])

    if mode == "trend":
        passed = last_close > ma20 and ma20 >= ma60 and ma20 >= prev_ma20 and ma60 >= prev_ma60 * 0.995
        reason = f"收盘 {last_close:.2f}，MA20 {ma20:.2f}，MA60 {ma60:.2f}"
    else:
        passed = last_close > ma20 and last_close >= high20 * 0.97 and ma20 >= ma60 * 0.995
        reason = f"收盘 {last_close:.2f}，MA20 {ma20:.2f}，20日高点 {high20:.2f}"

    return {
        "mode": mode,
        "mode_label": DAILY_FILTER_MODES[mode]["label"],
        "status": "pass" if passed else "fail",
        "passed": passed,
        "reason": reason,
        "last_close": round(last_close, 3),
        "ma5": round(ma5, 3),
        "ma20": round(ma20, 3),
        "ma60": round(ma60, 3),
        "high20": round(high20, 3),
    }


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


def analyze_one(
    code: str,
    period: str,
    signal_mode: str,
    scan_phase: str,
    max_data_lag_minutes: int,
    daily_filter_mode: str,
):
    period_cfg = _period_cfg(period)
    mode_cfg = SIGNAL_MODES.get(signal_mode, SIGNAL_MODES["balanced"])
    resolved_phase = resolve_scan_phase(scan_phase)
    session_ok = is_session_valid(resolved_phase)
    daily_filter = compute_daily_filter(code, daily_filter_mode)

    if daily_filter["status"] == "fail":
        return {
            "code": code,
            "data_source": "daily_filter",
            "latest_time": "-",
            "data_delay_minutes": None,
            "data_stale": False,
            "scan_phase": resolved_phase,
            "session_ok": session_ok,
            "daily_filter": daily_filter,
            "last_price": None,
            "structure_high_60": None,
            "breakout_pct": None,
            "high_5": None,
            "high_20": None,
            "high_60": None,
            "low_2": None,
            "low_5": None,
            "conditions": {
                "A_high_resonance_5_20_60": False,
                "B_low_not_breaking_2_vs_5": False,
                "D_price_above_structure_high": False,
            },
            "score": 0,
            "signal": "NONE",
            "decision": f"日线过滤未通过：{daily_filter['reason']}",
        }

    bars = fetch_kline(code, period)

    if len(bars) < 65:
        raise RuntimeError(f"{code} {period_cfg['label']}K线不足，当前只有 {len(bars)} 根")

    latest = bars[-1]
    hist = bars[:-1]
    lag_minutes = calc_bar_delay_minutes(latest.get("time"))
    data_stale = lag_minutes is None or lag_minutes > max_data_lag_minutes

    last_price = latest["close"]

    high_5 = max(x["high"] for x in hist[-5:])
    high_20 = max(x["high"] for x in hist[-20:])
    high_60 = max(x["high"] for x in hist[-60:])

    low_2 = min(x["low"] for x in hist[-2:])
    low_5 = min(x["low"] for x in hist[-5:])

    structure_high = high_60
    breakout_pct = pct(last_price, structure_high)

    cond_a = near(high_5, high_20, mode_cfg["high_tolerance"]) and near(high_20, high_60, mode_cfg["high_tolerance"])
    cond_b = low_2 >= (low_5 * mode_cfg["low_floor_ratio"])
    cond_d = last_price > structure_high * (1 + mode_cfg["breakout_buffer"])

    score = 0
    if cond_a:
        score += 30
    if cond_b:
        score += 20
    if cond_d:
        score += 50

    if not session_ok:
        signal = "NONE"
        decision = f"当前不在{resolved_phase}扫描时段"
    elif data_stale:
        signal = "NONE"
        decision = f"数据延迟过高（{lag_minutes} 分钟），跳过信号判断"
    elif cond_d and cond_a and cond_b:
        signal = "BREAKOUT"
        decision = f"触发{period_cfg['label']}突破，进入重点观察"
    elif cond_a and cond_b:
        signal = "WATCH"
        decision = f"{period_cfg['label']}结构压缩，等待突破"
    elif cond_d:
        signal = "WEAK_BREAKOUT"
        decision = f"价格突破，但{period_cfg['label']}结构条件不完整"
    else:
        signal = "NONE"
        decision = "暂未触发"

    return {
        "code": code,
        "data_source": latest.get("source"),
        "latest_time": latest["time"],
        "data_delay_minutes": lag_minutes,
        "data_stale": data_stale,
        "scan_phase": resolved_phase,
        "session_ok": session_ok,
        "daily_filter": daily_filter,
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
    parser.add_argument("--period", default="15m", choices=sorted(PERIOD_CONFIG.keys()))
    parser.add_argument("--signal-mode", default="balanced", choices=sorted(SIGNAL_MODES.keys()))
    parser.add_argument("--daily-filter-mode", default="trend", choices=sorted(DAILY_FILTER_MODES.keys()))
    parser.add_argument("--scan-phase", default="auto", choices=["auto", "intraday", "close"])
    parser.add_argument("--max-data-lag-minutes", type=int, default=15)
    parser.add_argument("--output", default="json")
    args = parser.parse_args()

    stocks = [x.strip() for x in args.stocks.split(",") if x.strip()]

    bj = datetime.now(CN_TZ)

    results = []
    errors = []

    for code in stocks:
        try:
            results.append(
                analyze_one(
                    code,
                    args.period,
                    args.signal_mode,
                    args.scan_phase,
                    args.max_data_lag_minutes,
                    args.daily_filter_mode,
                )
            )
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
        "title": f"TradingAgents-CN {PERIOD_CONFIG[args.period]['label']}突破扫描",
        "beijing_time": bj.strftime("%Y-%m-%d %H:%M:%S"),
        "analysis_type": args.analysis_type,
        "period": args.period,
        "signal_mode": args.signal_mode,
        "daily_filter_mode": args.daily_filter_mode,
        "scan_phase": resolve_scan_phase(args.scan_phase),
        "max_data_lag_minutes": args.max_data_lag_minutes,
        "stocks": stocks,
        "summary": {
            "total": len(stocks),
            "ok": len(results),
            "errors": len(errors),
            "candidates": len(candidates),
            "daily_pass": sum(1 for x in results if x.get("daily_filter", {}).get("status") == "pass"),
            "daily_fail": sum(1 for x in results if x.get("daily_filter", {}).get("status") == "fail"),
            "daily_unknown": sum(1 for x in results if x.get("daily_filter", {}).get("status") == "unknown"),
        },
        "candidates": candidates,
        "all_results": results,
        "errors": errors,
        "rule": {
            "A": f"近5/20/60根{PERIOD_CONFIG[args.period]['label']}K线高点共振",
            "B": f"近2根{PERIOD_CONFIG[args.period]['label']}K线低点不低于近5根低点（按模式可放宽）",
            "D": f"当前价突破近60根结构高点（按模式可放宽）",
        },
        "market_phase": market_phase(bj),
        "scan_enabled": is_session_valid(resolve_scan_phase(args.scan_phase), bj),
        "daily_filter_label": DAILY_FILTER_MODES[args.daily_filter_mode]["label"],
    }

    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
