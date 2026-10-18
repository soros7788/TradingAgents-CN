#!/usr/bin/env python3
"""上证指数三池 PASS 输出（sh000001），供上证日报 [三池·上证] 段使用。

- 内存补丁：在生产 shim 补丁落地前也能跑；落地后幂等无害。
- 只读：不写任何生产文件，不进 sealed 区。
- 口径：顺势池=R0+R1双pass(dual gate)；转折池=dlp>0.618；
  组合池=weekly_pos==above(周线硬门槛) ∩ dlp>0.618(力度硬门槛)，同 combo_pool.py。
"""
import os, sys
sys.path.insert(0, os.path.expanduser("~/TradingAgents-CN"))
sys.path.insert(0, os.path.expanduser("~/TradingAgents-CN/scripts/chanlun-workflow"))

import tradingagents.utils.kline_cache_shim as shim
_orig = shim._normalize_cache_df
def _patched(df):
    df = _orig(df)
    if df is not None and "day" in df.columns:
        df["day"] = df["day"].astype(str)
    return df
shim._normalize_cache_df = _patched

from datetime import datetime


def _fmt(v, nd=2):
    try:
        return ("%%.%df" % nd) % float(v)
    except (TypeError, ValueError):
        return "?"


def main():
    import dual2_scan
    import weekly_nesting as wn

    now = datetime.now(dual2_scan.SH_TZ)
    out = []
    # --- 双引擎 ---
    try:
        r = dual2_scan.run_dual_engine("sh000001", now)
    except Exception as e:
        print("[三池·上证] 计算失败：双引擎异常 %s" % type(e).__name__)
        return 2
    gate = r.get("gate")
    direction = r.get("recursive_direction")
    price = r.get("price")
    rs = r.get("recursive_summary", {}) or {}
    r0_dir = rs.get("r0_dir")
    # --- dlp ---
    try:
        sig = dual2_scan._scan_one_with_timeout("sh000001", "上证指数", price or 0, timeout=120)
        dlp = (sig or {}).get("dlp")
    except Exception:
        dlp = None
    # --- 周线 ---
    try:
        z = wn.analyze("sh000001") or {}
        lv = z.get("levels") or {}
        w = lv.get("周线") or {}
        wpos, wzg, wzd, wlast = w.get("pos"), w.get("zg"), w.get("zd"), w.get("last")
    except Exception:
        wpos, wzg, wzd, wlast = None, None, None, None

    data_date = (str(wlast)[:10] if wlast else now.strftime("%Y-%m-%d"))
    out.append("[三池·上证]（数据截至 %s）" % data_date)

    # 顺势池
    if gate == "PASS":
        out.append("- 顺势池：✅ pass——GATE=PASS，递归方向 %s" % direction)
    else:
        extra = ""
        if r0_dir and r0_dir != (direction or ""):
            extra = "（R0 为 %s，大级别压制）" % r0_dir
        out.append("- 顺势池：❌ 未 pass——GATE=%s，递归方向 %s%s" % (gate, direction, extra))

    # 转折池
    if dlp is not None and dlp > 0.618:
        out.append("- 转折池：✅ pass——dlp=%s（>0.618）" % _fmt(dlp, 3))
    elif dlp is None:
        out.append("- 转折池：⚠️ 数据缺失——dlp 未算出")
    else:
        out.append("- 转折池：❌ 未 pass——dlp=%s（<0.618）" % _fmt(dlp, 3))

    # 组合池
    dlp_ok = dlp is not None and dlp > 0.618
    if wpos == "above" and dlp_ok:
        out.append("- 组合池：✅ pass——周线 pos=above ∩ dlp=%s" % _fmt(dlp, 3))
    elif wpos is None:
        out.append("- 组合池：⚠️ 数据缺失——周线未算出")
    else:
        zs_txt = "中枢 %s–%s，现价 %s 处中枢内" % (_fmt(wzd), _fmt(wzg), _fmt(price)) \
            if (wpos == "inside" and wzg and wzd) else "周线 pos=%s（硬门槛要求 above）" % wpos
        dlp_txt = "且 dlp=%s 未达 0.618" % _fmt(dlp, 3) if not dlp_ok else ""
        out.append("- 组合池：❌ 未 pass——%s%s" % (zs_txt, ("，" + dlp_txt) if dlp_txt else ""))

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
