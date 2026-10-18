#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v5-full 中枢检测器 (2026-10-04).

替换 v5-lite，接口保持兼容：v5_state(code) -> (盘整/趋势/无, detail)

架构：
- R0：生产 pipeline（canonical_bars_to_recursive_r0 → analyze_same_rules，5min）
- R1 段 = R0 movements 1:1 直转，无分型笔
- 中枢：生产 _build_zhongshus + 2% 最小尺寸过滤
- 1 个中枢=盘整；≥2 个同级别=趋势；0 个=无（=非趋势）

不修改 tradingagents/chan/*、recursive_core、interval_engine。
"""
import os
import os, sys

sys.path.insert(0, os.path.expanduser('~/TradingAgents-CN'))

CACHE = os.path.expanduser("~/TradingAgents-CN/kline_cache")

# 复用持久化核心模块（2026-10-05：从 /tmp/v5_research 迁入仓库，避免重启丢失）
from v5_full_core import v5_classify


def v5_state(code):
    """对外接口: 返回 (state_cn, detail). state_cn: 盘整/趋势/无."""
    path = os.path.join(CACHE, "%s_5m.csv" % code)
    if not os.path.exists(path):
        return "无", {"reason": "no 5m data"}

    try:
        state, detail = v5_classify(path, code, max_bars=2000)
    except Exception as e:
        return "无", {"reason": "v5-full error: %s" % str(e)[:100]}

    # 接口兼容：趋势-向上/趋势-向下 → 趋势
    if state.startswith("趋势"):
        state = "趋势"
    return state, detail


if __name__ == "__main__":
    import json
    code = sys.argv[1] if len(sys.argv) > 1 else "000001"
    state, detail = v5_state(code)
    print(json.dumps({"code": code, "v5_state": state, "detail": detail}, ensure_ascii=False, indent=1))
