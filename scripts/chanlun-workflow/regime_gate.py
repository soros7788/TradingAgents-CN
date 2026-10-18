#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Regime Gate 共享模块 (2026-10-03).

为三池 (trend / beichi_turn / combo) 提供统一的市场 regime 判定。
基于上证综指 (sh000001) vs 60日均线。

Regime 定义 (2026-10-03 v3):
  池子永远跑，regime 只调阈值/仓位/模式，不做一刀切。
  个股可以有独立于指数的周线趋势。

  - BEAR: threshold×1.5, size×0.3, mode=CAUTIOUS
  - CONSOLIDATION: threshold×1.2, size×0.5, mode=CAUTIOUS (beichi_turn为主)
  - BULL: threshold×1.0, size×1.0, mode=TRADE (trend为主)

治理: 只读 kline_cache，不写任何生产状态。
"""
import os
import csv
import json
import uuid
from datetime import datetime

CACHE = os.path.expanduser("~/TradingAgents-CN/kline_cache")
INDEX_FILE = os.path.join(CACHE, "sh000001_day.csv")
MA_WINDOW = 60
CONFIRM_DAYS = 5
BUFFER_PCT = 3.0


def get_regime():
    """返回 (regime, detail_dict).
    regime: 'BEAR' | 'CONSOLIDATION' | 'BULL' | 'UNKNOWN'
    """
    if not os.path.exists(INDEX_FILE):
        return "UNKNOWN", {"reason": "index file missing"}
    rows = []
    try:
        with open(INDEX_FILE) as f:
            for r in csv.DictReader(f):
                rows.append((r["date"][:10], float(r["close"])))
    except Exception as e:
        return "UNKNOWN", {"reason": "read error: %s" % str(e)[:50]}
    rows.sort()
    if len(rows) < MA_WINDOW + CONFIRM_DAYS:
        return "UNKNOWN", {"reason": "insufficient data"}
    closes = [c for _, c in rows]

    def ma_at(i):
        w = closes[max(0, i - MA_WINDOW + 1):i + 1]
        return sum(w) / len(w)

    last = len(rows) - 1
    last_date, last_close = rows[last]
    ma60 = ma_at(last)
    dev_pct = (last_close / ma60 - 1) * 100 if ma60 else 0

    # 连续在均线上/下天数
    above = 0
    below = 0
    for i in range(last, -1, -1):
        m = ma_at(i)
        if closes[i] > m:
            if below > 0:
                break
            above += 1
        elif closes[i] < m:
            if above > 0:
                break
            below += 1
        else:
            break

    detail = {
        "date": last_date,
        "close": round(last_close, 1),
        "ma60": round(ma60, 1),
        "dev_pct": round(dev_pct, 2),
        "days_above": above,
        "days_below": below,
    }

    if below >= CONFIRM_DAYS:
        return "BEAR", detail
    if above >= CONFIRM_DAYS:
        return "BULL", detail
    return "CONSOLIDATION", detail


def gen_run_id(pool_name):
    """生成唯一 run_id: pool_YYYYMMDD_HHMMSS_6位hex.
    同一天多次运行不覆盖，可追溯，闭环审计用。
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    short = uuid.uuid4().hex[:6]
    return "%s_%s_%s" % (pool_name, ts, short)


def pool_config(pool_name, regime):
    """给定 regime，返回某个池的配置.
    pool_name: 'trend' | 'beichi_turn' | 'combo'
    返回 dict: {mode, threshold_mult, size_mult, engine, primary}
    """
    base = {
        "engine": "v1",  # 信号引擎标注 (v5 未接入)
        "v5_status": "not_integrated",  # phase 2 接入个股 v5 过滤
        "market_regime": regime,
    }
    if regime == "BEAR":
        cfg = {"mode": "CAUTIOUS", "threshold_mult": 1.5, "size_mult": 0.3}
    elif regime == "CONSOLIDATION":
        cfg = {"mode": "CAUTIOUS", "threshold_mult": 1.2, "size_mult": 0.5}
    elif regime == "BULL":
        cfg = {"mode": "TRADE", "threshold_mult": 1.0, "size_mult": 1.0}
    else:  # UNKNOWN
        cfg = {"mode": "CAUTIOUS", "threshold_mult": 1.5, "size_mult": 0.3}
    # 主力池标注
    primary_map = {
        "BEAR": None,  # 熊市无主力，只观察
        "CONSOLIDATION": "beichi_turn",
        "BULL": "trend",
    }
    cfg["primary"] = (pool_name == primary_map.get(regime))
    return {**base, **cfg}


def pool_mode(pool_name, regime):
    """兼容 v2 接口: 返回 TRADE/OBSERVE/BLOCKED.
    v3 不再 BLOCK，BEAR 时 combo 为 OBSERVE，其余为 TRADE (带 CAUTIOUS 模式).
    """
    cfg = pool_config(pool_name, regime)
    if regime == "BEAR" and pool_name == "combo":
        return "OBSERVE"
    return "TRADE"


def pool_allowed(pool_name, regime):
    """兼容老接口: v3 永远允许运行."""
    return True


if __name__ == "__main__":
    regime, detail = get_regime()
    print(json.dumps({"regime": regime, "detail": detail}, ensure_ascii=False, indent=1))
