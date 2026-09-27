#!/usr/bin/env python3
"""
incremental_kline_sync.py — 1970 根治方案: append-only 增量同步

核心:
  - NEVER overwrite existing CSV. 只追加新 bar (mode='a').
  - 幂等: live.date > local_last_date → 已经存在的不追加.
  - 非交易日跳过 (省流量和时间).
  - 数据源: day=ak.stock_zh_a_daily (+baostock fallback), minute=ak.stock_zh_a_minute.

用法:
  python incremental_kline_sync.py                      # 全部 2545 只
  python incremental_kline_sync.py --codes 603650,600519
  python incremental_kline_sync.py --limit 100
  python incremental_kline_sync.py --force              # 忽略交易日检查
"""
import os, sys, time, argparse
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd
import akshare as ak

SH_TZ = timezone(timedelta(hours=8))
KLINE_DIR = Path(os.environ.get(
    "KLINE_CACHE_DIR",
    "/home/gorgesoros39/gdrive/TradingAgents-CN/kline_cache"
))

DATE_COL   = {"day": "date", "1m": "day", "5m": "day", "30m": "day"}
MIN_PERIOD = {"1m": "1", "5m": "5", "30m": "30"}
ALL_TFS    = ["day", "5m", "1m", "30m"]
API_SLEEP  = 0.10

# 交易日历 (本地缓存, 第一次拉后不重复请求)
_TRADE_DATES_CACHE = None


def is_trade_day(today: str = None) -> bool:
    """判断今天是不是 A 股交易日."""
    global _TRADE_DATES_CACHE
    if today is None:
        today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    
    if _TRADE_DATES_CACHE is None:
        try:
            df = ak.tool_trade_date_hist_sina()
            _TRADE_DATES_CACHE = set(df["trade_date"].astype(str).tolist())
        except Exception:
            # fallback: 工作日 (排除周末, 不排除法定节假日)
            wd = datetime.strptime(today, "%Y-%m-%d").weekday()
            _TRADE_DATES_CACHE = None
            return wd < 5
    
    return today in _TRADE_DATES_CACHE


def _code_bs(code: str) -> str:
    return f"sh.{code}" if code.startswith(("6","9")) else f"sz.{code}"


def fetch_day(code: str) -> pd.DataFrame:
    """day: ak.stock_zh_a_daily 优先, baostock fallback."""
    try:
        prefix = "sh" if code.startswith("6") else "sz"
        df = ak.stock_zh_a_daily(
            symbol=f"{prefix}{code}",
            start_date="20210909",
            end_date=datetime.now(SH_TZ).strftime("%Y%m%d"),
            adjust="qfq",
        )
        wanted = ['date','open','high','low','close','volume','amount',
                  'outstanding_share','turnover']
        for c in wanted:
            if c not in df.columns:
                df[c] = pd.NA
        return df[wanted]
    except Exception:
        pass
    import baostock as bs
    bs.login()
    try:
        rs = bs.query_history_k_data_plus(
            _code_bs(code),
            "date,open,high,low,close,volume,amount,turn",
            start_date="2021-09-09",
            end_date=datetime.now(SH_TZ).strftime("%Y-%m-%d"),
            frequency="d", adjustflag="2",
        )
        rows = []
        while rs.error_code == '0' and rs.next():
            rows.append(rs.get_row_data())
        if not rows:
            raise RuntimeError("baostock empty")
        df = pd.DataFrame(rows, columns=rs.fields)
        for c in ['open','high','low','close','volume','amount']:
            df[c] = pd.to_numeric(df[c], errors='coerce')
        df['turnover'] = pd.to_numeric(df['turn'], errors='coerce') / 100.0
        df['outstanding_share'] = pd.NA
        return df[['date','open','high','low','close','volume','amount',
                  'outstanding_share','turnover']]
    finally:
        bs.logout()


def fetch_minute(code: str, tf: str) -> pd.DataFrame:
    prefix = "sh" if code.startswith("6") else "sz"
    return ak.stock_zh_a_minute(symbol=f"{prefix}{code}", period=MIN_PERIOD[tf])


def append_only(code: str, tf: str) -> dict:
    """追加新 bar, 幂等, 不覆盖."""
    csv_path = KLINE_DIR / f"{code}_{tf}.csv"
    date_col = DATE_COL[tf]

    # 拉远端
    try:
        df_live = fetch_day(code) if tf == "day" else fetch_minute(code, tf)
    except Exception as e:
        return {"code": code, "tf": tf, "ok": False, "error": str(e)[:80]}

    if df_live is None or len(df_live) == 0:
        return {"code": code, "tf": tf, "ok": False, "error": "empty"}

    df_live = df_live.copy()
    df_live[date_col] = df_live[date_col].astype(str)

    if csv_path.exists():
        df_local = pd.read_csv(csv_path, dtype={date_col: str})
        local_rows = len(df_local)
        local_last = df_local[date_col].iloc[-1]

        # 关键: 只追加 date > local_last 的 → 已经存在的不会重复
        df_new = df_live[df_live[date_col] > local_last]
        added = len(df_new)

        if added > 0:
            df_new.sort_values(date_col).to_csv(csv_path, mode='a', header=False, index=False)
    else:
        df_live_sorted = df_live.sort_values(date_col).drop_duplicates(
            subset=[date_col], keep="last"
        )
        df_live_sorted.to_csv(csv_path, index=False)
        local_rows = 0
        local_last = ""
        added = len(df_live_sorted)

    return {
        "code": code, "tf": tf, "ok": True,
        "local_before": local_rows,
        "added": added,
        "live_last": df_live[date_col].iloc[-1][:16],
    }


def get_all_codes() -> list:
    codes = set()
    for p in ["*_day.csv","*_5m.csv","*_1m.csv","*_30m.csv"]:
        for f in KLINE_DIR.glob(p):
            codes.add(f.stem.rsplit("_", 1)[0])
    return sorted(codes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--codes", type=str, default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--tf", type=str, default=",".join(ALL_TFS))
    parser.add_argument("--force", action="store_true", help="忽略交易日检查")
    args = parser.parse_args()

    tfs = [t.strip() for t in args.tf.split(",") if t.strip() in ALL_TFS]
    codes = [c.strip() for c in args.codes.split(",")] if args.codes else get_all_codes()
    if args.limit > 0:
        codes = codes[:args.limit]

    os.makedirs(KLINE_DIR, exist_ok=True)
    t0 = time.time()

    # ── 交易日检查 ──
    today = datetime.now(SH_TZ).strftime("%Y-%m-%d")
    if not args.force and not is_trade_day(today):
        print(f"[sync] {today} 非交易日, 跳过 (--force 可强制)")
        sys.exit(0)

    print(f"[sync] {datetime.now(SH_TZ).strftime('%Y-%m-%d %H:%M:%S')}  trade_day={is_trade_day(today)}")
    print(f"[sync] codes={len(codes)}  tfs={tfs}  cache={KLINE_DIR}")
    print(f"[sync] STRATEGY: append-only, 幂等, 不覆盖")
    sys.stdout.flush()

    grand_total = len(codes) * len(tfs)
    done = 0
    results = []

    for code in codes:
        for tf in tfs:
            r = append_only(code, tf)
            results.append(r)
            done += 1
            if done % 100 == 0 or done == grand_total:
                elapsed = time.time() - t0
                rate = done / elapsed if elapsed > 0 else 0
                eta = (grand_total - done) / rate if rate > 0 else 0
                print(f"[sync] {done}/{grand_total} ({done/grand_total*100:.0f}%) "
                      f"rate={rate:.1f}/s eta={eta/60:.1f}min", flush=True)
            time.sleep(API_SLEEP)

    elapsed = time.time() - t0

    # ── 汇总 ──
    by_tf = {}
    for r in results:
        s = by_tf.setdefault(r["tf"], {"ok": 0, "err": 0, "added": 0})
        if r["ok"]:
            s["ok"] += 1; s["added"] += r["added"]
        else:
            s["err"] += 1

    print(f"\n{'='*60}")
    print(f"  增量同步完成 — {elapsed:.0f}s ({elapsed/60:.1f}min)")
    print(f"{'='*60}")
    grand_added = 0
    for tf in tfs:
        s = by_tf.get(tf, {})
        print(f"  {tf:4s}: {s.get('ok',0)}/{len(codes)} OK  "
              f"{s.get('err',0)} ERR  +{s.get('added',0)} 根")
        grand_added += s.get("added", 0)
    print(f"  合计: +{grand_added} 根  ✅ 幂等 (重复跑不会重复追加)")

    # 日志
    log_dir = Path.home() / "chan_logs"; log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"kline_sync_{datetime.now(SH_TZ).strftime('%Y%m%d_%H%M%S')}.log"
    with open(log_file, "w") as f:
        f.write(f"timestamp: {datetime.now(SH_TZ).isoformat()}\n")
        f.write(f"codes: {len(codes)} tfs: {tfs} elapsed: {elapsed:.1f}\n\n")
        for r in results:
            if r.get("ok"):
                f.write(f"{r['code']},{r['tf']},OK,{r['local_before']},{r['added']}\n")
            else:
                f.write(f"{r['code']},{r['tf']},ERR,{r.get('error','?')}\n")
    print(f"  📄 {log_file}")


if __name__ == "__main__":
    main()
