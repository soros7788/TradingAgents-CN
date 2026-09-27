#!/usr/bin/env python3
"""meet_in_the_middle_scan.py — 双机对撞扫描 (汇合即停, 批间检测 v4)

设计 (用户定义, 2026-09-13 恢复):
  两端各扫**全量**清单, 方向相反:
      asc  (VM-A): 000001 → 000002 → ... (正序, 从头部出发)
      desc (VM-B): 605599 → 605598 → ... (倒序, 从尾部出发)
  两端相向而行, 速度可以不同, 只要都在推进, 必然在中间相遇。
  相遇时全清单已覆盖, 总扫描量 ≈ N + 重叠。

终止判据 (关键):
  asc_scanned + desc_scanned >= N
  用"数量和"而非"同一 code", 因为共享盘同步有延迟 (数十秒),
  两端可能互相冲过对方; 而"数量和 >= N"与延迟无关, 并集必然覆盖全量。
  代价: 最多多扫一个 batch 的重叠 (相对 2222 只可忽略)。
  好处: 抗单点 —— 一端慢/死, 另一端多扫, 总和照样能达标。

批间检测 (v4, 2026-09-13):
  单只 subprocess 冷启动 + 每只重新解析 K 线, 实测 49-71s/只; 而
  `--codes a,b,c` 逗号列表走 Stage2 批量循环 (一次加载, ~15-20s/批),
  速度回到快路径。v4 改为**分批调用**: 每批 BATCH_SIZE 只, 批间做汇合检测。
  相遇粒度降到"批" (最多多扫 BATCH_SIZE 只), 但速度提升 4-5 倍。

跨机同步 (共享账本文件轮询, rclone):
  批间把本地已扫 code 追加到本地账本, rclone copy 到
  <share_remote>/<side>.jsonl; 然后 rclone copy 拉对方那份, 统计其行数。
  同步失败不致命: 检测不到对方 → 退化为单端扫全量, 仍能完成 (只是慢)。

VM-A 环境 (实测):
  - rclone remote: gdrive:  (VM-B 为 chanlun-gdrive:)
  - 通过 --share-remote / 环境变量 MITM_SHARE_REMOTE 指定
"""
from __future__ import annotations
import argparse, json, os, re, subprocess, sys, time
from datetime import datetime
from pathlib import Path

SCAN_SLEEP = float(os.environ.get("MITM_SLEEP", "0.5"))
SSH_TIMEOUT = 60
DEFAULT_LEDGER = os.environ.get("MITM_LEDGER", "~/chan_logs/scan_ledger.jsonl")

# --- 批间检测参数 (v4) ---
BATCH_SIZE = int(os.environ.get("MITM_BATCH_SIZE", "20"))          # 每批只数
BATCH_TIMEOUT = int(os.environ.get("MITM_BATCH_TIMEOUT",
                                   str(BATCH_SIZE * 60)))           # 单批超时 (s)

# --- 汇合即停参数 ---
SYNC_EVERY = int(os.environ.get("MITM_SYNC_EVERY", "10"))          # 每扫 N 只同步一次
SYNC_TIMEOUT = int(os.environ.get("MITM_SYNC_TIMEOUT", "60"))      # 单次 rclone 超时
DEFAULT_SHARE_REMOTE = os.environ.get("MITM_SHARE_REMOTE", "gdrive:mtim_ledger")
MIN_SCANNED_BEFORE_STOP = int(os.environ.get("MITM_MIN_BEFORE_STOP", "1"))

# dual2 输出行: "  [1/20] 000001 name A=... → GATE (...)"
_LINE_PAT = re.compile(r"^\s*\[(\d+)/(\d+)\]\s+(\d{6})\b.*?→\s*(\S+)")


def expand(p):
    return os.path.expanduser(p)


def ledger_done_codes(ledger_path, today_only=True):
    """读 MTIM 进度账本已覆盖的唯一 code 集合 (当日维度, 增量语义)。

    修复前: 每次重启都从全量清单头/尾重扫, 导致 watchdog 重启后
    反复扫同一批 code (账本 5943 行只有 1301 唯一码), 全市场永远扫不完。

    today_only=True: 只统计 ts 为【当日】的条目。
      同一交易日内 watchdog 重启 → 续扫 (增量, 不重复扫)
      跨到新交易日 → 自动归零重新扫全市场 (每日循环才不会第二天就空转)
    """
    done = set()
    today = _mtim_cut_ts()
    try:
        with open(expand(ledger_path)) as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                c = d.get("code")
                if not c:
                    continue
                if today_only and str(d.get("ts") or "") < today:
                    continue
                done.add(c)
    except (FileNotFoundError, OSError):
        pass
    return done


def load_codes(path, side):
    """两端各扫全量清单, 方向相反 (汇合即停的前置条件)。"""
    all_codes = [ln.strip() for ln in open(expand(path)) if ln.strip()]
    codes = list(reversed(all_codes)) if side == "desc" else list(all_codes)
    print(f"[init] side={side} universe={len(all_codes)} "
          f"assigned={len(codes)} first={codes[0]} last={codes[-1]}", flush=True)
    return codes


def parse_batch_output(text):
    """从 dual2_scan.py stdout 解析每只的 (code, gate)。"""
    out = []
    for line in text.splitlines():
        m = _LINE_PAT.match(line)
        if m:
            out.append({"code": m.group(3), "gate": m.group(4)})
    return out


def run_dual2_batch(codes, dual2_dir, kline_override=None,
                    dual2_ledger=None, vm_tag=None):
    """批量调用 dual2_scan.py --codes (逗号列表, 走 Stage2 快路径)。

    返回 [(code, gate), ...], 解析自 stdout。dual2 自身会把 6 字段写入
    dual2_ledger (DL_P 链路), 本函数不负责那部分。
    """
    env = os.environ.copy()
    if kline_override:
        env["KLINE_CACHE_DIR"] = kline_override
    cmd = [sys.executable, "dual2_scan.py", "--codes", ",".join(codes)]
    if dual2_ledger:
        cmd += ["--ledger", expand(dual2_ledger)]
    if vm_tag:
        cmd += ["--vm", vm_tag]
    proc = subprocess.run(
        cmd, cwd=dual2_dir, env=env,
        capture_output=True, text=True, timeout=BATCH_TIMEOUT,
    )
    return parse_batch_output(proc.stdout + "\n" + proc.stderr)


def _ssh_cmd(host, key=None):
    cmd = ["ssh"]
    if key: cmd += ["-i", expand(key)]
    cmd += ["-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=no", host]
    return cmd


def _remote_ledger_path(local_ledger: str, ssh_host: str) -> str:
    """SSH 模式下把【本地】账本路径映射为【远端】对应路径。

    bug(2026-09-21 修复): 原实现把 VM-A 本地路径 (/home/gorgesoros39/chan_logs/...)
      原样发给 VM-B 执行 `cat >>`, 而 VM-B 上 /home/gorgesoros39 属主为 gorgesoros39、
      权限 drwxr-x---, 远端用户 katelolita7788 无写权限 -> 每次 Permission denied,
      每只标的白跑一次 SSH 往返, 扫描被拖慢(3h25m 仅 union=1280/3195)。
    fix: 按远端 ssh 用户名重写 home 段 /home/<user>/...;
         可用环境变量 MITM_REMOTE_LEDGER 显式覆盖(优先, 防路径规则变化)。
    """
    override = os.environ.get("MITM_REMOTE_LEDGER")
    if override:
        return override
    user = ssh_host.split("@")[0] if "@" in ssh_host else None
    m = re.match(r"^/home/[^/]+/(.*)$", str(local_ledger))
    if user and m:
        return f"/home/{user}/{m.group(1)}"
    return str(local_ledger)


def append_ledger(ledger, entry, ssh_host=None, ssh_key=None):
    if ssh_host:
        remote_ledger = _remote_ledger_path(ledger, ssh_host)
        remote_json = json.dumps(entry, ensure_ascii=False)
        proc = subprocess.run(
            _ssh_cmd(ssh_host, ssh_key) + [f"cat >> {remote_ledger}"],
            input=remote_json + "\n", text=True,
            capture_output=True, timeout=SSH_TIMEOUT,
        )
        if proc.returncode != 0:
            print(f"[WARN] ssh ledger write failed: {proc.stderr.strip()[:120]}", flush=True)
    else:
        with open(expand(ledger), "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 跨机共享账本 (rclone) —— 汇合检测
# ---------------------------------------------------------------------------

def _rclone(args, timeout=SYNC_TIMEOUT):
    """跑一次 rclone, 返回 (rc, stdout, stderr)。失败不抛异常。"""
    try:
        p = subprocess.run(["rclone"] + args, capture_output=True,
                           text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except FileNotFoundError:
        return 127, "", "rclone not found"
    except subprocess.TimeoutExpired:
        return 124, "", f"rclone timeout {timeout}s"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


def _has_rclone():
    rc, _, _ = _rclone(["--version"], timeout=15)
    return rc == 0


def _mtim_cut_ts():
    """汇合判据的滚动时间下界: 最近 N 小时内的对端记录才算本轮有效覆盖。

    不用 datetime.now().date() 硬判当日 —— 扫描常跨午夜(15:40 起跑到次日凌晨),
    隔夜后「当日」翻转会导致对端记录被全部过滤, peer 恒 0, 两端永不汇合。
    MTIM_WINDOW_H 默认 20h: 覆盖整轮隔夜扫描, 又能在次日午后新轮次排除昨天的记录。
    """
    import datetime as _dt
    now = _dt.datetime.now()
    # 交易日以 15:35 (K线刷新/收盘) 为界, 而不是自然日:
    #   盘后 15:40 起跑      → cut = 今日 15:30 (新交易日, 全量重扫)
    #   跨午夜延续到次日凌晨 → cut = 昨日 15:30 (同一轮, 不断裂)
    cut = now.replace(hour=15, minute=30, second=0, microsecond=0)
    if now < cut:
        cut -= _dt.timedelta(days=1)
    return cut.strftime("%Y-%m-%d %H:%M:%S")


class MeetDetector:
    """共享账本轮询: 本地写 + push 自己那份 + pull 对方那份 + 统计行数。"""

    def __init__(self, side, share_remote, local_ledger, enabled=True):
        self.side = side
        self.peer = "desc" if side == "asc" else "asc"
        self.share_remote = share_remote.rstrip("/")
        self.local = expand(local_ledger)
        self.enabled = enabled and _has_rclone()
        self.peer_scanned = 0
        self.peer_codes = set()
        self.peer_synced_at = None
        self.push_fail = 0
        self.pull_fail = 0
        if not self.enabled:
            print("[meet] rclone 不可用 → 退化为单端扫全量 (无汇合检测)", flush=True)

    def _local_lines(self):
        try:
            with open(self.local) as f:
                return sum(1 for ln in f if ln.strip())
        except FileNotFoundError:
            return 0

    def sync(self):
        """push 自己 + pull 对方, 更新 peer_scanned。返回是否成功。"""
        if not self.enabled:
            return False
        ok = True
        # push: 本地账本 -> 共享盘 <side>.jsonl
        rc, _, err = _rclone(["copyto", self.local,
                              f"{self.share_remote}/{self.side}.jsonl",
                              "--size-only"])
        if rc != 0:
            self.push_fail += 1
            ok = False
            print(f"[meet] push 失败 rc={rc} {err.strip()[:100]}", flush=True)

        # pull: 共享盘 <peer>.jsonl -> 本地临时
        peer_local = os.path.join(os.path.dirname(self.local), f"peer_{self.peer}.jsonl")
        rc, _, err = _rclone(["copyto", f"{self.share_remote}/{self.peer}.jsonl",
                              peer_local, "--size-only"])
        if rc != 0:
            self.pull_fail += 1
            ok = False
        else:
            try:
                with open(peer_local) as f:
                    _seen = set()
                    _today = _mtim_cut_ts()
                    for _ln in f:
                        _ln = _ln.strip()
                        if not _ln:
                            continue
                        try:
                            _d = json.loads(_ln)
                            _c = _d.get("code")
                            # 只认对端【当日】扫描: 历史记录算进覆盖会导致假汇合
                            if str(_d.get("ts") or "") < _today:
                                continue
                        except Exception:
                            _c = _ln
                        if _c:
                            _seen.add(_c)
                    self.peer_scanned = len(_seen)
                    self.peer_codes = _seen
                self.peer_synced_at = datetime.now().strftime("%H:%M:%S")
            except FileNotFoundError:
                ok = False
        return ok

    def met(self, self_codes, universe_n):
        """汇合判据: 两端【当日】已覆盖 code 的并集 >= 全量。

        用并集而非求和: 两端扫到同一只只算一次, 避免高估导致提前停止。
        """
        return len(set(self_codes) | self.peer_codes) >= universe_n

    def status(self, self_codes, universe_n):
        u = set(self_codes) | self.peer_codes
        return (f"self={len(set(self_codes))} peer={len(self.peer_codes)} "
                f"union={len(u)}/{universe_n}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--side", required=True, choices=["asc", "desc"])
    ap.add_argument("--codes", required=True)
    ap.add_argument("--dual2-dir", default=".")
    ap.add_argument("--ledger", default=DEFAULT_LEDGER,
                    help="MTIM 自身进度账本 (用于汇合计数), 建议 mtim_ledger/<side>.jsonl")
    ap.add_argument("--dual2-ledger", default=None,
                    help="传给 dual2_scan.py 的 6 字段 DL_P 账本路径")
    ap.add_argument("--kline-override", default=None)
    ap.add_argument("--vm", default=None, help="传给 dual2_scan.py 的 VM 标记 (A/B)")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-key", default=None)
    ap.add_argument("--share-remote", default=DEFAULT_SHARE_REMOTE,
                    help="共享盘 rclone remote 路径 (默认 $MITM_SHARE_REMOTE)")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略账本进度, 强制全量重扫")
    ap.add_argument("--no-meet", action="store_true",
                    help="关闭汇合检测 (退化为: 本端扫完全量清单才停)")
    ap.add_argument("--split", action="store_true",
                    help="[已废弃] 静态砍半, 仅作降级开关")
    args = ap.parse_args()

    vm_tag = args.vm or ("A" if args.side == "asc" else "B")

    all_codes = [ln.strip() for ln in open(expand(args.codes)) if ln.strip()]
    universe_n = len(all_codes)

    if args.split:
        # 降级: 静态砍半 (不推荐, 保留仅为应急回退)
        mid = (universe_n + 1) // 2
        codes = all_codes[:mid] if args.side == "asc" else list(reversed(all_codes[mid:]))
        print(f"[init] side={args.side} SPLIT-MODE universe={universe_n} "
              f"assigned={len(codes)}", flush=True)
    else:
        codes = load_codes(args.codes, args.side)

    # 增量续扫: 跳过本端账本【当日】已扫过的 code (修复: 重启后从头重扫)
    done = set() if args.fresh else ledger_done_codes(expand(args.ledger))
    if done:
        _before = len(codes)
        codes = [c for c in codes if c not in done]
        print(f"[resume] 账本已覆盖 {len(done)} 只唯一码 → 待扫 {len(codes)} 只 "
              f"(全量 {_before})", flush=True)
    else:
        print("[resume] 当日账本为空 → 首轮全量扫", flush=True)

    print(f"[start] side={args.side} vm={vm_tag} assigned={len(codes)} "
          f"universe={universe_n} batch={BATCH_SIZE} "
          f"ledger={expand(args.ledger)} dual2_ledger={expand(args.dual2_ledger) if args.dual2_ledger else 'default'} "
          f"ssh={args.ssh_host or 'local'} share={args.share_remote}", flush=True)

    detector = MeetDetector(args.side, args.share_remote, args.ledger,
                            enabled=not args.no_meet)

    # --- 对撞: 启动时同步对端账本, 剥掉对端本轮已扫的码 ---
    # A 升序 / B 降序, 两端前期无重叠, 仅靠批内过滤无效; 必须在算 n_batches 前剥掉
    # 保险: peer 覆盖 >90% 全量时停用, 防对端数据错乱导致本端空转
    if detector.enabled:
        detector.sync()
        if detector.peer_codes and len(detector.peer_codes) < universe_n * 0.9:
            _n1 = len(codes)
            codes = [c for c in codes if c not in detector.peer_codes]
            print(f"[resume] 对撞: 排除对端已扫 {_n1 - len(codes)} 只 → "
                  f"待扫 {len(codes)} 只 (全量 {universe_n})", flush=True)

    n_batches = (len(codes) + BATCH_SIZE - 1) // BATCH_SIZE
    # 跨轮累积: 已扫数从账本去重计数起算 (修复: 重启后归零导致永不汇合)
    covered = set(done & set(all_codes))
    seen = len(covered)
    print(f"[resume] seen 起算 = {seen}/{universe_n}", flush=True)
    stopped_by = "exhausted"
    skipped_by_peer = 0
    skipped_codes = 0
    for b in range(n_batches):
        batch = codes[b * BATCH_SIZE:(b + 1) * BATCH_SIZE]
        # --- 对撞优化: 跳过对端本轮已扫的码, 避免两端重复劳动 ---
        # 保险: peer 覆盖 >90% 全量时停用, 防对端数据错乱导致本端空转
        _peer = getattr(detector, "peer_codes", None) or set()
        if _peer and len(_peer) < universe_n * 0.9:
            _n0 = len(batch)
            batch = [c for c in batch if c not in _peer]
            if _n0 and len(batch) < _n0:
                skipped_by_peer += 1
                skipped_codes += (_n0 - len(batch))
        if not batch:
            continue
        t0 = time.time()
        try:
            results = run_dual2_batch(batch, args.dual2_dir, args.kline_override,
                                      args.dual2_ledger, vm_tag)
        except subprocess.TimeoutExpired:
            print(f"[batch {b+1}/{n_batches}] TIMEOUT (> {BATCH_TIMEOUT}s) "
                  f"sent={len(batch)} → 记为已扫(UNKNOWN)", flush=True)
            results = []
        except Exception as e:
            print(f"[batch {b+1}/{n_batches}] ERROR {type(e).__name__}: {e} "
                  f"sent={len(batch)} → 记为已扫(UNKNOWN)", flush=True)
            results = []

        elapsed = time.time() - t0
        parsed = {r["code"]: r["gate"] for r in results}
        # 把本批每只都记入 MTIM 进度账本 (已扫即计数, 防无限重扫)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code in batch:
            entry = {
                "code": code, "gate": parsed.get(code, "UNKNOWN"),
                "vm": vm_tag, "ts": ts,
            }
            append_ledger(args.ledger, entry, args.ssh_host, args.ssh_key)
            seen += 1
        covered.update(batch)
        seen = len(covered)

        if (b + 1) % 5 == 0 or b == 0 or (b + 1) == n_batches:
            print(f"[batch {b+1}/{n_batches}] sent={len(batch)} "
                  f"scanned={seen}/{universe_n} {elapsed:.1f}s "
                  f"gates={ {g: sum(1 for r in results if r['gate']==g) for g in set(parsed.values())} }",
                  flush=True)

        # --- 汇合检测 (批间) ---
        if detector.enabled:
            detector.sync()
            if seen >= MIN_SCANNED_BEFORE_STOP and detector.met(covered, universe_n):
                print(f"[meet] 汇合! {detector.status(covered, universe_n)} "
                      f"peer_synced={detector.peer_synced_at} → 停止", flush=True)
                stopped_by = "meet"
                break
            print(f"[meet] {detector.status(covered, universe_n)}", flush=True)

        time.sleep(SCAN_SLEEP)

    print(f"[done] side={args.side} vm={vm_tag} scanned={seen} "
          f"stopped_by={stopped_by} skip_batches={skipped_by_peer} "
          f"skip_codes={skipped_codes} push_fail={detector.push_fail} "
          f"pull_fail={detector.pull_fail}", flush=True)
    # W1-3: 正常结束写完成标记, 供 watchdog 判"已完成"(不误判卡死/不重复拉起)
    try:
        _done_path = os.path.expanduser(f"~/chan_logs/.mtim_scan_{args.side}.done")
        with open(_done_path, "w") as _f:
            json.dump({"ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "side": args.side, "vm": vm_tag, "scanned": seen,
                       "stopped_by": stopped_by}, _f)
    except Exception as _e:
        print(f"[done-marker] write failed: {_e}", flush=True)



if __name__ == "__main__":
    main()
