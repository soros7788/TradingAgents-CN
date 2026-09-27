#!/usr/bin/env python3
"""递归级别联立提取 (R0/R1/R2) — 只读调用 Chan Dual API, 不改 sealed module, 不写 prod.

用法:
  python3 recursive_joint.py <codes.json> <out.json>
  codes.json: [[code, price], ...]
"""
import sys, os, json, time
from datetime import datetime
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for p in (_SCRIPT_DIR, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

from tradingagents.chan.canonical_snapshot_acquisition import acquire_canonical_market_snapshot
from tradingagents.chan.canonical_evidence_orchestration import build_evidence_bundle_from_snapshot
from tradingagents.chan.data_alignment import DataAlignmentResult
from tradingagents.chan.evidence_schema import EvidenceDirection, EvidenceType

SH_TZ = ZoneInfo("Asia/Shanghai")


def unverified_alignment():
    return DataAlignmentResult(
        status="unverified", symbol_match=False, date_match=False,
        source_match=False, adjustment_match=False, timezone_match=False,
        details={"comparison_performed": False, "reason": "single_source"},
    )


def level_direction(evs):
    """同 dual_scan 优先级: TREND > SEGMENT > BUY_POINT > 其他; 取该 type 最后一条 direction."""
    for t in (EvidenceType.TREND, EvidenceType.SEGMENT, EvidenceType.BUY_POINT,
              EvidenceType.BI, EvidenceType.ZHONGSHU, EvidenceType.BREAKOUT):
        matched = [e for e in evs if e.type == t]
        if matched:
            return matched[-1].direction.value
    return "neutral"


def extract(code, as_of):
    snapshot = acquire_canonical_market_snapshot(symbol=code, as_of=as_of)
    bundle = build_evidence_bundle_from_snapshot(
        snapshot=snapshot,
        alignment_result=unverified_alignment(),
        as_of=as_of,
        generated_at=datetime.now(SH_TZ),
    )
    rec = bundle.recursive_evidence or []
    intv = bundle.interval_evidence or []

    # 按 level 分组 recursive_evidence
    by_level = {}
    for e in rec:
        by_level.setdefault(e.level, []).append(e)

    levels = {}
    buy_points = {}
    for lvl, evs in by_level.items():
        levels[lvl] = level_direction(evs)
        # 收集买点 subtype (一买/二买/三买)
        bps = []
        for e in evs:
            if e.type == EvidenceType.BUY_POINT:
                bps.append({"subtype": e.subtype, "direction": e.direction.value,
                            "status": e.status.value, "strength": e.strength})
        if bps:
            buy_points[lvl] = bps

    # 联立判定
    has = {lvl: (levels.get(lvl) == "bullish") for lvl in ("R0", "R1", "R2")}
    two_joint = has["R0"] and has["R1"]
    three_joint = has["R0"] and has["R1"] and has["R2"]

    return {
        "code": code,
        "recursive_levels_found": sorted(by_level.keys()),
        "recursive_level_direction": levels,
        "buy_points": buy_points,
        "has_R0_bull": has["R0"],
        "has_R1_bull": has["R1"],
        "has_R2_bull": has["R2"],
        "bullish_confluence_r0r1": two_joint,
        "bullish_confluence_r0r1r2": three_joint,
        "interval_evidence_count": len(intv),
        "interval_bull_confirmed": sum(1 for e in intv if e.direction == EvidenceDirection.BULLISH and e.status.value == "confirmed"),
        "interval_bear_confirmed": sum(1 for e in intv if e.direction == EvidenceDirection.BEARISH and e.status.value == "confirmed"),
    }


def main():
    codes_path = sys.argv[1]
    out_path = sys.argv[2]
    codes = json.load(open(codes_path))
    as_of = datetime.now(SH_TZ)
    results = []
    for i, (code, price) in enumerate(codes, 1):
        try:
            r = extract(code, as_of)
        except Exception as e:
            r = {"code": code, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        tag = "J2" if r.get("bullish_confluence_r0r1") else ("J3" if r.get("bullish_confluence_r0r1r2") else "-")
        print(f"  [{i}/{len(codes)}] {code} dirs={r.get('recursive_level_direction')} "
              f"buy={list(r.get('buy_points',{}).keys())} {tag}", flush=True)
        time.sleep(0.5)
    json.dump({"as_of": as_of.strftime("%Y-%m-%d %H:%M:%S %Z"), "results": results},
              open(out_path, "w"), ensure_ascii=False, indent=2, default=str)
    print(f"WROTE {out_path} ({len(results)} stocks)")


if __name__ == "__main__":
    main()
