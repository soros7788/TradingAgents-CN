#!/usr/bin/env python3
"""proc_guard.py — 可靠的进程存活 + 资源水位检查点.

替代 `pgrep -f` 纯模式匹配：基于 /proc/<pid>/stat 判活，可识别
zombie (Z)、不可中断睡眠 (D)、孤儿 (ppid=1 被 init 收养)；
读 /proc/meminfo 做 swap/内存水位告警。

只检查、不 kill。退出码: 0=OK, 1=WARN, 2=CRIT, 3=UNKNOWN。

用法:
    proc_guard.py --pid 1234 [--swap-warn 50 --swap-crit 80 --mem-min-mb 100]
    proc_guard.py --pidfile ~/chan_logs/scan.pid
    proc_guard.py --pgrep-pattern "meet_in_the_middle_scan"   # 无 pidfile 时的回退
"""
from __future__ import annotations
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

OK, WARN, CRIT, UNKNOWN = 0, 1, 2, 3
LVL_NAME = {0: "OK", 1: "WARN", 2: "CRIT", 3: "UNKNOWN"}


def read_stat(pid: int):
    """返回 (state, ppid, comm)，读不到返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError):
        return None
    # comm 可能含空格/括号：取最后一个 ')' 之后
    rest = data[data.rfind(")") + 1:].split()
    comm = data[data.find("(") + 1:data.rfind(")")]
    if len(rest) < 2:
        return None
    return rest[0], int(rest[1]), comm  # state, ppid, comm


def read_meminfo():
    info = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for ln in f:
                k, _, v = ln.partition(":")
                info[k.strip()] = int(v.strip().split()[0])  # kB
    except OSError:
        return None
    return info


def check_pid(pid: int, swap_warn: float, swap_crit: float, mem_min_mb: int):
    reasons = []
    level = OK
    st = read_stat(pid)
    if st is None:
        return {"pid": pid, "alive": False, "level": "UNKNOWN",
                "reasons": ["no /proc/%d (not running or no permission)" % pid]}, UNKNOWN
    state, ppid, comm = st
    entry = {"pid": pid, "alive": True, "state": state, "ppid": ppid, "comm": comm,
             "orphaned": False}
    if state == "Z":
        reasons.append("zombie (Z)")
        level = max(level, CRIT)
    elif state == "D":
        reasons.append("uninterruptible sleep (D) — possible IO stuck")
        level = max(level, WARN)
    elif state in ("T", "t"):
        reasons.append("stopped (T)")
        level = max(level, WARN)
    # 注: ppid==1 在本架构是常态（systemd 服务 / setsid 守护），不告警；
    # 单实例由 ensure_mtim.sh 的 pgrep 保证。Z/D/T 与 swap 水位才是真信号。

    mi = read_meminfo()
    if mi and mi.get("SwapTotal", 0) > 0:
        sw_pct = (mi["SwapTotal"] - mi.get("SwapFree", 0)) * 100.0 / mi["SwapTotal"]
        entry["swap_used_pct"] = round(sw_pct, 1)
        if sw_pct >= swap_crit:
            reasons.append("swap %.1f%% >= crit %.0f%%" % (sw_pct, swap_crit))
            level = max(level, CRIT)
        elif sw_pct >= swap_warn:
            reasons.append("swap %.1f%% >= warn %.0f%%" % (sw_pct, swap_warn))
            level = max(level, WARN)
    if mi and "MemAvailable" in mi:
        avail_mb = mi["MemAvailable"] // 1024
        entry["mem_avail_mb"] = avail_mb
        if avail_mb < mem_min_mb:
            reasons.append("MemAvailable %dMB < %dMB" % (avail_mb, mem_min_mb))
            level = max(level, WARN)

    entry["level"] = LVL_NAME[level]
    entry["reasons"] = reasons
    return entry, level


def resolve_pids(args) -> list[int]:
    if args.pid:
        return [args.pid]
    if args.pidfile:
        try:
            with open(os.path.expanduser(args.pidfile), encoding="utf-8") as f:
                return [int(f.read().strip().split()[0])]
        except (OSError, ValueError):
            return []
    if args.pgrep_pattern:
        try:
            out = subprocess.run(["pgrep", "-f", args.pgrep_pattern],
                                 capture_output=True, text=True, timeout=10)
            return [int(x) for x in out.stdout.split() if x.strip().isdigit()]
        except (OSError, subprocess.TimeoutExpired):
            return []
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description="进程存活 + 资源水位检查点（只读，不 kill）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pid", type=int)
    g.add_argument("--pidfile")
    g.add_argument("--pgrep-pattern")
    ap.add_argument("--swap-warn", type=float, default=50)
    ap.add_argument("--swap-crit", type=float, default=80)
    ap.add_argument("--mem-min-mb", type=int, default=100)
    ap.add_argument("--log", default=None, help="告警日志路径（JSONL 追加）")
    args = ap.parse_args()

    pids = resolve_pids(args)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not pids:
        rec = {"ts": now, "level": "UNKNOWN",
               "reasons": ["no target pid resolved"]}
        print(json.dumps(rec, ensure_ascii=False))
        return UNKNOWN

    worst = OK
    for pid in pids:
        entry, lvl = check_pid(pid, args.swap_warn, args.swap_crit, args.mem_min_mb)
        entry["ts"] = now
        worst = max(worst, lvl)
        print(json.dumps(entry, ensure_ascii=False))
        if args.log and lvl >= WARN:
            with open(os.path.expanduser(args.log), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return worst


if __name__ == "__main__":
    sys.exit(main())
