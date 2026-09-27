#!/usr/bin/env python3
# 新机冒烟: scan_one 端到端 (beichi ML + 新浪K线)
import sys, os, time, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) or ".")
import full_scan

codes = [
    ("600006", "东风股份", 5.34),
    ("002141", "贤丰控股", 6.72),
]
t0 = time.time()
for code, name, price in codes:
    try:
        r = full_scan.scan_one(code, name, price)
        print(f"{code} {name}: dlp={r.get('dlp'):.3f} ratio={r.get('ratio'):.1f}% "
              f"score={r.get('score')} tier={r.get('tier','?')}")
    except Exception as e:
        import traceback
        print(f"{code} ERROR: {e}")
        traceback.print_exc()
print(f"smoke elapsed {time.time()-t0:.1f}s")
