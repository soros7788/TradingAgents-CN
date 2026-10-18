import os
import sys
import glob
import time
from multiprocessing import Pool, cpu_count
import pandas as pd

KLINE_DIR = os.environ.get("KLINE_CACHE_DIR", "kline_cache")
if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]):
    KLINE_DIR = sys.argv[1]

def process_single_stock(code):
    try:
        f_1m = os.path.join(KLINE_DIR, f"{code}_1m.csv")
        f_5m = os.path.join(KLINE_DIR, f"{code}_5m.csv")
        f_30m = os.path.join(KLINE_DIR, f"{code}_30m.csv")

        if not os.path.exists(f_1m):
            return code, 0, 0, "no_1m"

        df_1m = pd.read_csv(f_1m)
        if df_1m.empty or len(df_1m) < 2:
            return code, 0, 0, "1m_empty"

        t_col_1m = df_1m.columns[0]
        df_1m[t_col_1m] = pd.to_datetime(df_1m[t_col_1m])
        df_1m = df_1m.set_index(t_col_1m).sort_index()

        # Resample 5m
        df_5m_res = df_1m.resample("5min", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()

        # Resample 30m
        df_30m_res = df_1m.resample("30min", label="right", closed="right").agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()

        appended_5m = 0
        appended_30m = 0

        # Handle 5m
        if os.path.exists(f_5m) and os.path.getsize(f_5m) > 10:
            df_5m_orig = pd.read_csv(f_5m)
            t_col_5m = df_5m_orig.columns[0]
            last_5m_time = pd.to_datetime(df_5m_orig.iloc[-1][t_col_5m])
            new_5m = df_5m_res[df_5m_res.index > last_5m_time].copy()
            if not new_5m.empty:
                new_5m = new_5m.reset_index()
                new_5m.columns = [t_col_5m, "open", "high", "low", "close", "volume"]
                new_5m.to_csv(f_5m, mode="a", header=False, index=False)
                appended_5m = len(new_5m)
        else:
            new_5m = df_5m_res.reset_index()
            new_5m.columns = ["date", "open", "high", "low", "close", "volume"]
            new_5m.to_csv(f_5m, index=False)
            appended_5m = len(new_5m)

        # Handle 30m
        if os.path.exists(f_30m) and os.path.getsize(f_30m) > 10:
            df_30m_orig = pd.read_csv(f_30m)
            t_col_30m = df_30m_orig.columns[0]
            last_30m_time = pd.to_datetime(df_30m_orig.iloc[-1][t_col_30m])
            new_30m = df_30m_res[df_30m_res.index > last_30m_time].copy()
            if not new_30m.empty:
                new_30m = new_30m.reset_index()
                new_30m.columns = [t_col_30m, "open", "high", "low", "close", "volume"]
                new_30m.to_csv(f_30m, mode="a", header=False, index=False)
                appended_30m = len(new_30m)
        else:
            new_30m = df_30m_res.reset_index()
            new_30m.columns = ["date", "open", "high", "low", "close", "volume"]
            new_30m.to_csv(f_30m, index=False)
            appended_30m = len(new_30m)

        return code, appended_5m, appended_30m, "ok"
    except Exception as e:
        return code, 0, 0, f"err: {str(e)}"

def run_resample_universe(max_workers=None, limit=None):
    if max_workers is None:
        max_workers = min(cpu_count(), 4)

    files = sorted(glob.glob(os.path.join(KLINE_DIR, "*_1m.csv")))
    codes = [os.path.basename(f).split("_")[0] for f in files]
    if limit:
        codes = codes[:limit]

    total = len(codes)
    print(f"Target directory: {KLINE_DIR}")
    print(f"Starting resample for {total} stocks with {max_workers} workers...")
    t0 = time.time()

    total_5m = 0
    total_30m = 0
    updated_cnt = 0
    err_cnt = 0

    with Pool(processes=max_workers) as pool:
        for i, res in enumerate(pool.imap_unordered(process_single_stock, codes, chunksize=32)):
            code, n5, n30, status = res
            if status == "ok":
                if n5 > 0 or n30 > 0:
                    updated_cnt += 1
                    total_5m += n5
                    total_30m += n30
            else:
                err_cnt += 1

            if (i + 1) % 500 == 0 or (i + 1) == total:
                elapsed = time.time() - t0
                qps = (i + 1) / elapsed
                print(f"[{i+1}/{total}] Progress: {((i+1)/total)*100:.1f}%, Updated: {updated_cnt}, Err: {err_cnt}, Speed: {qps:.1f} stocks/s")

    elapsed = time.time() - t0
    print(f"=== Complete ===")
    print(f"Total stocks: {total}, Updated: {updated_cnt}, Errors: {err_cnt}")
    print(f"Appended 5m bars: {total_5m}, 30m bars: {total_30m}")
    print(f"Total time elapsed: {elapsed:.1f}s ({elapsed/60:.2f}m)")

if __name__ == "__main__":
    limit = None
    workers = None
    for arg in sys.argv[1:]:
        if arg.isdigit():
            limit = int(arg)
    run_resample_universe(max_workers=workers, limit=limit)
