#!/usr/bin/env python3
"""proc_guard.py v2 — 可靠的进程存活 + 资源水位检查点.

替代 `pgrep -f` 纯模式匹配：基于 /proc/<pid>/stat 判活，可识别
zombie (Z)、不可中断睡眠 (D)、孤儿 (ppid=1 被 init 收养)；
读 /proc/meminfo 做 swap/内存水位告警。

v2 新增:
- --sample-log: 每次运行都写一行样本（不只 WARN/CRIT），形成连续趋势
- --alert-file: CRIT 时写 ALERT 标记文件，恢复 OK 时删除；早检脚本读此文件置顶告警
- 子进程检查：对目标 PID 递归枚举子进程，一并检查 Z/D/T 状态
- 进程 CPU%：从 /proc/<pid>/stat 取 utime+stime，算采样间隔内的 CPU 占用

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
import time
from datetime import datetime

OK, WARN, CRIT, UNKNOWN = 0, 1, 2, 3
LVL_NAME = {0: "OK", 1: "WARN", 2: "CRIT", 3: "UNKNOWN"}


def read_stat(pid: int):
    """返回 (state, ppid, comm, utime, stime)，读不到返回 None。"""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
            data = f.read()
    except (FileNotFoundError, PermissionError):
        return None
    # comm 可能含空格/括号：取最后一个 ')' 之后
    rest = data[data.rfind(")") + 1:].split()
    comm = data[data.find("(") + 1:data.rfind(")")]
    if len(rest) < 15:
        return None
    try:
        return rest[0], int(rest[1]), comm, int(rest[11]), int(rest[12])
    except (ValueError, IndexError):
        return None


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


def build_ppid_map():
    """扫描 /proc，返回 {ppid: [child_pids]}。"""
    ppid_map: dict[int, list[int]] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return ppid_map
    for e in entries:
        if not e.isdigit():
            continue
        pid = int(e)
        st = read_stat(pid)
        if st is None:
            continue
        _, ppid, _, _, _ = st
        ppid_map.setdefault(ppid, []).append(pid)
    return ppid_map


def find_children(pid: int, ppid_map: dict[int, list[int]], depth: int = 0,
                  max_depth: int = 5) -> list[int]:
    """递归找子进程（防循环，限深度）。"""
    if depth >= max_depth:
        return []
    children = []
    for child in ppid_map.get(pid, []):
        if child == pid:
            continue
        children.append(child)
        children.extend(find_children(child, ppid_map, depth + 1, max_depth))
    return children


def check_pid(pid: int, swap_warn: float, swap_crit: float, mem_min_mb: int,
              ppid_map: dict[int, list[int]] | None = None):
    reasons = []
    level = OK
    st = read_stat(pid)
    if st is None:
        return {"pid": pid, "alive": False, "level": "UNKNOWN",
                "reasons": ["no /proc/%d (not running or no permission)" % pid]}, UNKNOWN
    state, ppid, comm, utime, stime = st
    entry = {"pid": pid, "alive": True, "state": state, "ppid": ppid, "comm": comm,
             "orphaned": False, "cpu_ticks": utime + stime}
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

    # 子进程检查
    children_info = []
    if ppid_map is not None:
        for child in find_children(pid, ppid_map):
            cst = read_stat(child)
            if cst is None:
                continue
            cstate, cppid, ccomm, cutime, cstime = cst
            cinfo = {"pid": child, "state": cstate, "comm": ccomm}
            if cstate == "Z":
                cinfo["issue"] = "zombie"
                reasons.append(f"child {child}({ccomm}) zombie")
                level = max(level, CRIT)
            elif cstate == "D":
                cinfo["issue"] = "D-state"
                reasons.append(f"child {child}({ccomm}) uninterruptible sleep")
                level = max(level, WARN)
            elif cstate in ("T", "t"):
                cinfo["issue"] = "stopped"
                reasons.append(f"child {child}({ccomm}) stopped")
                level = max(level, WARN)
            children_info.append(cinfo)
    if children_info:
        entry["children"] = children_info
        entry["n_children"] = len(children_info)

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
            me, parent = os.getpid(), os.getppid()
            return [int(x) for x in out.stdout.split()
                    if x.strip().isdigit() and int(x) not in (me, parent)]
        except (OSError, subprocess.TimeoutExpired):
            return []
    return []


def write_alert(alert_file: str, level: int, entries: list[dict], now: str):
    """CRIT 写 ALERT 文件；OK 时删除。"""
    path = os.path.expanduser(alert_file)
    if level >= CRIT:
        crit_reasons = []
        for e in entries:
            for r in e.get("reasons", []):
                crit_reasons.append(f"pid={e['pid']}({e.get('comm','?')}): {r}")
        payload = {"ts": now, "level": "CRIT", "reasons": crit_reasons}
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, path)
        except OSError:
            pass
    elif level == OK:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    # WARN 不动 ALERT 文件（避免抖动）


def main() -> int:
    ap = argparse.ArgumentParser(description="进程存活 + 资源水位检查点（只读，不 kill）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--pid", type=int)
    g.add_argument("--pidfile")
    g.add_argument("--pgrep-pattern")
    ap.add_argument("--swap-warn", type=float, default=50)
    ap.add_argument("--swap-crit", type=float, default=80)
    ap.add_argument("--mem-min-mb", type=int, default=100)
    ap.add_argument("--log", default=None, help="告警日志路径（WARN+ 才写，JSONL 追加）")
    ap.add_argument("--sample-log", default=None,
                    help="全样本日志路径（每次运行都写一行，JSONL 追加，用于趋势）")
    ap.add_argument("--alert-file", default=None,
                    help="CRIT 时写 ALERT 标记文件，OK 时删除")
    ap.add_argument("--check-children", action="store_true", default=True,
                    help="递归检查子进程（默认开）")
    ap.add_argument("--no-check-children", dest="check_children", action="store_false")
    args = ap.parse_args()

    pids = resolve_pids(args)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if not pids:
        rec = {"ts": now, "level": "UNKNOWN",
               "reasons": ["no target pid resolved"]}
        print(json.dumps(rec, ensure_ascii=False))
        # 2026-09-28 修复: 无 PID 也是故障, 必须写 ALERT, 不能静默返回
        if args.alert_file:
            write_alert(args.alert_file, UNKNOWN,
                        [{"pid": 0, "comm": "?", "reasons": rec["reasons"]}], now)
        return UNKNOWN

    ppid_map = build_ppid_map() if args.check_children else None

    worst = OK
    entries = []
    for pid in pids:
        entry, lvl = check_pid(pid, args.swap_warn, args.swap_crit,
                               args.mem_min_mb, ppid_map)
        entry["ts"] = now
        entries.append(entry)
        worst = max(worst, lvl)
        print(json.dumps(entry, ensure_ascii=False))
        if args.log and lvl >= WARN:
            with open(os.path.expanduser(args.log), "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # 全样本日志：每次运行一行
    if args.sample_log:
        mi = read_meminfo()
        sample = {"ts": now, "level": LVL_NAME[worst], "n_pids": len(pids),
                  "pids": [{"pid": e["pid"], "state": e.get("state"),
                            "level": e["level"],
                            "n_children": e.get("n_children", 0)}
                           for e in entries]}
        if mi:
            if mi.get("SwapTotal", 0) > 0:
                sample["swap_used_pct"] = round(
                    (mi["SwapTotal"] - mi.get("SwapFree", 0)) * 100.0 / mi["SwapTotal"], 1)
            if "MemAvailable" in mi:
                sample["mem_avail_mb"] = mi["MemAvailable"] // 1024
        try:
            with open(os.path.expanduser(args.sample_log), "a", encoding="utf-8") as f:
                f.write(json.dumps(sample, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ALERT 文件
    if args.alert_file:
        write_alert(args.alert_file, worst, entries, now)

    return worst


if __name__ == "__main__":
    sys.exit(main())
