import sys, os, json
try:
    import numpy as np
except Exception:
    np = None
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from full_scan import scan_one

SRC = "/home/gorgesoros39/chan_logs/dualscan_20260907_131841.json"
src = json.load(open(SRC))
dual = src["dual"]

def bc(x): return (x.get("interval_timing") or {}).get("bullish_confirmed", 0)
def kc(x): return (x.get("interval_timing") or {}).get("bearish_confirmed", 0)

# Tier1 强确认候选
t1 = [x["code"] for x in dual if x.get("gate") == "PASS" and bc(x) >= 2 and kc(x) == 0]
print(f"[rescan] Tier1 共 {len(t1)} 只, 开始补算 DL_P(dl_prob)/ratio/price", flush=True)

# numpy 安全序列化
def _conv(o):
    if np is not None:
        if isinstance(o, np.integer): return int(o)
        if isinstance(o, np.floating): return float(o)
        if isinstance(o, np.bool_): return bool(o)
        if isinstance(o, np.ndarray): return o.tolist()
    if isinstance(o, bool): return bool(o)
    if isinstance(o, (int, float, str)) or o is None: return o
    return str(o)

out = {}
logf = open("/home/gorgesoros39/chan_logs/dlp_rescan_run.log", "w")
for i, code in enumerate(t1, 1):
    try:
        r = scan_one(code, "", 0)
    except Exception as e:
        r = None
    if r:
        out[code] = {
            "dlp": r.get("dlp"), "ratio": r.get("ratio"), "price": r.get("price"),
            "confirmed": r.get("confirmed"), "near": r.get("near"),
            "score": r.get("score"), "valid": r.get("valid"),
        }
    else:
        out[code] = {"dlp": None, "ratio": None, "price": None,
                     "confirmed": False, "near": False, "score": 0, "valid": False}
    line = f"[{i}/{len(t1)}] {code} dlp={out[code]['dlp']} ratio={out[code]['ratio']} price={out[code]['price']}"
    print(line, flush=True)
    print(line, file=logf, flush=True)
logf.close()

# 先 dump 字符串（若失败不写文件），再落盘
try:
    txt = json.dumps(out, default=_conv, ensure_ascii=False, indent=2)
    with open("/home/gorgesoros39/chan_logs/dlp_rescan_tier1.json", "w") as f:
        f.write(txt)
except Exception as e:
    print("JSON dump err:", e, flush=True)
got = sum(1 for v in out.values() if v.get("dlp") is not None)
print(f"DONE t1={len(t1)} got_dlp={got}", flush=True)
