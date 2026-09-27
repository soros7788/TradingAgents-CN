import sys, traceback, time, os; sys.path.insert(0, os.path.expanduser("~/TradingAgents-CN/scripts/chanlun-workflow"))
from datetime import datetime
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")
as_of = datetime.now(SH_TZ)

print("=== 1. import dual2 ===", flush=True)
from dual2_scan import run_dual_engine

for code in ["002141", "300750", "000001", "002594", "300059"]:
    print(f"\n=== RUN {code} ===", flush=True)
    t0 = time.time()
    try:
        r = run_dual_engine(code, as_of)
        dt = time.time() - t0
        gate = r.get("gate", "?")
        dd = r.get("dual_direction", "?")
        print(f"  OK [{dt:.1f}s] gate={gate} dir={dd}", flush=True)
    except Exception as e:
        dt = time.time() - t0
        print(f"  FAIL [{dt:.1f}s]", flush=True)
        traceback.print_exc()

print("\n=== ALL DONE ===", flush=True)
