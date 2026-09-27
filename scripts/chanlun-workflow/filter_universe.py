#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universe 板块过滤器 —— 永久规则: 只要主板 + 中小板

保留: 600 601 603 605 (沪主板) | 000 001 002 003 (深主板 + 中小板)
排除: 300 301 (创业板) | 688 689 (科创板) | 8xx 43x 920 82x (北交所) | 2xx (B股)

用法:
    python3 filter_universe.py <输入> <输出> [--check]
"""
import sys
import os
from collections import Counter

# ---- 永久规则 (用户 2026-09-14 确立) ----
ALLOW_PREFIX = ("600", "601", "603", "605", "000", "001", "002", "003")
DENY_PREFIX = ("300", "301", "688", "689", "820", "830", "870", "920", "430", "200")
CORE = ("600006", "002141", "603256")

NAMES = {
    "600": "沪主板", "601": "沪主板", "603": "沪主板", "605": "沪主板",
    "000": "深主板", "001": "深主板", "002": "中小板", "003": "中小板",
    "300": "创业板", "301": "创业板", "688": "科创板", "689": "科创板",
}


def keep(code: str) -> bool:
    code = code.strip()
    if not code.isdigit() or len(code) != 6:
        return False
    if any(code.startswith(p) for p in DENY_PREFIX):
        return False
    return code.startswith(ALLOW_PREFIX)


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]

    codes = [ln.strip() for ln in open(src, encoding="utf-8") if ln.strip()]
    before = len(codes)
    sel = sorted({c for c in codes if keep(c)})
    dropped = before - len(sel)

    print(f"[in ] {src}: {before} 只")
    print(f"[out] {dst}: {len(sel)} 只 (剔除 {dropped})")

    cnt = Counter(c[:3] for c in sel)
    print("[分布] " + ", ".join(
        f"{k}({NAMES.get(k, '?')}):{v}" for k, v in sorted(cnt.items())))

    # 黑名单必须为 0
    bad = {p: sum(1 for c in sel if c.startswith(p)) for p in DENY_PREFIX}
    bad = {k: v for k, v in bad.items() if v}
    if bad:
        print(f"[FAIL] 仍有黑名单段: {bad}")
        sys.exit(2)
    print("[check] 创业板/科创板/北交所 = 0 ✅")

    # 核心股必须在池
    s = set(sel)
    miss = [c for c in CORE if c not in s]
    if miss:
        print(f"[FAIL] 核心股缺失: {miss}")
        sys.exit(3)
    print(f"[check] 核心股 {', '.join(CORE)} 全部在池 ✅")

    os.makedirs(os.path.dirname(os.path.abspath(dst)) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        f.write("\n".join(sel) + "\n")
    print(f"[write] {dst}")


if __name__ == "__main__":
    main()
