#!/usr/bin/env python3
"""generate_plan_md — 盘后自动生成次日交易计划 md
输入: chan-merge 的 merged_*.json + v3_positions_*.json + AKShare T-1 情绪数据
      + dualscan_*.json (回填 price/ratio/dlp)
输出: ~/chan_logs/tomorrow_plan_YYYY-MM-DD.md
"""
import os, sys, json, glob, time, argparse, traceback, re
import socket
socket.setdefaulttimeout(30)  # 防御 akshare/东方财富接口无超时挂死 (2026-09-16)
from datetime import datetime, timedelta


def parse_merged(path):
    if not path or not os.path.exists(os.path.expanduser(path)):
        return {"passes": [], "blocks": [], "meta": {}}
    d = json.load(open(os.path.expanduser(path)))
    passes = d.get("pass_candidates") or []
    blocks = d.get("blocked_observations") or []
    if not passes and not blocks:
        for c in d.get("candidates", []) or []:
            gate = (c.get("gate") or c.get("status") or "").upper()
            if "PASS" in gate:
                passes.append(c)
            else:
                blocks.append(c)
    return {"passes": passes, "blocks": blocks, "watch": d.get("watch_candidates") or [], "meta": d.get("meta", {})}


def _merged_scan_date(meta, merged_path):
    """从 merged 推断其扫描日 (YYYYMMDD)，推断不出返回 None。
    1) meta.source 形如 .../combined_scan_ledger_20260925.jsonl → 20260925
    2) 文件名 merged_2026-09-26.json (次日+横杠命名制) → 减一天 → 20260925
    2026-09-27 教训: --date=20260926 却用了 9/25 扫描的 merge，导致计划
    股票池实为旧数据却标注新扫描日 ("陈旧数据新鲜标")。"""
    src = str((meta or {}).get("source") or "")
    m = re.search(r"combined_scan_ledger_(\d{8})", src)
    if m:
        return m.group(1)
    base = os.path.basename(merged_path or "")
    m = re.search(r"merged_(\d{4})-(\d{2})-(\d{2})", base)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))) - timedelta(days=1)
            return d.strftime("%Y%m%d")
        except ValueError:
            return None
    return None


def parse_v3(path):
    if not path or not os.path.exists(os.path.expanduser(path)):
        return {}
    return json.load(open(os.path.expanduser(path)))


def load_dualscan_signals(log_dir="~/chan_logs", max_age_hours=24):
    """从 dualscan_*.json 报告回填 code -> {price, ratio, dlp, valid, confirmed, near}。
    dual2_scan.py 把完整信号写进这些报告, 但账本只留 code/gate/vm/ts, 故需回填。"""
    sig = {}
    d = os.path.expanduser(log_dir)
    if not os.path.isdir(d):
        return sig
    cutoff = time.time() - max_age_hours * 3600
    files = sorted(glob.glob(os.path.join(d, "dualscan_*.json")),
                   key=lambda p: os.path.getmtime(p), reverse=True)
    scanned = 0
    MAX_FILES = 400          # 上限: 防历史报告堆积拖慢 (超出即已覆盖全量)
    MAX_BYTES = 64 * 1024 * 1024   # 单文件 64MB 上限, 防读入巨型报告
    for fp in files:
        if scanned >= MAX_FILES:
            break
        try:
            if os.path.getmtime(fp) < cutoff:
                continue
            if os.path.getsize(fp) > MAX_BYTES:
                continue
            rep = json.load(open(fp))
        except Exception:
            continue
        scanned += 1
        for r in rep.get("dual", []) or []:
            code = r.get("code")
            if not code:
                continue
            # 新的覆盖旧的 (files 已按 mtime 降序, 首次写入即最新)
            if code not in sig:
                sig[code] = {
                    "price": r.get("price"),
                    "ratio": r.get("ratio"),
                    "dlp": r.get("dlp"),
                    "valid": r.get("valid"),
                    "confirmed": r.get("confirmed"),
                    "near": r.get("near"),
                    "name": r.get("name", ""),
                }
    return sig


def _trade_calendar():
    """返回已过去的交易日列表 (['YYYYMMDD', ...] 升序); 失败返回 None。"""
    try:
        import akshare as ak
        import pandas as pd
        cal = ak.tool_trade_date_hist_sina()
        if cal is None or len(cal) == 0:
            return None
        col = "trade_date" if "trade_date" in cal.columns else cal.columns[0]
        days = pd.to_datetime(cal[col]).dt.strftime("%Y%m%d").tolist()
        days = sorted(set(days))
        return days
    except Exception:
        return None


def _prev_trading_day(d):
    """回退到最近交易日: 优先交易日历(覆盖法定节假日), 否则仅按周末回退。

    修复: 原逻辑 `today - 1 天`, 当 today 为周一时会取到周日(非交易日),
    导致涨停池/炸板池/龙虎榜全部为空 → 计划里 "涨停 0 炸板 0"。
    """
    cal = _trade_calendar()
    if cal:
        ds = d.strftime("%Y%m%d")
        past = [x for x in cal if x < ds]
        if past:
            from datetime import datetime as _dt
            return _dt.strptime(past[-1], "%Y%m%d")
    wd = d.weekday()
    if wd == 0:      # 周一 → 上周五
        return d - timedelta(days=3)
    if wd == 6:      # 周日 → 周五
        return d - timedelta(days=2)
    if wd == 5:      # 周六 → 周五
        return d - timedelta(days=1)
    return d - timedelta(days=1)

def fetch_sentiment(max_backtrack=15):
    """拉 T-1 情绪数据; 命中非交易日(数据全空)时自动往前回退。

    判定非交易日: 涨停池 <=0 且 炸板池 <=0 (含拉取异常)。
    """
    # [PATCH] 优先读本地情绪缓存 (akshare 实时拉取挂死时的兜底); 无缓存/缓存过期才走 akshare
    # 2026-09-27 修复: 缓存无新鲜度检查, 9/22 之后所有计划都用了 9/21 的情绪数据。
    #   1) 缓存 t1 < 预期 T-1 → 视为过期, 走 akshare 重拉
    #   2) akshare 拉成功后回写缓存 (见函数末尾)
    import os as _os, json as _json
    _cache = _os.path.expanduser("~/chan_logs/sentiment_cache.json")
    _expected_t1 = _prev_trading_day(datetime.now()).strftime("%Y%m%d")
    if _os.path.exists(_cache):
        try:
            _c = _json.load(open(_cache, encoding="utf-8"))
            if _c.get("t1", "") >= _expected_t1:
                _c["ok"] = True
                _c["cached"] = True
                return _c
            # 缓存过期 → 落到 akshare 重拉
        except Exception:
            pass
    s = {"ok": False}
    try:
        import akshare as ak
        d = _prev_trading_day(datetime.now())
        tried = []
        hit = False
        for _ in range(max_backtrack):
            t1 = d.strftime("%Y%m%d")
            tried.append(t1)
            zt_n = zb_n = -1
            try:
                zt = ak.stock_zt_pool_em(date=t1)
                zt_n = len(zt) if zt is not None else -1
            except Exception as e:
                s.setdefault("warn", []).append("zt@%s: %s" % (t1, str(e)[:60]))
            try:
                zb = ak.stock_zt_pool_zbgc_em(date=t1)
                zb_n = len(zb) if zb is not None else -1
            except Exception as e:
                s.setdefault("warn", []).append("zb@%s: %s" % (t1, str(e)[:60]))
            if zt_n <= 0 and zb_n <= 0:
                d = _prev_trading_day(d)      # 非交易日/接口空, 继续回退
                continue
            s["t1"] = t1
            s["zt"] = zt_n if zt_n > 0 else 0
            s["zb"] = zb_n if zb_n > 0 else 0
            hit = True
            break
        if not hit:
            s["t1"] = tried[-1] if tried else "?"
            s["zt"] = 0
            s["zb"] = 0
            s["err"] = "回退 %d 天仍无情绪数据 (试过 %s)" % (
                max_backtrack, ",".join(tried))
        # 龙虎榜: 防御 None/空表 (akshare 对空响应会做下标 →
        # 'NoneType' object is not subscriptable)
        try:
            lhb = ak.stock_lhb_detail_em(start_date=s["t1"], end_date=s["t1"])
            if lhb is None or len(lhb) == 0:
                s["lhb"] = 0
            else:
                s["lhb"] = len(lhb)
                col = "代码" if "代码" in lhb.columns else (
                    lhb.columns[1] if lhb.shape[1] > 1 else None)
                if col is not None:
                    s["lhb_codes"] = (lhb[col].astype(str).str.zfill(6)
                                      .unique().tolist()[:30])
        except Exception as e:
            s["lhb"] = "?(%s: %s)" % (type(e).__name__, str(e)[:60])
        s["tried"] = tried
        s["ok"] = True
        # 2026-09-27: 拉成功后回写缓存, 避免下次用过期数据
        try:
            _w = {k: s.get(k) for k in ("t1", "zt", "zb", "lhb", "lhb_codes")}
            _w["ok"] = True
            _tmp = _cache + ".tmp"
            _json.dump(_w, open(_tmp, "w", encoding="utf-8"),
                       ensure_ascii=False)
            _os.replace(_tmp, _cache)
        except Exception:
            pass
    except Exception as e:
        s["err"] = str(e)
    return s

def fmt_pct(x):
    try:
        return f"{float(x):.4f}"
    except Exception:
        return "-"


# ─────────────────────────────────────────────────────────────────────────────
# 2026-09-23 WORKBUDDY: DL_P 可采信性标注 (低风险, 仅影响显示)
#
# 判据基于【结构性】的量比 ratio, 而非日间不稳定的 dl_prob 本身:
#   模型的有效训练分布为 [10,150]; ratio 触及/超出上界说明 pre 段 MACD
#   方向性面积退化(趋零)或量比越界, 此时 dl_prob 已被固定门控 ×0.08 压成
#   饱和值(约 0.08) —— 它是模型输出的确定性地板值, 不含任何判别信息,
#   不可作为强弱依据(实证: 该类标的重算前后 dlp 分毫不差, Δ=0.0000)。
#
# 本改动只标注、不隐藏: 数值保留以便审计, 且不改变任何标的的入选与排序。
# ─────────────────────────────────────────────────────────────────────────────
RATIO_VALID_MAX = 150.0


def _ratio_out_of_range(c):
    """ratio 是否触及/超出模型训练有效上界 -> 该记录的 DL_P 不可采信."""
    r = c.get("ratio")
    if r is None:
        return False
    try:
        return float(r) >= RATIO_VALID_MAX
    except (TypeError, ValueError):
        return False


def fmt_dlp_cell(c):
    """渲染 DL_P 单元格: 越界时加注不可采信标记(保留数值, 不隐藏)."""
    v = c.get("dlp")
    if v is None:
        return "—"
    cell = fmt_pct(v)
    return f"{cell} ⚠不可采信" if _ratio_out_of_range(c) else cell


def fmt_ratio_cell(c):
    """渲染 ratio 单元格: 越界时加注标记."""
    v = c.get("ratio")
    if v is None:
        return "—"
    cell = fmt_pct(v)
    return f"{cell} ⚠越界" if _ratio_out_of_range(c) else cell


def fmt_price(x):
    try:
        return f"{float(x):.2f}"
    except Exception:
        return "-"

# K线日线缓存兜底价源 (仅取 >= 阈值 的新鲜收盘, 避免填入过期价)
# P1-11 修复 (2026-10-01): 原写死绝对日期 "2026-09-21"，随时间推移会静默接受
# 越来越陈旧的价格。改为相对口径：K线日期 >= 扫描日 - 10 天（覆盖国庆级长假）。
_KLINE_FRESH_LOOKBACK_DAYS = 10

def _fresh_cutoff(scan_d8):
    try:
        d = datetime.strptime(scan_d8, "%Y%m%d") - timedelta(days=_KLINE_FRESH_LOOKBACK_DAYS)
        return d.strftime("%Y-%m-%d")
    except Exception:
        return "2000-01-01"

def _kline_close(code):
    """从 ~/TradingAgents-CN/kline_cache/<code>_day.csv 取最新收盘。
    仅当最新日期 >= _KLINE_FRESH_CUTOFF 才返回, 否则返回 None (避免过期价)。"""
    try:
        import os as _os
        kp = _os.path.expanduser(
            f"~/TradingAgents-CN/kline_cache/{code}_day.csv")
        if not _os.path.exists(kp):
            return None
        rows = [ln for ln in open(kp).read().strip().splitlines()
                if ln and not ln.startswith("date,")]
        if not rows:
            return None
        parts = rows[-1].split(",")
        if len(parts) < 5:
            return None
        return {"close": float(parts[4]), "date": parts[0]}
    except Exception:
        return None


# ---- 并发批量预读: 消除回填阶段逐个串行 open FUSE CSV 的瓶颈 ----
import threading as _thr
_KLINE_CACHE = {}
_KLINE_CACHE_READY = False
_KLINE_CACHE_LOCK = _thr.Lock()

def _load_kline_cache(codes):
    """并发预读目标 codes 的最新日线收盘到内存, 仅保留 >= 阈值 的。"""
    global _KLINE_CACHE, _KLINE_CACHE_READY
    if _KLINE_CACHE_READY:
        return
    import os as _os, glob as _glob
    from concurrent.futures import ThreadPoolExecutor
    kdir = _os.path.expanduser("~/TradingAgents-CN/kline_cache")
    want = set(codes)
    targets = []
    try:
        for fp in _glob.glob(_os.path.join(kdir, "*_day.csv")):
            base = _os.path.basename(fp)
            if base.endswith("_day.csv"):
                code = base[:-len("_day.csv")]
                if code in want:
                    targets.append((code, fp))
    except Exception:
        pass
    def _read(item):
        code, fp = item
        try:
            rows = [ln for ln in open(fp, encoding="utf-8").read().splitlines()
                    if ln and not ln.startswith("date,")]
            if rows:
                parts = rows[-1].split(",")
                if len(parts) >= 5:
                    return code, {"close": float(parts[4]), "date": parts[0]}
        except Exception:
            return code, None
        return code, None
    local = {}
    try:
        with ThreadPoolExecutor(max_workers=8) as ex:
            for code, res in ex.map(_read, targets):
                if res:
                    local[code] = res
    except Exception:
        pass
    _KLINE_CACHE.update(local)
    _KLINE_CACHE_READY = True

def kline_close_cached(code):
    """查预读缓存; 缓存已就绪但未命中即 None。"""
    return _KLINE_CACHE.get(code)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None)
    ap.add_argument("--merged", default=None)
    ap.add_argument("--v3", default=None)
    ap.add_argument("--out-dir", default="~/chan_logs")
    ap.add_argument("--log-dir", default="~/chan_logs")
    ap.add_argument("--allow-stale-merged", action="store_true",
                    help="显式允许 --merged 的扫描日与 --date 不一致 (如周末用旧数据兜底)。"
                         "不传则不一致时拒绝生成并 exit 4。")
    args = ap.parse_args()

    date_str = (args.date or "").strip() or datetime.now().strftime("%Y%m%d")
    try:
        _scan_d = datetime.strptime(date_str, "%Y%m%d")
    except (ValueError, TypeError):
        # 坏日期（如 shell 吞成 "+%Y%m%d)"）一律回退今天，避免崩溃或产出错误日期的空计划
        _scan_d = datetime.now()
        date_str = _scan_d.strftime("%Y%m%d")
        print(f"[plan] 警告: --date 解析失败, 回退到今天 {date_str}", flush=True)
    plan_date = (_scan_d + timedelta(days=1)).strftime("%Y-%m-%d")
    merged_path = args.merged or os.path.expanduser(f"~/chan_logs/merged_{date_str}.json")
    v3_path = args.v3 or os.path.expanduser(f"~/chan_logs/v3_positions_{date_str}.json")

    print(f"[plan] date={date_str} merged={merged_path}", flush=True)
    merged = parse_merged(merged_path)

    # ---- merged/--date 一致性校验: 防"陈旧数据新鲜标" ----
    # 正常链路 (regen_plan.sh) 传 --date <扫描日> --merged merged_<次日>.json，
    # merged 的 meta.source 即 combined_scan_ledger_<扫描日>.jsonl，必然一致。
    # 2026-09-27 实测: 旁路用 9/25 的 merge 却标 scan=20260926 出了计划。
    _merged_scan = _merged_scan_date(merged.get("meta") or {}, merged_path)
    if _merged_scan and _merged_scan != date_str:
        print(f"[plan] ❌ 拒绝生成: --date={date_str} 与 merged 扫描日={_merged_scan} "
              f"不一致 (merged={merged_path})", flush=True)
        if not args.allow_stale_merged:
            print("[plan] 如确需用旧数据 (如周末兜底), 请显式传 --allow-stale-merged", flush=True)
            sys.exit(4)
        print("[plan] ⚠ --allow-stale-merged 已显式指定, 继续生成 (注意: 股票池为旧扫描日数据!)", flush=True)
    elif not _merged_scan:
        print(f"[plan] ⚠ 无法从 merged 推断扫描日 (merged={merged_path}), 跳过日期一致性校验", flush=True)

    v3 = parse_v3(v3_path)
    sigmap = load_dualscan_signals(args.log_dir)
    print(f"[plan] dualscan signals loaded: {len(sigmap)} codes", flush=True)
    sent = fetch_sentiment()

    passes = merged["passes"]
    blocks = merged["blocks"]
    watch = merged["watch"]
    meta = merged["meta"]
    total = len(passes) + len(blocks) + len(watch)

    # 预读 K线兜底价 (并发批量, 仅针对 nan 价 code) — 压缩回填耗时
    _nan_codes = [c.get("code") or c.get("symbol") for c in passes + blocks
                  if (c.get("price") is None
                      or (isinstance(c.get("price"), float) and c.get("price") != c.get("price")))]
    _load_kline_cache(_nan_codes)

    # 回填信号
    enriched = 0
    cache_filled = 0
    for c in passes + blocks:
        code = c.get("code") or c.get("symbol")
        if code and code in sigmap:
            for k, v in sigmap[code].items():
                if c.get(k) in (None, "") and v not in (None, ""):
                    c[k] = v
            enriched += 1
        # 兜底: dualscan 无价时从 K线日线缓存最新收盘回填 (仅取 >= 阈值 的新鲜数据)
        if code:
            p = c.get("price")
            if p is None or (isinstance(p, float) and p != p):
                kb = kline_close_cached(code)
                if kb and kb["date"] >= _fresh_cutoff(date_str):
                    c["price"] = kb["close"]
                    c["price_src"] = "cache"
                    cache_filled += 1
    price_missing = sum(
        1 for c in passes + blocks
        if c.get("price") is None
        or (isinstance(c.get("price"), float) and c.get("price") != c.get("price")))

    lines = [
        f"# 次日交易计划 ({plan_date})", "",
        f"> 生成: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | scan={date_str} | "
        f"PASS={len(passes)} BLOCKED={len(blocks)} | 信号回填={enriched}",
        "", "",
        "## 一、扫描概览", "",
        "| 指标 | 数值 |", "|------|------|",
        f"| PASS 候选 | {len(passes)} 只 |",
        f"| BLOCKED 观察 | {len(blocks)} 只 |",
        f"| 合计过双引擎 | {total} 只 |",
        f"| 信号已回填 (price/DL_P) | {enriched} 只 |",
        f"| 缓存兜底价 (≈, ≥09-21) | {cache_filled} 只 |",
        f"| 价格仍缺失 | {price_missing} 只 |",
        f"| 观察池(PASS_JEVN+NEUTRAL) | {len(watch)} 只 |",
        "", "",
        "## 二、T-1 情绪", "",
    ]
    if sent.get("ok"):
        lines += [
            f"T-1 = {sent.get('t1', '?')}", "",
            "| 指标 | 数值 |", "|------|------|",
            f"| 涨停数 | {sent.get('zt', '?')} |",
            f"| 炸板数 | {sent.get('zb', '?')} |",
            f"| 龙虎榜上榜 | {sent.get('lhb', '?')} |",
        ]
        lhb_codes = sent.get("lhb_codes", [])
        if lhb_codes:
            lines += ["", f"**龙虎榜:** {', '.join(lhb_codes[:20])}"]
    else:
        lines += [f"> ⚠ 情绪拉取失败: {sent.get('err')}"]

    lines += ["", "", "## 三、PASS 候选池 (按 DL_P 降序)", ""]
    ordered = []
    n_unreliable = 0
    if passes:
        # 有 DL_P 的按 DL_P 降序排前, 无 DL_P 的按时间排后
        with_dlp = [c for c in passes if c.get("dlp") is not None]
        without = [c for c in passes if c.get("dlp") is None]
        with_dlp.sort(key=lambda c: (c.get("dlp") or 0), reverse=True)
        without.sort(key=lambda c: c.get("ts", ""))
        ordered = with_dlp + without

        lines += ["| # | 代码 | 名称 | 价格 | DL_P | ratio | 判定 | VM |",
                  "|---|------|------|------|------|-------|------|----|"]
        for i, c in enumerate(ordered[:40], 1):
            code = c.get("code", c.get("symbol", "?"))
            name = (c.get("name") or "")[:6]
            price = ("\u2248" if c.get("price_src") == "cache" else "") + fmt_price(c.get("price"))
            dlp = fmt_dlp_cell(c)
            ratio = fmt_ratio_cell(c)
            if c.get("confirmed"):
                tag = "confirmed"
            elif c.get("near"):
                tag = "near"
            else:
                tag = "PASS"
            lines.append(f"| {i} | {code} | {name} | {price} | {dlp} | {ratio} | {tag} | {c.get('vm', '?')} |")
        if len(ordered) > 40:
            lines += [f"\n> ... 共 {len(ordered)} 只 (+{len(ordered) - 40} 更多)"]
        # 必须统计全量而非仅前 40: 实测越界票因 DL_P 极低都沉在 #42 之后,
        # 只看前 40 会得 0、导致整段标注形同虚设(曾实际踩到)。
        n_unreliable = sum(1 for c in ordered if _ratio_out_of_range(c))
        if n_unreliable:
            lines += ["", f"> ⚠ 全池 {n_unreliable} 只 ratio ≥ {RATIO_VALID_MAX:g}, "
                      f"超出背驰模型训练有效上界 [10, {RATIO_VALID_MAX:g}] —— 其 DL_P "
                      f"为固定门控压出的饱和地板值, 不含判别信息（数值保留仅为可审计, "
                      f"不影响入选与排序）。因 DL_P 极低它们沉在队列末段, 通常不在上表前 40 行内, "
                      f"完整清单见「三之一、DL_P 不可采信批次」。"]
        if not with_dlp:
            lines += ["", "> ⚠ 无 DL_P 数据 — dualscan 报告可能缺失或超过 24h"]
    else:
        lines += ["> 今日无 PASS 候选 — 休息或只做持仓调仓"]

    # ── 2026-09-23 WORKBUDDY: DL_P 不可采信批次 ────────────────────────────────
    # 目的: 把"这个 DL_P 数值没有信息"显式暴露出来。
    # 背景: ratio ≥ 150(模型训练上界)时, pre 段 MACD 方向性面积趋零, dl_prob 被 ×0.08
    #       固定门控压成饱和地板值 —— 实测该簇全部落在 0.0799x~0.0800 窄带、重算前后
    #       Δ=0.0000, 簇内彼此不可区分, 按 DL_P 排序属纯噪声。
    #       它们因 DL_P 极低沉在主表前 40 行之外, 故单列成节以保证可见。
    # 边界: 仅显示标注, 不改变入选与排序; 数值保留以便审计。
    if n_unreliable:
        bad = [c for c in ordered if _ratio_out_of_range(c)]
        lines += ["", "", "## 三之一、DL_P 不可采信批次", "",
                  f"> ⚠ 下列 {n_unreliable} 只 **ratio ≥ {RATIO_VALID_MAX:g}**, "
                  f"超出背驰模型训练有效上界 [10, {RATIO_VALID_MAX:g}]。", "",
                  "> **成因**: pre 段 MACD 方向性面积趋零（或量比突破有效量程）, 使 "
                  "`post_a / pre_a * 100` 失去意义; 经固定门控 `×0.08` 后 dl_prob 塌缩为饱和地板值。", "",
                  "> **实证**: 该簇 DL_P 全部挤在 0.0799x~0.0800 窄带, 且重算前后分毫不差（Δ=0.0000）。", "",
                  "> **后果**: 簇内 DL_P 彼此不可区分 —— 按 DL_P 排序在这一段属**纯噪声**, "
                  "不可作为强弱依据。它们因 DL_P 极低自然沉到队列末段, 不出现在主表前 40 行。", "",
                  "> 本节仅揭示不可靠性, **不改变任何标的的入选与排序**; 数值保留仅为可审计。", "",
                  "| # | 代码 | 名称 | 价格 | DL_P | ratio | VM |",
                  "|---|------|------|------|------|-------|----|"]
        for i, c in enumerate(bad[:40], 1):
            lines.append(
                f"| {i} | {c.get('code', '?')} | {(c.get('name') or '')[:6]} | "
                f"{fmt_price(c.get('price'))} | {fmt_dlp_cell(c)} | "
                f"{fmt_ratio_cell(c)} | {c.get('vm', '?')} |")
        if len(bad) > 40:
            lines += [f"\n> ... 共 {n_unreliable} 只 (+{len(bad) - 40} 更多)"]

    if blocks:
        lines += ["", "", "## 四、BLOCKED 观察池 (前 20)", "",
                  "| 代码 | 名称 | 价格 | DL_P | gate | VM |",
                  "|------|------|------|------|------|----|"]
        for c in blocks[:20]:
            lines.append(
                f"| {c.get('code', '?')} | {(c.get('name') or '')[:6]} | "
                f"{fmt_price(c.get('price'))} | "
                f"{fmt_dlp_cell(c)} | "
                f"{c.get('gate', 'BLOCKED')} | {c.get('vm', '?')} |")

    # F3 (2026-09-23 WORKBUDDY): 观察池章节 — 双引擎判 PASS/中性但被 JEV 降级或判中性的标的,
    # 列入观察而非丢弃, 解决 402 只静默消失问题.
    if watch:
        lines += ["", "", "## 四之一、观察池 (PASS_JEVN + NEUTRAL, 前 20)", "",
                  "> 这些标的双引擎判 PASS 或中性, 但被 JEV 二次门禁降级(FAIL)或判中性(NEUTRAL) — "
                  "不进 PASS 主池, 也不再丢弃, 纳入观察.", "",
                  "| 代码 | 名称 | 价格 | DL_P | gate | VM |",
                  "|------|------|------|------|------|----|"]
        for c in watch[:20]:
            lines.append(
                f"| {c.get('code', '?')} | {(c.get('name') or '')[:6]} | "
                f"{fmt_price(c.get('price'))} | "
                f"{fmt_dlp_cell(c)} | "
                f"{c.get('gate', '?')} | {c.get('vm', '?')} |")

    lines += [
        "", "", "## 五、次日交易计划", "",
        "### 执行要点", "",
        "- **入场**: 9:30 后 1min 放量突破前高 → 分批试仓",
        "- **止损**: 入场 K 低点, 或 -2.5%",
        "- **止盈**: +5% 减半, +10% 清仓",
        "- **过滤**: 涨停高开 >5% 不追, 缩量不进",
        "- **单票 ≤15%, 总仓 ≤70%**",
    ]
    if v3:
        lines += ["", "", "### v3 持仓建议"]
        for key in ("keep", "sell", "buy", "reduce", "add"):
            items = v3.get(key) or []
            if items:
                lines += ["", f"**{key.upper()}** ({len(items)})", "",
                          "| 代码 | 操作 | 理由 |", "|------|------|------|"]
                for it in items[:15]:
                    lines.append(f"| {it.get('code', '?')} | {it.get('action', key)} | "
                                 f"{str(it.get('reason', ''))[:60]} |")

    out_dir = os.path.expanduser(args.out_dir)
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"tomorrow_plan_{plan_date}.md")
    with open(out_path, "w") as f:
        f.write("\n".join(lines))

    summary = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "plan_date": plan_date, "scan_date": date_str,
        "pass_count": len(passes), "block_count": len(blocks),
        "watch_count": len(watch),
        "enriched": enriched,
        "pass_codes": [c.get("code", "?") for c in passes],
        "sentiment": {k: v for k, v in sent.items() if k != "ok"},
    }
    json_path = os.path.join(out_dir, f"plan_summary_{plan_date}.json")
    with open(json_path, "w") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    try:
        proj_dir = os.path.expanduser("~/TradingAgents-CN/scripts/chanlun-workflow")
        proj_path = os.path.join(proj_dir, f"明日计划_{plan_date}.md")
        with open(proj_path, "w") as f:
            f.write("\n".join(lines))
    except Exception:
        pass

    print(f"[plan] ✅ {len(passes)} PASS / {len(blocks)} BLOCKED / "
          f"enriched={enriched} → {out_path}", flush=True)


if __name__ == "__main__":
    main()
