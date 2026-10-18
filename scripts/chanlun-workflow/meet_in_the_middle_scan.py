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
BATCH_TIMEOUT = int(os.environ.get("MITM_BATCH_TIMEOUT", "600"))   # 单批超时 (默认 600s/10分钟)

# --- 汇合即停参数 ---
SYNC_TIMEOUT = int(os.environ.get("MITM_SYNC_TIMEOUT", "60"))
SHARE_MODE = os.environ.get("MITM_SHARE_MODE", "rsync")       # 默认采用内网 rsync 直连模式
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
    import os
    if os.environ.get("MTIM_NO_DATE_FILTER"):
        today_only = False
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
                # P1-7 (2026-10-01): UNKNOWN 不算成功。超时/异常批次此前记 UNKNOWN
                # 却计入 done，重启后静默跳过、永不重试。此处排除，UNKNOWN 码重回待扫。
                if str(d.get("gate") or "") == "UNKNOWN":
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
    proc = subprocess.Popen(
        cmd, cwd=dual2_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        start_new_session=True
    )
    try:
        stdout, stderr = proc.communicate(timeout=BATCH_TIMEOUT)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        proc.communicate()
        raise
    return parse_batch_output(stdout + "\n" + stderr)


def append_ledger(ledger, entry, ssh_host=None, ssh_key=None):
    # 始终写本地账本（sync() push 的源，必须更新，否则对端 peer 永远读不到）。
    # P1-3 (2026-10-01): ssh_host/ssh_key 参数保留仅为兼容旧调用，双机已统一为
    # 始终写本地 + sync() 推共享盘；B 机旧的 SSH 直写对端分支已随双机对齐退役。
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

def _rsync_push_pull(local_file, remote_user_host, remote_path, direction, ssh_key=None, timeout=60):
    import subprocess
    ssh_cmd = "ssh"
    if ssh_key:
        ssh_cmd = "ssh -i %s -o ConnectTimeout=10 -o BatchMode=yes -o StrictHostKeyChecking=accept-new" % ssh_key
    if direction == "push":
        src, dst = local_file, "%s:%s" % (remote_user_host, remote_path)
    else:
        src, dst = "%s:%s" % (remote_user_host, remote_path), local_file
    cmd = ["rsync", "-avz", "-e", ssh_cmd, src, dst]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stderr.strip()[:100]
    except FileNotFoundError:
        return 127, "rsync not found"
    except subprocess.TimeoutExpired:
        return 124, "rsync timeout %ss" % timeout
    except Exception as e:
        return 1, "%s: %s" % (type(e).__name__, e)

def _get_peer_b_host():
    import subprocess, re, os
    try:
        p = subprocess.run(["bash", "-c", "source ~/chan_logs/peer_ip.sh 2>/dev/null && peer_b_ip"],
                           capture_output=True, text=True, timeout=10)
        ip = p.stdout.strip()
        if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            return "katelolita7788@%s" % ip
    except Exception:
        pass
    cache_path = os.path.expanduser("~/chan_logs/.peer_b_ip.cache")
    if os.path.exists(cache_path):
        try:
            with open(cache_path) as f:
                ip = f.read().strip()
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
                    return "katelolita7788@%s" % ip
        except Exception:
            pass
    return "katelolita7788@35.212.213.72"


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
        import os
        if os.environ.get("MTIM_NO_MEET"):
            enabled = False
        if SHARE_MODE == "rsync":
            self.enabled = enabled
        else:
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
        # rsync 模式: A(asc) 经 SSH 直连 B, B 只写本地由 A 拉取
        if SHARE_MODE == "rsync":
            return self._sync_rsync()
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
                            # P1-7: 对端 UNKNOWN 同样不算成功
                            if str(_d.get("gate") or "") == "UNKNOWN":
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

    def _sync_rsync(self):
        """rsync 直连模式: A(asc) 双向同步, B(desc) 只读本地。"""
        import os
        ok = True
        peer_local = os.path.join(os.path.dirname(self.local), f"peer_{self.peer}.jsonl")
        if self.side == "asc":
            # A: push 自己 -> B, pull B -> 本地
            b_host = _get_peer_b_host()
            ssh_key = os.path.expanduser("~/.ssh/hermes_key")
            b_ledger_dir = "/home/katelolita7788/chan_logs/mtim_ledger"
            # push asc.jsonl -> B
            rc, err = _rsync_push_pull(self.local, b_host, "%s/asc.jsonl" % b_ledger_dir,
                                       "push", ssh_key=ssh_key)
            if rc != 0:
                self.push_fail += 1
                ok = False
                print(f"[meet-rsync] push 失败 rc={rc} {err}", flush=True)
            # pull desc.jsonl <- B
            rc, err = _rsync_push_pull(peer_local, b_host, "%s/desc.jsonl" % b_ledger_dir,
                                       "pull", ssh_key=ssh_key)
            if rc != 0:
                self.pull_fail += 1
                ok = False
            else:
                self._parse_peer(peer_local)
        else:
            # B: 只读本地 peer_asc.jsonl (由 A push 过来的)
            # push 由 A 完成 (A 会来拉 desc.jsonl)
            b_ledger_dir = os.path.dirname(self.local)
            peer_asc = os.path.join(b_ledger_dir, "asc.jsonl")
            if os.path.exists(peer_asc):
                self._parse_peer(peer_asc)
            # B 不主动 push, A 会来拉
        return ok

    def _parse_peer(self, peer_file):
        """解析对端账本文件, 更新 peer_scanned/peer_codes。"""
        import json
        try:
            with open(peer_file) as f:
                _seen = set()
                _today = _mtim_cut_ts()
                for _ln in f:
                    _ln = _ln.strip()
                    if not _ln:
                        continue
                    try:
                        _d = json.loads(_ln)
                        _c = _d.get("code")
                        if str(_d.get("ts") or "") < _today:
                            continue
                        if str(_d.get("gate") or "") == "UNKNOWN":
                            continue
                    except Exception:
                        _c = _ln
                    if _c:
                        _seen.add(_c)
                self.peer_scanned = len(_seen)
                self.peer_codes = _seen
            from datetime import datetime
            self.peer_synced_at = datetime.now().strftime("%H:%M:%S")
        except FileNotFoundError:
            pass

    def met(self, self_codes, universe_n):
        """汇合判据: 两端【当日】已覆盖 code 的并集 >= 全量。

        用并集而非求和: 两端扫到同一只只算一次, 避免高估导致提前停止。

        BUGFIX 2026-10-12: 计数达标后加新鲜度抽查。20h 窗口账本会导致
        resume 旧记录被算入，union 虚高（如 1840/3197 误判汇合）。
        抽查 20 只，<90% 为当日新鲜则不判汇合。
        """
        _union = set(self_codes) | self.peer_codes
        if len(_union) < universe_n:
            return False
        # 新鲜度抽查：从 union 中抽 20 只，查 ledger ts 是否为当日
        return self._fresh_check(list(_union)[:20])

    def _fresh_check(self, sample_codes, min_ratio=0.9):
        """抽查 ledger 新鲜度：sample 中 ts 为当日的比例 >= min_ratio"""
        import datetime as _dt
        _today_str = _dt.datetime.now().strftime("%Y-%m-%d")
        _fresh = 0
        _total = 0
        try:
            with open(expand(self.local)) as f:
                _ts_map = {}
                for _ln in f:
                    try:
                        _d = __import__('json').loads(_ln.strip())
                        _c = _d.get("code")
                        if _c and _c not in _ts_map:
                            _ts_map[_c] = str(_d.get("ts") or "")
                    except:
                        continue
            for _c in sample_codes:
                _ts = _ts_map.get(_c, "")
                _total += 1
                if _ts.startswith(_today_str):
                    _fresh += 1
        except:
            return False
        return _total > 0 and (_fresh / _total) >= min_ratio

    def status(self, self_codes, universe_n):
        u = set(self_codes) | self.peer_codes
        return (f"self={len(set(self_codes))} peer={len(self.peer_codes)} "
                f"union={len(u)}/{universe_n}")


def _freshness_gate(codes, kline_dir, expected_date=None, sample_n=20, min_ratio=0.9):
    """K线新鲜度门: 抽查 sample_n 只的 day.csv 最新日期。
    返回 (ok, msg)。ok=False 时调用方应中止扫描。
    expected_date: 期望的交易日 (YYYY-MM-DD)，默认取"上一交易日"逻辑——
      简化为: 用样本中最常见的日期作为基准，要求 >=90% 样本等于基准。
    环境变量 MITM_NO_FRESHNESS=1 或 --no-freshness-check 可 bypass。"""
    import os, random
    if os.environ.get("MITM_NO_FRESHNESS"):
        return True, "bypassed by env"
    if not codes:
        return True, "no codes to check"
    sample = random.sample(codes, min(sample_n, len(codes)))
    dates = {}
    missing = 0
    for c in sample:
        fp = os.path.join(kline_dir, "%s_day.csv" % c)
        try:
            with open(fp) as f:
                lines = f.readlines()
                if not lines:
                    missing += 1
                    continue
                last = lines[-1].strip().split(",")[0].strip()
                dates[last] = dates.get(last, 0) + 1
        except FileNotFoundError:
            missing += 1
        except Exception:
            missing += 1
    if not dates:
        return False, "CRITICAL: 抽查 %d 只全部缺 K 线文件" % len(sample)
    baseline = max(dates, key=dates.get)
    baseline_n = dates[baseline]
    ratio = baseline_n / len(sample)
    msg = "抽查%d只: 基准日期%s(%d只,%.0f%%), 缺文件%d只" % (
        len(sample), baseline, baseline_n, ratio * 100, missing)
    if ratio < min_ratio:
        return False, "CRITICAL: K线新鲜度不足: " + msg
    return True, "OK: " + msg


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
    ap.add_argument("--no-meet", action="store_true",
                    help="禁用汇合检测（单端跑时用，防 peer 旧账本误判提前停）")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-key", default=None)
    ap.add_argument("--share-remote", default=DEFAULT_SHARE_REMOTE,
                    help="共享盘 rclone remote 路径 (默认 $MITM_SHARE_REMOTE)")
    ap.add_argument("--fresh", action="store_true",
                    help="忽略账本进度, 强制全量重扫")
    ap.add_argument("--no-freshness-check", action="store_true", help="跳过K线新鲜度门")
    ap.add_argument("--share-mode", default=None, choices=["rclone", "rsync"])
    ap.add_argument("--split", action="store_true",
                    help="[已废弃] 静态砍半, 仅作降级开关")
    args = ap.parse_args()
    global SHARE_MODE
    if args.share_mode:
        SHARE_MODE = args.share_mode
    print("[init] SHARE_MODE=%s" % SHARE_MODE, flush=True)

    # K线新鲜度门 (2026-10-11): 扫描前抽查 K 线日期，不新鲜则中止
    if not args.no_freshness_check:
        _codes_for_gate = []
        try:
            with open(expand(args.codes)) as _f:
                _codes_for_gate = [ln.strip() for ln in _f if ln.strip()]
        except Exception as _e:
            print("[freshness] 读代码列表失败: %s" % _e, flush=True)
        _kline_dir = os.environ.get("KLINE_CACHE_DIR", "./kline_cache")
        _ok, _msg = _freshness_gate(_codes_for_gate, expand(_kline_dir))
        print("[freshness] %s" % _msg, flush=True)
        if not _ok:
            print("[freshness] 中止扫描。用 --no-freshness-check 强制绕过。", flush=True)
            sys.exit(2)

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
    pending_unknown = set()  # P1-7: 本轮 UNKNOWN 码，help 阶段认领重试
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
                  f"sent={len(batch)} → 记 UNKNOWN，不计入汇合", flush=True)
            results = []
        except Exception as e:
            print(f"[batch {b+1}/{n_batches}] ERROR {type(e).__name__}: {e} "
                  f"sent={len(batch)} → 记 UNKNOWN，不计入汇合", flush=True)
            results = []

        elapsed = time.time() - t0
        parsed = {r["code"]: r["gate"] for r in results}
        # P1-7 (2026-10-01): 账本照记 UNKNOWN（可观测），但汇合判据只认真实 gate。
        # UNKNOWN 码不进 covered，后续 help 阶段自动认领重试；防无限重扫靠“每码每轮
        # 最多主批+help 各一次”，不靠虚假计数。
        # BUGFIX (2026-10-05): 账本与批次文件不同步导致 20 只丢失。
        # 修复：写账本前验证批次文件落盘成功且包含该码，否则记 UNKNOWN 重试。
        # 批次文件路径：dual2_scan.py 写到 LOGDIR/dualscan_{ts}.json
        _batch_file_ok = {}
        try:
            import glob
            _today = datetime.now().strftime('%Y%m%d')
            # 找最近 5 分钟内的批次文件（本批刚写）
            _candidates = sorted(glob.glob(os.path.expanduser(f'~/chan_logs/dualscan_{_today}_*.json')),
                               key=os.path.getmtime, reverse=True)[:3]
            for _bf in _candidates:
                try:
                    _bd = json.load(open(_bf))
                    for _r in _bd.get('dual', []):
                        _c = _r.get('code')
                        if _c:
                            _batch_file_ok[_c] = True
                except:
                    pass
        except:
            pass
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for code in batch:
            gate = parsed.get(code, "UNKNOWN")
            # 验证：批次文件里必须有这只，否则不算完成
            if gate != "UNKNOWN" and code not in _batch_file_ok:
                print(f"[verify] {code} 账本有但批次文件缺失 → 记 UNKNOWN 重试", flush=True)
                gate = "UNKNOWN"
            entry = {
                "code": code, "gate": gate,
                "vm": vm_tag, "ts": ts,
            }
            append_ledger(args.ledger, entry, args.ssh_host, args.ssh_key)
            if gate != "UNKNOWN":
                covered.add(code)
            else:
                pending_unknown.add(code)
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

    # ── 真·互助: 本端跑完后，帮对端扫剩余 ──
    # BUGFIX (2026-10-05): meet 停止后也要跑 help 做最终校验。
    # 原因：meet 判定依据账本计数，账本可能虚高（如 20 只丢失事件）。
    # 此前 `stopped_by != "meet"` 直接跳过，导致 5 只（603257-603261）永远没人扫。
    # 修复：meet 后也做一次全量核对，缺谁补谁。
    if detector.enabled:
        detector.sync()
        _peer = getattr(detector, "peer_codes", None) or set()
        _all = set(all_codes)
        # 对方未扫、本端未扫 = 可认领
        _claimable = [c for c in all_codes if c not in _peer and c not in covered]
        if _claimable:
            if stopped_by == "meet":
                print(f"[help] meet 后最终校验：发现 {len(_claimable)} 只漏网，开始补扫", flush=True)
            else:
                print(f"[help] 本端已完，认领对端剩余 {len(_claimable)} 只开始帮扫", flush=True)
            _hb = 0
            for _i in range(0, len(_claimable), BATCH_SIZE):
                _batch = _claimable[_i:_i + BATCH_SIZE]
                _hb += 1
                try:
                    _results = run_dual2_batch(_batch, args.dual2_dir, args.kline_override,
                                              args.dual2_ledger, vm_tag)
                except Exception as _e:
                    print(f"[help batch {_hb}] ERROR {_e} → 记 UNKNOWN，不计入汇合", flush=True)
                    _results = []
                _parsed = {r["code"]: r["gate"] for r in _results}
                _ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                for _code in _batch:
                    _gate = _parsed.get(_code, "UNKNOWN")
                    _entry = {"code": _code, "gate": _gate,
                              "vm": vm_tag, "ts": _ts}
                    append_ledger(args.ledger, _entry, args.ssh_host, args.ssh_key)
                    if _gate != "UNKNOWN":
                        covered.add(_code)
                seen = len(covered)
                print(f"[help batch {_hb}] 帮扫 {len(_batch)} 只，累计 {seen}/{universe_n}", flush=True)
                time.sleep(SCAN_SLEEP)
        else:
            print("[help] 对方无剩余可认领", flush=True)

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
