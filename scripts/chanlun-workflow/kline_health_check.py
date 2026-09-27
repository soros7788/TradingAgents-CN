#!/usr/bin/env python3
"""kline_health_check — 并行全量健康检查 + 去重修复"""
import os, sys, csv, json, time, argparse, hashlib
from pathlib import Path
from collections import Counter, defaultdict
from multiprocessing import Pool, cpu_count

VALID_TFS = ("day", "1min", "5min", "15min", "30min", "60min", "1m", "5m", "15m", "30m", "60m")

def process_file(args):
    path_str, fix = args
    path = Path(path_str)
    stem = path.stem
    parts = stem.rsplit("_", 1)
    if len(parts) != 2 or parts[1] not in VALID_TFS:
        return path_str, {"status": "WRONG_NAME"}
    tf = parts[1]

    try:
        with open(path, newline='') as f:
            reader = csv.reader(f)
            hdr = next(reader, None)
            rows = [r for r in reader if len(r) >= 6]
    except Exception as e:
        return path_str, {"status": "LOAD_ERR", "err": str(e)}

    if not rows:
        return path_str, {"status": "EMPTY"}

    # 重复: 按第一列 (day=日期, min=时间戳)
    if tf == "day":
        keys = [r[0] for r in rows]
    else:
        keys = [r[0] + r[1][:5] for r in rows]

    dup_count = sum(c - 1 for c in Counter(keys).values() if c > 1)
    unordered = False
    dates = [r[0] for r in rows]
    if dates != sorted(dates):
        unordered = True

    issue = {"rows": len(rows), "dup": dup_count, "unordered": unordered}

    if dup_count == 0 and not unordered and (len(rows) >= 10 or tf != "day"):
        return path_str, {"status": "OK", **issue}

    # 修复
    if fix and dup_count > 0:
        seen = set(); dedup = []
        for r in rows:
            k = keys[len(dedup)] if len(dedup) < len(keys) else None
            real_k = (r[0] if tf == "day" else r[0] + r[1][:5])
            if real_k not in seen:
                seen.add(real_k); dedup.append(r)
        dedup.sort(key=lambda r: r[0])
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            if hdr: w.writerow(hdr)
            w.writerows(dedup)
        issue["fixed_dup"] = len(rows) - len(dedup)

    if fix and unordered:
        rows.sort(key=lambda r: r[0])
        with open(path, 'w', newline='') as f:
            w = csv.writer(f)
            if hdr: w.writerow(hdr)
            w.writerows(rows)
        issue["fixed_order"] = True

    if dup_count > 0:
        issue["status"] = "HAS_DUP"
    elif unordered:
        issue["status"] = "UNORDERED"
    elif len(rows) < 10:
        issue["status"] = "TOO_SHORT"
    else:
        issue["status"] = "OK"

    return path_str, issue

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=os.environ.get("KLINE_CACHE_DIR", "~/kline_cache_local"))
    parser.add_argument("--fix", action="store_true")
    parser.add_argument("--report", default="/tmp/kline_health.json")
    parser.add_argument("--tf", default=None)
    args = parser.parse_args()

    cache_dir = Path(os.path.expanduser(args.dir))
    files = sorted(cache_dir.glob("*.csv"))
    if args.tf:
        files = [f for f in files if f.stem.endswith(f"_{args.tf}")]

    print(f"  待查 {len(files)} 文件, 进程={cpu_count()} fix={args.fix}", file=sys.stderr)
    t0 = time.time()
    with Pool(cpu_count()) as pool:
        results = pool.map(process_file, [(str(f), args.fix) for f in files])

    # 汇总
    summary = Counter(); issues = {}; fixed = defaultdict(int)
    tf_count = defaultdict(int)
    for path_str, info in results:
        status = info.get("status", "OK")
        summary[status] += 1
        tf = Path(path_str).stem.rsplit("_", 1)[-1]
        tf_count[tf] += 1
        if status != "OK":
            issues[Path(path_str).name] = info
        if info.get("fixed_dup"):
            fixed["dup_total"] += info["fixed_dup"]
        if info.get("fixed_order"):
            fixed["order_total"] += 1

    report = {
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_s": round(time.time() - t0, 1),
        "dir": str(cache_dir),
        "total_files": len(files),
        "tf_count": dict(tf_count),
        "summary": dict(summary),
        "issues_top": dict(list(issues.items())[:50]),
        "issues_count": len(issues),
        "fixed": dict(fixed),
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"  完成 {report['elapsed_s']}s → {args.report}", file=sys.stderr)
    print(json.dumps(report, ensure_ascii=False))

if __name__ == "__main__":
    main()
