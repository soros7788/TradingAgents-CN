#!/usr/bin/env python3
"""
universe_kline_refresh.py — 全宇宙每交易日增量刷新 + 1min 滚窗封顶 5000

基于 VM-A 现有 incremental_kline_sync.py 的 append-only 逻辑，扩展两处：
  1. 代码源改为 _codes_mainboard.txt（全 3195 主板/中小板，非创业板/科创板）
     —— 不是只更新 3 只，也不是只同步"已有 CSV 的码"
  2. 每个 _1m.csv 追加后若 >5000 行，截取最后 5000（防磁盘爆，用户硬性要求）

原则（沿用原脚本）：
  - NEVER overwrite。只追加 date > local_last 的新 bar（mode='a'），幂等。
  - 非交易日跳过（省流量）。
  - flock 防重叠运行。
  - 取数超时熔断(FETCH_TIMEOUT 默认 25s): akshare/baostock 挂死自动放弃, 不阻塞整轮。
  - 并发取数(MAX_WORKERS 默认 1 = 单线程, env KLINE_MAX_WORKERS 可调): 瓶颈为网络等待;
    ⚠️ 实测 worker=4 触发 akshare 分钟线限流(成片 empty + 重试)反而更慢, 故生产默认保持 1。
    并发时全局 QPS 由 KLINE_MIN_SUBMIT_INTERVAL(默认 0.15s)兜底防限流。
  - 写 GDrive hub（KLINE_CACHE_DIR 默认 FUSE 路径）；VM-A 本地副本由独立 rclone 拉取维持。

用法：
  python universe_kline_refresh.py                 # 全 3195 只，全周期（断点续跑，跨窗口累计）
  python universe_kline_refresh.py --limit 50      # 前 50 只冒烟（不写断点）
  python universe_kline_refresh.py --force         # 忽略交易日检查
  python universe_kline_refresh.py --reset         # 清断点，强制新周期
"""
import os, sys, time, argparse, fcntl, json
import concurrent.futures as _cf
# 2026-10-10: DummyExecutor 已移除，恢复真 ThreadPoolExecutor（线程无 bug，真凶是东财限流）
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import akshare as ak

SH_TZ = timezone(timedelta(hours=8))
KLINE_DIR = Path(os.environ.get(
    "KLINE_CACHE_DIR",
    str(Path.home() / "kline_cache_local")
))
UNIVERSE_FILE = Path(os.environ.get(
    "UNIVERSE_FILE",
    str(Path.home() / "TradingAgents-CN" / "kline_cache" / "_codes_mainboard.txt")
))
ONE_MIN_CAP = int(os.environ.get("ONE_MIN_CAP", "5000"))   # 1min 滚窗上限（防爆盘）

DATE_COL   = {"day": "date", "1m": "day", "5m": "day", "30m": "day"}
MIN_PERIOD = {"1m": "1", "5m": "5", "30m": "30"}
ALL_TFS    = ["day", "5m", "1m", "30m"]

# ── 限流检测器: 滑动窗口，触发时写 gdrive 信号 ──
import collections
class RateLimitDetector:
    def __init__(self, coord_id=None, window=60, fail_threshold=5, latency_threshold=10.0):
        self.coord_id = coord_id
        self.window = window
        self.fail_threshold = fail_threshold
        self.latency_threshold = latency_threshold
        self.events = collections.deque()  # (ts, ok, latency)
        self.triggered = False
    def record(self, ok, latency):
        now = time.time()
        self.events.append((now, ok, latency))
        while self.events and self.events[0][0] < now - self.window:
            self.events.popleft()
        self._check()
    def _check(self):
        if self.triggered or not self.coord_id:
            return
        recent = list(self.events)
        if not recent:
            return
        # 连续失败
        consec_fail = 0
        for _, ok, _ in reversed(recent):
            if not ok:
                consec_fail += 1
            else:
                break
        # 平均延迟
        avg_lat = sum(l for _, _, l in recent) / len(recent)
        if consec_fail >= self.fail_threshold or avg_lat > self.latency_threshold:
            self.triggered = True
            print(f"[ratelimit] 触发! 连续失败={consec_fail} 平均延迟={avg_lat:.1f}s", flush=True)
            self._signal()
    def _signal(self):
        import subprocess
        try:
            sig = Path.home() / f"kline_ratelimited_{self.coord_id}"
            sig.write_text(datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S"))
            subprocess.run(["rclone", "copyto", str(sig),
                            f"gdrive:TradingAgents-CN/signals/{self.coord_id}_ratelimited"],
                           timeout=30, capture_output=True)
            print(f"[ratelimit] 信号已发布: {self.coord_id}_ratelimited", flush=True)
        except Exception as e:
            print(f"[ratelimit] 信号发布失败: {e}", flush=True)
    def should_pause(self):
        return self.triggered

API_SLEEP  = 0.10

# ── A 项提吞吐(12.5 待办 A): 并发取数 worker ──
# 瓶颈 = akshare 单请求网络等待(实测 5-10s/单元), API_SLEEP 仅 0.1s 非瓶颈 → 只能靠并发重叠等待时间。
# MIN_SUBMIT_INTERVAL 为【全局】最小提交间隔(默认 0.15s ⇒ QPS ≤ ~6.7), 兜底防并发把 QPS 放大到触发限流/封 IP。
# ⚠️ 实测(2026-09-21): MAX_WORKERS=4 触发 akshare 分钟线限流 —— 成片 empty_result + retries=3 重试风暴,
#    200 单元 901s 仍跑不完, 反而【慢于】单线程; 单线程同批 24 单元 0 ERR(历史 2000 单元 ERR 率≈0.1%)。
#    → 生产默认 1(= 原单线程行为, 安全)。并发能力保留: env KLINE_MAX_WORKERS 可调,
#      待换同花顺数据源(已授权)或验证出安全并发度后再启用。
MAX_WORKERS = int(os.environ.get("KLINE_MAX_WORKERS", "1"))
MIN_SUBMIT_INTERVAL = float(os.environ.get("KLINE_MIN_SUBMIT_INTERVAL", "0.15"))
_TRADE_DATES_CACHE = None

# ── 超时熔断: 防 akshare/baostock 网络挂死导致整轮刷新永久阻塞(历史 635709 卡死根因) ──
# 每次取数放到 daemon 线程执行, join(timeout) 兜底; 超时即放弃本次取数(返回 None),
# 交由上层 retry / baostock 回退, 主循环绝不因单只代码卡死而停摆。
import threading
FETCH_TIMEOUT = float(os.environ.get("KLINE_FETCH_TIMEOUT", "25"))


def _fetch_with_timeout(fn, *args, **kw):
    """在 daemon 线程中执行 fn(*args, **kw), FETCH_TIMEOUT 秒内未返回则放弃(返回 None)。

    仅用于包裹会网络阻塞的取数; 超时视为失败, 触发上层 retry / baostock 回退。
    daemon 线程保证超时放弃后不阻塞整进程退出。
    """
    box = {}
    def _run():
        try:
            box["r"] = fn(*args, **kw)
        except Exception:
            box["r"] = None
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join(timeout=FETCH_TIMEOUT)
    return box.get("r") if not t.is_alive() else None


# ── 断点续跑: 记录已完成 (code|tf), 跨 VM 窗口/每日 timer 累计跑完全量 ──
# 历史根因: 全量 12780 单元×0.2/s≈17.6h, 但每轮从 codes[0] 重启 + VM 仅 10h/日,
#          → 永远跑不完, 后半段 5m 永远陈旧(首选=0 的源头)。续跑让已完成单元落盘,
#          下一轮跳过它们只取剩余, 多窗口累计直至 done==grand_total, 完成后自动开新周期。
CHECKPOINT_PATH = Path.home() / "kline_refresh_checkpoint.json"

def _load_checkpoint():
    """返回 {cycle:int, done:list[str], target_date:str}。文件损坏/缺失则返回 cycle=0, done=[]。
    2026-10-13: 增加 target_date 校验，目标日期变化时 checkpoint 失效（防 10-12 的 done 跳过 10-13 的任务）。"""
    try:
        if CHECKPOINT_PATH.exists():
            d = json.loads(CHECKPOINT_PATH.read_text())
            if isinstance(d, dict) and "done" in d:
                d.setdefault("cycle", 0)
                # 校验 target_date
                _cur_target = _current_target_date()
                if d.get("target_date") != _cur_target:
                    # 目标日期变了，清空 done，强制重查
                    return {"cycle": d.get("cycle", 0), "done": [], "target_date": _cur_target}
                return d
    except Exception:
        pass
    return {"cycle": 0, "done": [], "target_date": _current_target_date()}

def _current_target_date() -> str:
    """当前目标交易日（与 _append_only_inner 预检逻辑一致）：15:00 前为昨天，否则今天；周末顺延。"""
    from datetime import datetime, timedelta
    _now = datetime.now(SH_TZ)
    _today = _now.date()
    _wd = _today.weekday()
    if _wd == 5:
        return (_today - timedelta(days=1)).strftime("%Y-%m-%d")
    elif _wd == 6:
        return (_today - timedelta(days=2)).strftime("%Y-%m-%d")
    elif _now.strftime("%H:%M") < "15:00":
        _y = _today - timedelta(days=1)
        if _y.weekday() == 6:
            _y -= timedelta(days=2)
        elif _y.weekday() == 5:
            _y -= timedelta(days=1)
        return _y.strftime("%Y-%m-%d")
    else:
        return _today.strftime("%Y-%m-%d")

def _coord_publish(coord_id, done_keys, grand_total):
    """发布本机完整 done_keys 到 gdrive，供对方认领剩余"""
    import subprocess
    prog = {"coord_id": coord_id, "done": list(done_keys), "total": grand_total,
            "ts": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S")}
    pf = Path.home() / f"kline_progress_{coord_id}.json"
    pf.write_text(json.dumps(prog))
    try:
        subprocess.run(["rclone", "copyto", str(pf),
                        f"gdrive:TradingAgents-CN/kline_progress_{coord_id}.json"],
                       timeout=60, capture_output=True)
    except Exception:
        pass  # 发布失败不阻塞主流程

def _coord_get_other_remaining(coord_id, my_done_keys, all_keys):
    """读取对方 done_keys，返回对方还没做、本机也没做的 key 列表（可认领）"""
    import subprocess
    other = "b" if coord_id == "a" else "a"
    try:
        r = subprocess.run(["rclone", "cat",
                            f"gdrive:TradingAgents-CN/kline_progress_{other}.json"],
                           timeout=30, capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip():
            od = json.loads(r.stdout)
            other_done = set(od.get("done", []))
            # 对方未做 且 本机未做 = 可认领
            claimable = [k for k in all_keys if k not in other_done and k not in my_done_keys]
            return claimable
    except Exception:
        pass
    return []

def _save_checkpoint(cp):
    """原子落盘: 先写 .tmp 再 replace, 防 VM 断电/被杀导致 JSON 损坏。
    2026-10-13: 保存 target_date，用于下次加载时校验。"""
    tmp = CHECKPOINT_PATH.with_suffix(".tmp")
    try:
        cp["target_date"] = _current_target_date()
        tmp.write_text(json.dumps(cp))
        tmp.replace(CHECKPOINT_PATH)
    except Exception:
        pass


def is_trade_day(today: str = None) -> bool:
    global _TRADE_DATES_CACHE
    if today is None:
        today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    if _TRADE_DATES_CACHE is None:
        try:
            df = ak.tool_trade_date_hist_sina()
            _TRADE_DATES_CACHE = set(df["trade_date"].astype(str).tolist())
        except Exception:
            wd = datetime.strptime(today, "%Y-%m-%d").weekday()
            _TRADE_DATES_CACHE = None
            return wd < 5
    return today in _TRADE_DATES_CACHE


def _code_bs(code: str) -> str:
    return f"sh.{code}" if code.startswith(("6", "9")) else f"sz.{code}"


def _day_start_for(code: str) -> str:
    """增量优化: 已有本地 day 文件则只抓近 45 天窗口(含缓冲), 新码才拉全量。

    避免每日重抓 2021 全量历史(单股 5 年 ~1200 根, 全宇宙 3195 只 = 50h+ 不可行)。
    append_only 仍按 date>local_last 过滤, 故 45 天窗口足以覆盖间隙。
    """
    day_csv = KLINE_DIR / f"{code}_day.csv"
    if day_csv.exists():
        try:
            last = pd.read_csv(day_csv, usecols=["date"], dtype=str)["date"].iloc[-1]
            d = datetime.strptime(last, "%Y-%m-%d")
            d0 = d - timedelta(days=45)
            if d0.year >= 2021:
                return d0.strftime("%Y%m%d")
        except Exception:
            pass
    return "20210909"


def fetch_day_sina(code: str) -> pd.DataFrame:
    """新浪日线 (scale=240, 免 JS 引擎, 跨平台直通, 毫秒级返回)"""
    import json, urllib.request
    prefix = "sh" if code.startswith(("6", "9")) else "sz"
    url = (f"https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{code}&scale=240&ma=no&datalen=45")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
        data = j.get("result", {}).get("data", [])
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        if "day" not in df.columns:
            return pd.DataFrame()
        df = df.rename(columns={"day": "date"})
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        for c in ["amount", "outstanding_share", "turnover"]:
            df[c] = pd.NA
        return df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                  'outstanding_share', 'turnover']]
    except Exception:
        return pd.DataFrame()


def fetch_day(code: str) -> pd.DataFrame:
    # 2026-10-14: 新浪日线优先 (0.05s 直连，海外 GCP 与国内网络均畅通，免 py_mini_racer 引擎)
    try:
        df_sina = fetch_day_sina(code)
        if df_sina is not None and len(df_sina) > 0:
            return df_sina
    except Exception:
        pass
    start = _day_start_for(code)
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        df = _fetch_with_timeout(
            ak.stock_zh_a_daily,
            symbol=f"{prefix}{code}",
            start_date=start,
            end_date=datetime.now(SH_TZ).strftime("%Y%m%d"),
            adjust="qfq",
        )
        wanted = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                  'outstanding_share', 'turnover']
        for c in wanted:
            if c not in df.columns:
                df[c] = pd.NA
        return df[wanted]
    except Exception:
        pass
    import baostock as bs
    bs.login()
    try:
        rs = _fetch_with_timeout(
            bs.query_history_k_data_plus,
            _code_bs(code),
            "date,open,high,low,close,volume,amount,turn",
            start_date=d0.strftime("%Y-%m-%d") if (d0 := datetime.strptime(start, "%Y%m%d")) and d0.year >= 2021 else "2021-09-09",
            end_date=datetime.now(SH_TZ).strftime("%Y-%m-%d"),
            frequency="d", adjustflag="2",
        )
        if rs is None:
            raise RuntimeError("baostock timeout")
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            raise RuntimeError("baostock empty")
        df = pd.DataFrame(rows, columns=rs.fields)
        for c in ['open', 'high', 'low', 'close', 'volume', 'amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df['turnover'] = pd.to_numeric(df['turn'], errors='coerce') / 100.0
        df['outstanding_share'] = pd.NA
        return df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                  'outstanding_share', 'turnover']]
    finally:
        bs.logout()



def fetch_minute_sina(code: str, tf: str) -> pd.DataFrame:
    """新浪分钟线 (2026-10-10: 东财限流时切新浪)。
    限制: datalen<=1970, 无日期参数, 默认不复权。"""
    import json, urllib.request
    prefix = "sh" if code.startswith("6") else "sz"
    scale = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "60m": 60}.get(tf, 5)
    url = (f"https://quotes.sina.cn/cn/api/openapi.php/CN_MarketDataService.getKLineData"
           f"?symbol={prefix}{code}&scale={scale}&ma=no&datalen=1970")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            j = json.loads(resp.read().decode("utf-8", "ignore"))
        data = j.get("result", {}).get("data", [])
        if not data:
            return pd.DataFrame()
        df = pd.DataFrame(data)
        # 2026-10-12: 退市/ST/停牌票 sina 返回无 day 列，直接返回空触发 fallback/跳过
        if "day" not in df.columns:
            return pd.DataFrame()
        # day -> date, 格式 "2026-10-09 14:15:00"
        # 列名对齐东财: day（分钟线用 day，不用 date）
        df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        for c in ["open", "high", "low", "close", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["amount"] = pd.to_numeric(df.get("amount", 0), errors="coerce")
        return df[["day", "open", "high", "low", "close", "volume", "amount"]]
    except Exception as e:
        print(f"  [WARN] sina minute failed {code}/{tf}: {e}", file=sys.stderr)
        return pd.DataFrame()

# 2026-10-13: 东财域名轮换 (zwldarren/akshare-one commit 161ac3d 思路)
# push2.eastmoney.com 间歇性 502，三域名轮换；HTML 错误页也算失败
_EM_QUOTE_HOSTS = (
    "push2.eastmoney.com",
    "82.push2.eastmoney.com",
    "push2delay.eastmoney.com",
)
_EM_KLINE_HOSTS = ("push2his.eastmoney.com",)

# 2026-10-13: 东财字段映射表（学习 akshare PR #7427：按名取数，防上游改字段顺序）
# fields2 请求顺序决定返回的列顺序，用字段名映射而非硬编码位置
_EM_KLINE_FIELDS2 = ["f51", "f52", "f53", "f54", "f55", "f56", "f57", "f58", "f59", "f60", "f61"]
_EM_KLINE_FIELDMAP = {
    "f51": "day",     # 时间
    "f52": "open",    # 开
    "f53": "close",   # 收
    "f54": "high",    # 高
    "f55": "low",     # 低
    "f56": "volume",  # 量
    "f57": "amount",  # 额
    # f58-f61: 涨幅/换手等，暂不用
}
_EM_TIMEOUT = 15.0

def _em_get_json(path: str, params: dict, hosts: tuple) -> dict:
    """从多个东财域名依次尝试，返回可用 JSON；全失败抛异常。"""
    import requests
    last_err = None
    for host in hosts:
        url = f"https://{host}{path}"
        try:
            r = requests.get(url, params=params, timeout=_EM_TIMEOUT)
            r.raise_for_status()
            return r.json()  # HTML 错误页会抛 JSON 解析异常，算失败换下一台
        except Exception as e:
            last_err = e
            continue
    raise ConnectionError(f"All EastMoney hosts failed for {path}: {last_err}")

def fetch_minute_eastmoney(code: str, tf: str) -> pd.DataFrame:
    """东财分钟线（带域名轮换）。失败返回空 DataFrame。"""
    try:
        prefix = "1" if code.startswith("6") else "0"
        secid = f"{prefix}.{code}"
        klt_map = {"5m": "5", "15m": "15", "30m": "30", "60m": "60", "1m": "1"}
        klt = klt_map.get(tf, "5")
        params = {
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": klt,
            "fqt": "1",
            "secid": secid,
            "beg": "0",
            "end": "20500101",
        }
        data = _em_get_json("/api/qt/stock/kline/get", params, _EM_KLINE_HOSTS)
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return pd.DataFrame()
        # 按字段名映射取数（防东财改字段顺序）
        _idx = {f: i for i, f in enumerate(_EM_KLINE_FIELDS2)}
        rows = []
        for k in klines:
            parts = k.split(",")
            if len(parts) < len(_EM_KLINE_FIELDS2):
                continue
            try:
                rows.append({
                    "day": parts[_idx["f51"]],
                    "open": float(parts[_idx["f52"]]),
                    "close": float(parts[_idx["f53"]]),
                    "high": float(parts[_idx["f54"]]),
                    "low": float(parts[_idx["f55"]]),
                    "volume": int(float(parts[_idx["f56"]])),
                    "amount": float(parts[_idx["f57"]]),
                })
            except (ValueError, IndexError, KeyError):
                continue
        df = pd.DataFrame(rows)
        df["day"] = pd.to_datetime(df["day"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        return df[["day", "open", "high", "low", "close", "volume", "amount"]]
    except Exception as e:
        print(f"  [WARN] eastmoney minute failed {code}/{tf}: {e}", file=sys.stderr)
        return pd.DataFrame()


def fetch_minute(code: str, tf: str, retries: int = 3) -> pd.DataFrame:
    """Fetch minute K-line: 新浪优先 (2026-10-14 架构重构: 免阻断、超低延迟), 东财 fallback。"""
    # 1. 新浪优先
    df = fetch_minute_sina(code, tf)
    if df is not None and len(df) > 0:
        return df
    # 2. 东财 fallback
    df = fetch_minute_eastmoney(code, tf)
    if df is not None and len(df) > 0:
        return df
    # 3. akshare fallback
    prefix = "sh" if code.startswith("6") else "sz"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            df = _fetch_with_timeout(ak.stock_zh_a_minute, symbol=f"{prefix}{code}", period=MIN_PERIOD[tf], adjust="qfq")
            if df is None or len(df) == 0:
                last_err = "empty_result"
                time.sleep(0.5 * attempt); continue
            return df
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.5 * attempt)
    print(f"  [WARN] fetch_minute failed {code}/{tf} after {retries} attempts: {last_err}", file=sys.stderr)
    return pd.DataFrame()


def append_only(code: str, tf: str) -> dict:
    """追加新 bar, 幂等, 不覆盖. 与 incremental_kline_sync.py 同逻辑."""
    try:
        return _append_only_inner(code, tf)
    except KeyError as e:
        return {"code": code, "tf": tf, "ok": False, "error": f"KeyError: {e}"}

def _append_only_inner(code: str, tf: str) -> dict:
    csv_path = KLINE_DIR / f"{code}_{tf}.csv"
    # ── 预检 (2026-10-10): 本地已是最新交易日则跳过 API ──
    try:
        if csv_path.exists():
            _dcol_pre = "date" if tf == "day" else "day"
            _df_pre = pd.read_csv(csv_path, usecols=[_dcol_pre], dtype=str)
            if len(_df_pre) > 0:
                _last = str(_df_pre[_dcol_pre].iloc[-1])[:10]
                # 目标：最新交易日（2026-10-13: 15:00 前以昨天为目标，盘中数据不完整）
                _now = datetime.now(SH_TZ)
                _today = _now.date()
                _wd = _today.weekday()
                if _wd == 5:
                    _target = (_today - timedelta(days=1)).strftime("%Y-%m-%d")
                elif _wd == 6:
                    _target = (_today - timedelta(days=2)).strftime("%Y-%m-%d")
                elif _now.strftime("%H:%M") < "15:00":
                    _y = _today - timedelta(days=1)
                    if _y.weekday() == 6:
                        _y -= timedelta(days=2)
                    elif _y.weekday() == 5:
                        _y -= timedelta(days=1)
                    _target = _y.strftime("%Y-%m-%d")
                else:
                    _target = _today.strftime("%Y-%m-%d")
                if _last >= _target:
                    return {"code": code, "tf": tf, "ok": True, "skipped": True, "added": 0}
    except Exception:
        pass
    date_col = DATE_COL[tf]
    try:
        df_live = fetch_day(code) if tf == "day" else fetch_minute(code, tf)
        date_col = "day" if tf != "day" else "date"
        if df_live is None or len(df_live) == 0 or date_col not in df_live.columns:
            # 2026-10-13 17:10 修正：有本地数据时返回空=临时问题（数据未发布），不记完成以便重试
            # 无本地数据=异常股（ST/停牌/退市），记完成
            if csv_path.exists():
                return {"code": code, "tf": tf, "ok": False, "error": "empty_retry"}
            # 2026-10-13: 异常股（ST/停牌/退市）计入完成，不再算失败
            return {"code": code, "tf": tf, "ok": True, "skipped": True, "added": 0, "reason": "empty"}
    except Exception as e:
        err_msg = str(e)[:120]
        print(f"  [ERROR] append_only {code}/{tf}: {err_msg}", file=sys.stderr)
        return {"code": code, "tf": tf, "ok": False, "error": err_msg}
    if df_live is None or len(df_live) == 0:
        print(f"  [WARN] append_only {code}/{tf}: empty result (retries exhausted)", file=sys.stderr)
        return {"code": code, "tf": tf, "ok": False, "error": "empty"}
    df_live = df_live.copy()
    df_live[date_col] = df_live[date_col].astype(str)
    if csv_path.exists():
        df_local = pd.read_csv(csv_path, dtype={date_col: str})
        # 列名容错：历史缓存可能用 date/datetime 而非 day
        _dcol = date_col
        if _dcol not in df_local.columns:
            for _alt in ("date", "datetime", "时间"):
                if _alt in df_local.columns:
                    _dcol = _alt
                    break
        # 2026-10-12: 列名全对不上（退市/ST/停牌导致数据源异常），直接跳过
        # 2026-10-13: 计入完成
        if _dcol not in df_local.columns:
            return {"code": code, "tf": tf, "ok": True, "skipped": True, "added": 0, "reason": "column mismatch skip"}
        local_rows = len(df_local)
        local_last = df_local[_dcol].iloc[-1]
        df_new = df_live[df_live[date_col] > local_last]
        added = len(df_new)
        if added > 0:
            # [B1-item1] 列对齐修复: df_live(fetch_day 9列/fetch_minute 7列) 比本地缓存(6列)多 amount 等列,
            # 直接追加会破坏 CSV(末尾行变 7/9 字段). 仅保留本地表头已有列, 按本地列顺序裁剪, 杜绝污染.
            _keep = [c for c in df_local.columns if c in df_new.columns]
            df_new = df_new[_keep]
            df_new.sort_values(date_col).to_csv(csv_path, mode='a', header=False, index=False)
    else:
        df_live_sorted = df_live.sort_values(date_col).drop_duplicates(subset=[date_col], keep="last")
        df_live_sorted.to_csv(csv_path, index=False)
        local_rows = 0
        added = len(df_live_sorted)
    return {"code": code, "tf": tf, "ok": True, "local_before": local_rows, "added": added}


def trim_cap(csv_path: Path, cap: int, date_col: str) -> int:
    """_1m.csv 追加后若超 cap 行，截取最后 cap 行（防爆盘）。返回截断行数。"""
    if not csv_path.exists():
        return 0
    df = pd.read_csv(csv_path, dtype={date_col: str})
    if len(df) <= cap:
        return 0
    df = df.sort_values(date_col).tail(cap)
    df.to_csv(csv_path, index=False)
    return len(df) - cap


def _stale_units(done_keys, max_lag_days=2):
    """B1 治本: 返回底层 K 线 last_bar 落后今天 > max_lag_days 的 (code,tf) 单元。

    用于每日增量 top-up 通道: 只对【落后】的已 done 单元补最新 bar,
    根除刷新断点缺陷导致的"done 即冻结"(done 单元在续跑周期内被整轮跳过, 不补新交易日)。
    正常交易日落后单元极少 → 低成本; 冻结单元会被持续补直到追上 today。
    仅本地读 CSV 找 last_bar, 不触发网络。
    """
    today = datetime.now(SH_TZ).date()
    stale = []
    for key in done_keys:
        code, tf = key.split("|")
        csv_path = KLINE_DIR / f"{code}_{tf}.csv"
        if not csv_path.exists():
            stale.append((code, tf)); continue
        try:
            dcol = DATE_COL[tf]
            d = pd.read_csv(csv_path, usecols=[dcol], dtype=str)[dcol].iloc[-1]
            last = datetime.strptime(str(d)[:10], "%Y-%m-%d").date()
            if (today - last).days > max_lag_days:
                stale.append((code, tf))
        except Exception:
            stale.append((code, tf))
    return stale


def get_universe_codes() -> list:
    if UNIVERSE_FILE.exists():
        codes = [l.strip() for l in UNIVERSE_FILE.read_text().splitlines() if l.strip()]
        return codes
    # fallback: 现有缓存推导（不应走到这里）
    codes = set()
    for p in ["*_day.csv", "*_5m.csv", "*_1m.csv", "*_30m.csv"]:
        for f in KLINE_DIR.glob(p):
            codes.add(f.stem.rsplit("_", 1)[0])
    return sorted(codes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", type=str, default=None, help="覆盖宇宙, 逗号分隔")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tf", type=str, default=",".join(ALL_TFS))
    parser.add_argument("--force", action="store_true", help="忽略交易日检查")
    parser.add_argument("--reset", action="store_true", help="清除断点, 强制开启新周期")
    parser.add_argument("--direction", type=str, default="asc", choices=["asc", "desc"],
                        help="遍历方向: asc升序(VM-A) / desc降序(VM-B), 双机相向会合")
    parser.add_argument("--coord-id", type=str, default=None, choices=["a", "b"],
                        help="双机协调ID: a=VM-A / b=VM-B, 启用时通过gdrive共享进度, 合计>=总数即停")
    args = parser.parse_args()

    tfs = [t.strip() for t in args.tf.split(",") if t.strip() in ALL_TFS]
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",")]
    else:
        codes = get_universe_codes()           # 全宇宙（_codes_mainboard.txt）
    if args.limit > 0:
        codes = codes[:args.limit]

    os.makedirs(KLINE_DIR, exist_ok=True)

    # ── flock 防重叠 ──
    lock_path = Path.home() / "kline_refresh.lock"
    lf = open(lock_path, "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print(f"[refresh] 上一次仍在跑, 跳过 ({lock_path})", flush=True)
        sys.exit(0)

    t0 = time.time()
    today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    if not args.force and not is_trade_day(today):
        print(f"[refresh] {today} 非交易日, 跳过 (--force 可强制)", flush=True)
        sys.exit(0)

    print(f"[refresh] {datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')} "
          f"direction={args.direction} codes={len(codes)} tfs={tfs} cap(1m)={ONE_MIN_CAP}", flush=True)

    grand_total = len(codes) * len(tfs)

    # ── 断点续跑: 仅默认全宇宙模式启用(不污染 --codes/--limit/--tf 冒烟) ──
    use_ckpt = (args.codes is None) and (args.limit == 0) and (args.tf == ",".join(ALL_TFS))
    if args.reset and CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        print(f"[refresh] --reset: 已清除断点 {CHECKPOINT_PATH}", flush=True)
    cp = _load_checkpoint() if use_ckpt else None
    done_keys = set()
    if cp is not None:
        valid_keys = {f"{c}|{t}" for c in codes for t in tfs}
        done_keys = set(cp.get("done", [])) & valid_keys   # 仅保留当前宇宙有效键(兼容 universe 变动)
        if len(done_keys) >= grand_total:
            # 上一周期已跑完 → 开启新周期(清空 done, 全量重刷)
            cp["cycle"] = cp.get("cycle", 0) + 1
            cp["done"] = []
            done_keys = set()
            _save_checkpoint(cp)
            print(f"[refresh] 周期 {cp['cycle']-1} 已完成, 开启新周期 {cp['cycle']} (全量重刷)", flush=True)
        else:
            print(f"[refresh] 续跑周期 {cp['cycle']}: 已完成 {len(done_keys)}/{grand_total} "
                  f"({len(done_keys)/grand_total*100:.0f}%), 跳过已完成单元", flush=True)

    todo_total = max(0, grand_total - len(done_keys))      # 本轮需实际取数单元数
    done, results = 0, []
    unsaved = 0                                            # BUG-C: 批量落盘计数, 避免每单元全量重写
    CKPT_SAVE_BATCH = 50                                   # 每完成 50 单元落盘一次(进程被杀最多丢 50 单元, 幂等重取)

    # ── A 项: 并发取数(默认 4 worker) ──
    # 扁平化待处理单元; 主线程【有序消费】future, 故 results/done_keys/unsaved 仅主线程改写 → 天然线程安全(无需锁)。
    # 2026-09-28 双机相向: desc 时逆序遍历, 与 VM-A asc 相向会合
    # 会合点动态漂移: 哪边限流慢, 另一边自然多跑, 并集>=总数即完成
    ordered_codes = codes if args.direction == "asc" else list(reversed(codes))
    pending = [(c, t) for c in ordered_codes for t in tfs if f"{c}|{t}" not in done_keys]

    def _task(code, tf):
        """worker 内: 取数 + 写盘 + 1m 截尾 + per-request 节流。per-(code,tf) 独立文件, 无写冲突。"""
        r = append_only(code, tf)
        if r.get("ok") and tf == "1m":
            try:
                trimmed = trim_cap(KLINE_DIR / f"{code}_1m.csv", ONE_MIN_CAP, DATE_COL["1m"])
                if trimmed:
                    r["trimmed"] = trimmed
            except Exception as e:
                print(f"  [WARN] trim_cap failed {code}/1m: {e}", file=sys.stderr)
        time.sleep(API_SLEEP)
        return r

    print(f"[refresh] 并发 worker={MAX_WORKERS} 全局最小提交间隔={MIN_SUBMIT_INTERVAL}s "
          f"(QPS 上限≈{1/MIN_SUBMIT_INTERVAL:.1f}/s)", flush=True)
    CHUNK = MAX_WORKERS * 8                                # 在途 future 上限: 防内存堆积 + 保 checkpoint 及时落盘
    _last_submit = [0.0]
    coord_chunk_n = [0]
    with _cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        for i in range(0, len(pending), CHUNK):
            # ── 双机协调: 每5个chunk发布进度 ──
            if args.coord_id and coord_chunk_n[0] % 5 == 0:
                _coord_publish(args.coord_id, done_keys, grand_total)
            coord_chunk_n[0] += 1
            chunk = pending[i:i + CHUNK]
            futs = []
            for c, t in chunk:
                gap = time.time() - _last_submit[0]
                if gap < MIN_SUBMIT_INTERVAL:              # 全局限速: 提交间隔兜底 QPS 上限
                    time.sleep(MIN_SUBMIT_INTERVAL - gap)
                _last_submit[0] = time.time()
                futs.append(ex.submit(_task, c, t))
            for (code, tf), fu in zip(chunk, futs):        # 有序消费, 保 checkpoint/统计顺序稳定
                key = f"{code}|{tf}"
                try:
                    r = fu.result()
                except Exception as e:
                    print(f"  [WARN] task failed {key}: {e}", file=sys.stderr)
                    r = {"ok": False}
                if r.get("ok"):
                    results.append(r)
                    done_keys.add(key)
                    if cp is not None:
                        unsaved += 1
                        if unsaved >= CKPT_SAVE_BATCH:      # BUG-C 修复: 批量落盘
                            cp["done"] = list(done_keys)
                            _save_checkpoint(cp)
                            unsaved = 0
                done += 1
                if todo_total and (done % 200 == 0 or done == todo_total):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (todo_total - done) / rate if rate > 0 else 0
                    print(f"[refresh] 续跑 {done}/{todo_total} 实取 ({done/todo_total*100:.0f}%) "
                          f"rate={rate:.1f}/s eta={eta/60:.1f}min "
                          f"(累计 {len(done_keys)}/{grand_total})", flush=True)

    # BUG-C 修复: 循环结束后兜底 flush 剩余未落盘单元(含全量完成态)
    # ── B1 治本: 每日增量 top-up 通道 — 已 done 但底层 K 线落后>max_lag_days 的单元补最新 bar ──
    # 根除"done 即冻结"(刷新断点缺陷: done 单元在续跑周期内被整轮跳过, 不补新交易日)。
    # 仅对落后单元取数(网络重), 正常日落后单元极少 → 低成本; 冻结单元持续补至追上 today。
    # 不写 done_keys(本已是 done), top-up 失败仅 WARN, 不影响全量回填续跑状态。
    # 由 universe-kline-sync.timer(每日15:35)自动触发 → 冻结单元每日被补齐。
    if cp is not None and done_keys and is_trade_day(today):
        # 2026-10-13: B1 top-up default skip, user wants daily incremental only
        MAX_LAG_DAYS = int(os.environ.get("KLINE_TOPUP_LAG", "2"))
        if os.environ.get("KLINE_TOPUP", "0") != "1":
            print("[refresh] B1 top-up skipped", flush=True)
            stale = []
        else:
            stale = _stale_units(done_keys, MAX_LAG_DAYS)
        if stale:
            print(f"[refresh] B1 top-up: {len(stale)} 个落后单元(last_bar落后>{MAX_LAG_DAYS}天)补最新bar",
                  flush=True)
            with _cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
                futs = [ex.submit(_task, c, t) for (c, t) in stale]
                for (code, tf), fu in zip(stale, futs):
                    try:
                        r = fu.result()
                    except Exception as e:
                        r = {"ok": False, "error": str(e)[:80]}
                    if not r.get("ok"):
                        print(f"  [WARN] B1 top-up failed {code}/{tf}: {r.get('error')}", file=sys.stderr)
            print(f"[refresh] B1 top-up 完成 — {len(stale)} 单元已尝试补最新bar", flush=True)
        else:
            print(f"[refresh] B1 top-up: 无落后单元(last_bar均在{MAX_LAG_DAYS}天内)", flush=True)

    if cp is not None and unsaved > 0:
        cp["done"] = list(done_keys)
        _save_checkpoint(cp)

    # ── 真·互助: 自己跑完后，认领对方剩余 ──
    if args.coord_id and use_ckpt:
        all_keys = {f"{c}|{t}" for c in codes for t in tfs}
        for _round in range(3):  # 最多3轮认领，避免无限循环
            _coord_publish(args.coord_id, done_keys, grand_total)
            claimable = _coord_get_other_remaining(args.coord_id, done_keys, all_keys)
            if not claimable:
                print("[coord] 对方无剩余可认领，退出", flush=True)
                break
            print(f"[coord] 认领对方剩余 {len(claimable)} 项，开始帮跑", flush=True)
            # 帮跑逻辑：复用 _task，但用认领的 key
            help_pending = []
            for k in claimable:
                c, t = k.split("|")
                help_pending.append((c, t))
            with _cf.ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex2:
                for i in range(0, len(help_pending), CHUNK):
                    chunk = help_pending[i:i + CHUNK]
                    futs = [ex2.submit(_task, c, t) for c, t in chunk]
                    for (code, tf), fu in zip(chunk, futs):
                        key = f"{code}|{tf}"
                        try:
                            r = fu.result()
                        except Exception:
                            r = {"ok": False}
                        if r.get("ok"):
                            done_keys.add(key)
                            if cp is not None:
                                cp["done"] = list(done_keys)
                                _save_checkpoint(cp)
                    _coord_publish(args.coord_id, done_keys, grand_total)
            print(f"[coord] 本轮帮跑完成，已做={len(done_keys)}/{grand_total}", flush=True)

    elapsed = time.time() - t0
    by_tf = {}
    for r in results:
        s = by_tf.setdefault(r["tf"], {"ok": 0, "err": 0, "added": 0, "trim": 0})
        if r["ok"]:
            s["ok"] += 1; s["added"] += r["added"]; s["trim"] += r.get("trimmed", 0)
        else:
            s["err"] += 1
    print(f"\n{'='*60}\n  本轮回跑完成 — {elapsed:.0f}s  实取 {done}/{todo_total}\n{'='*60}")
    for tf in tfs:
        s = by_tf.get(tf, {})
        print(f"  {tf:4s}: {s.get('ok',0)} OK  {s.get('err',0)} ERR  "
              f"+{s.get('added',0)} 截尾{s.get('trim',0)}")
    if cp is not None:
        print(f"  周期 {cp['cycle']} 累计进度: {len(done_keys)}/{grand_total} "
              f"({len(done_keys)/grand_total*100:.0f}%)  "
              f"{'✅ 全量完成' if len(done_keys) >= grand_total else '⏳ 待续跑'}")
    print(f"  ✅ 幂等 | 1m 封顶 {ONE_MIN_CAP} 防爆盘")


if __name__ == "__main__":
    main()
