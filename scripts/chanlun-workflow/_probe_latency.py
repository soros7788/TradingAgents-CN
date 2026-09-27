#!/usr/bin/env python3
# 测单只股票 beichi 分析耗时（含新浪 K 线取数），量化跨境延迟
import time, traceback
from beichi_analyzer import analyze_beichi
for code in ["600519", "000001", "002594"]:
    t0 = time.time()
    try:
        r = analyze_beichi(code, level="日线")
        dt = time.time() - t0
        if isinstance(r, dict):
            print(f"{code}: OK {dt:.1f}s signals={len(r.get('signals', []))}")
        else:
            print(f"{code}: ret={type(r).__name__} {dt:.1f}s")
    except Exception as e:
        dt = time.time() - t0
        print(f"{code}: ERR {dt:.1f}s {e}")
