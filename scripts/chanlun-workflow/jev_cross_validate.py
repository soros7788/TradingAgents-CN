
#!/usr/bin/env python3
"""JEV × 递归A引擎 交叉验证（旁路, 不改主流程）。

对指定/账本最新标的:
  1) 现有引擎 run_dual_engine() → recursive_direction (A引擎方向)
  2) 读 kline_cache_local 缓存 → 价格/量特征 (不泄露A引擎结论)
  3) JEV 独立判 direction (choice) + 清晰度 (score)
  4) 对比 A vs JEV 方向一致率, 落盘 chan_logs/jev_cross_YYYYmmdd_HHMMSS.json
用法: python3 jev_cross_validate.py [--codes 000001,600006] [--limit 12]
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for p in (_SCRIPT_DIR, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from tradingagents.utils.kline_cache_shim import install_kline_cache_shim
install_kline_cache_shim()

from full_scan import calc_funding  # noqa: E402
from dual2_scan import run_dual_engine  # noqa: E402
from jev_client import jev_judge, JevError  # noqa: E402

CACHE = os.path.expanduser("~/kline_cache_local")
LOGD = os.path.expanduser("~/chan_logs")
SH_TZ = ZoneInfo("Asia/Shanghai")


def load_rows(code, tf):
    p = os.path.join(CACHE, f"{code}_{tf}.csv")
    if not os.path.exists(p):
        return []
    with open(p) as f:
        return list(csv.DictReader(f))


def tail_stat(rows, n):
    """从尾部收集最近 n 根有 close 的K线并统计."""
    tail = []
    for r in reversed(rows):
        if r.get("close"):
            tail.append(r)
        if len(tail) >= n:
            break
    tail = tail[::-1]
    if not tail:
        return None
    closes = [float(r["close"]) for r in tail]
    vols = [float(r.get("volume") or 0) for r in tail]
    highs = [float(r["high"]) for r in tail if r.get("high")]
    lows = [float(r["low"]) for r in tail if r.get("low")]
    chg = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0
    avg_vol = sum(vols[:-1]) / max(1, len(vols) - 1)
    return {
        "last": round(closes[-1], 3),
        "n": len(tail),
        "chg_pct": round(chg, 2),
        "ma": round(sum(closes) / len(closes), 3) if closes else None,
        "high": round(max(highs), 3) if highs else None,
        "low": round(min(lows), 3) if lows else None,
        "vol_ratio": round(vols[-1] / avg_vol, 2) if avg_vol else None,
    }


def build_state(code):
    day = load_rows(code, "day")
    m5 = load_rows(code, "5m")
    d5 = tail_stat(day, 5)
    d20 = tail_stat(day, 20)
    m5_24 = tail_stat(m5, 24)
    if not d5 or not d20:
        return None
    pos_vs_ma20 = round((d5["last"] - d20["ma"]) / d20["ma"] * 100, 2) if d20["ma"] else None
    return {
        "code": code,
        "as_of": (day[-1].get("day") or day[-1].get("date")) if day else None,
        "price": d5["last"],
        "day_5d": d5,
        "day_20d": d20,
        "close_vs_ma20_pct": pos_vs_ma20,
        "m5_24bars": m5_24,
    }


QUESTIONS = {
    "direction": {
        "type": "choice",
        "instructions": "仅依据给出的价格、均线、成交量特征, 独立判断该标的中短期方向",
        "criteria": {
            "bullish": "趋势向上, 多头占优",
            "bearish": "趋势向下, 空头占优",
            "neutral": "方向不明或震荡",
        },
    },
    "clarity": {
        "type": "score",
        "instructions": "当前技术信号清晰程度(信号越清晰越接近极值)",
        "criteria": ["完全模糊", "较弱", "中等", "较清晰", "非常清晰"],
    },
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes", default=None, help="逗号分隔标的列表")
    ap.add_argument("--limit", type=int, default=12, help="从账本尾部取最新N条")
    args = ap.parse_args()

    codes = []
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
    else:
        led = os.path.join(LOGD, "scan_ledger.jsonl")
        seen, out = set(), []
        if os.path.exists(led):
            for line in open(led):
                try:
                    d = json.loads(line)
                    c = d.get("code")
                    if c and c not in seen:
                        seen.add(c)
                        out.append(c)
                except Exception:
                    pass
        codes = out[-args.limit:]
    print(f"[cross] 标的数: {len(codes)} -> {codes}")

    as_of = datetime.now(SH_TZ)
    rows, agree = [], 0
    for code in codes:
        row = {"code": code, "ts": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S")}
        try:
            res = run_dual_engine(code, as_of)
            a_dir = res.get("recursive_direction", "NEUTRAL")
            gate = res.get("gate", "?")
            rec = res.get("recursive_summary", {})
            row.update({
                "a_dir": a_dir, "gate": gate,
                "r2": rec.get("r2_dir"), "r1": rec.get("r1_dir"), "r0": rec.get("r0_dir"),
                "three_way": rec.get("three_way_unison"),
                "t_score": rec.get("trend_score"), "s_score": rec.get("segment_score"),
                "b_bull": res.get("interval_timing", {}).get("bullish_confirmed"),
                "b_bear": res.get("interval_timing", {}).get("bearish_confirmed"),
            })
        except Exception as e:  # noqa: BLE001
            row.update({"a_dir": "ERROR", "gate": "ERROR", "a_err": f"{type(e).__name__}: {e}"})
            rows.append(row)
            continue

        state = build_state(code)
        if not state:
            row.update({"j_dir": "SKIP", "j_err": "no cache"})
            rows.append(row)
            continue
        try:
            d = jev_judge(state, QUESTIONS, timeout=20)
            ans = d.get("answers", {})
            j_dir = ans.get("direction", {}).get("choice", "?")
            j_conf = ans.get("direction", {}).get("confidence")
            clarity = ans.get("clarity", {}).get("score")
            row.update({
                "j_dir": j_dir, "j_conf": round(j_conf, 3) if j_conf is not None else None,
                "j_clarity": round(clarity, 2) if clarity is not None else None,
                "latency_ms": round(d.get("_latency_ms", 0)),
            })
        except JevError as e:
            row.update({"j_dir": "ERR", "j_err": str(e)[:100]})
        except Exception as e:  # noqa: BLE001
            row.update({"j_dir": "ERR", "j_err": f"{type(e).__name__}: {e}"})

        # 一致性: A方向 与 JEV 方向 (direction.value 为小写)
        a, j = (row.get("a_dir") or "?").upper(), (row.get("j_dir") or "?").upper()
        if a in ("BULLISH", "BEARISH") and j in ("BULLISH", "BEARISH"):
            if a == j:
                row["agree"] = True
                agree += 1
            else:
                row["agree"] = False
        else:
            row["agree"] = None
        rows.append(row)
        print(f"  [{len(rows)}/{len(codes)}] {code} A={a} JEV={j} "
              f"agree={row.get('agree')} conf={row.get('j_conf')}", flush=True)

    # 汇总
    counted = [r for r in rows if r.get("agree") is not None]
    n_agree = sum(1 for r in counted if r["agree"])
    report = {
        "ts": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "n_total": len(rows),
        "n_directional": len(counted),
        "n_agree": n_agree,
        "agree_rate": round(n_agree / len(counted), 3) if counted else None,
        "rows": rows,
    }
    os.makedirs(LOGD, exist_ok=True)
    jp = os.path.join(LOGD, "jev_cross_" + datetime.now(SH_TZ).strftime("%Y%m%d_%H%M%S") + ".json")
    with open(jp, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n[cross] 方向一致率: {n_agree}/{len(counted)} = {report['agree_rate']}")
    print(f"[cross] 落盘: {jp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
