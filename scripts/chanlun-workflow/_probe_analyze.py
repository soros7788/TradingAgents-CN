#!/usr/bin/env python3
import traceback
from beichi_analyzer import analyze_beichi
for code in ["002141", "600006", "000001"]:
    print(f"=== {code} ===")
    try:
        r = analyze_beichi(code, level="日线")
        print("  type:", type(r).__name__)
        if isinstance(r, dict):
            print("  keys:", list(r.keys()))
            print("  signals count:", len(r.get("signals", [])))
            print("  C len:", len(r.get("C", [])))
            print("  error?", r.get("error"))
    except Exception as e:
        traceback.print_exc()
