"""kline_cache_shim — 消费层 shim：将 ak.stock_zh_a_minute 重定向到共享盘 K 线缓存。

设计原则（治理约束）:
  - 零改 sealed: 仅运行时 setattr(ak, "stock_zh_a_minute", cached_stock_zh_a_minute)。
    sealed canonical_snapshot_acquisition._fetch_raw_minute 调用
        ak.stock_zh_a_minute(symbol=..., period=..., adjust=...)
    symbol 已由 _to_provider_symbol 加 sh/sz 前缀; period 为 "30"/"5"/"1"; adjust 默认 ""。
  - 缓存 CSV 命名 {code}_{tf}.csv (tf in 1m/5m/30m), 列 day/open/close/high/low/volume,
    day 为字符串时间戳 (与 ak.stock_zh_a_minute 原生返回一致)。
  - 未覆盖 symbol / 非 "" adjust / CSV 损坏 -> 回退原 live ak.stock_zh_a_minute (graceful, 不崩)。
  - 数据源: 环境变量 KLINE_CACHE_DIR (VM-A 默认 5TB 共享盘挂载; VM-B 设本地 sh mirror)。

注入方式 (SINGLE_WRITER 启动期调用一次 install_kline_cache_shim):
  from tradingagents.utils.kline_cache_shim import install_kline_cache_shim
  install_kline_cache_shim()
"""
from __future__ import annotations

import os

import akshare as ak
import pandas as pd
try:
    import pyarrow.parquet as pq
    _HAS_PYARROW = True
except ImportError:
    _HAS_PYARROW = False

# 数据源目录: 默认 VM-A 的 5TB 共享盘挂载; VM-B 通过环境变量指本地 sh mirror。
_KLINE_CACHE_DIR = os.environ.get(
    "KLINE_CACHE_DIR",
    "/home/gorgesoros39/gdrive/TradingAgents-CN/kline_cache",
)

# sealed _PERIOD_BY_TIMEFRAME 反向: "30"/"5"/"1" -> "30m"/"5m"/"1m"
_PERIOD_TO_TF = {"30": "30m", "5": "5m", "1": "1m"}

# 保留原 live 引用, 用于回退。模块导入时捕获一次。
_ORIG = getattr(ak, "stock_zh_a_minute", None)
_INSTALLED = False

# 需要的列 (normalizer 契约: day 字符串 + OHLCV 数值)
_REQUIRED_COLS = ("day", "open", "close", "high", "low", "volume")


def _code_of(symbol: str) -> str:
    """剥 sh/sz 前缀 -> 6 位代码。"""
    if symbol and symbol[:2] in ("sh", "sz"):
        return symbol[2:]
    return symbol


# 2026-10-06: 本地 Parquet 目录（避开 gdrive FUSE 卡顿）
_LOCAL_PARQUET_DIR = "/home/gorgesoros39/TradingAgents-CN/kline_parquet"


def _read_cache_df(code: str, tf: str):
    """读缓存; 优先 Parquet (mmap零拷贝)，fallback 到 CSV。"""
    if "_HAS_PYARROW" in globals() and _HAS_PYARROW:
        # 优先本地 Parquet（快，不走 FUSE）
        local_pq = os.path.join(_LOCAL_PARQUET_DIR, f"{code}_{tf}.parquet")
        if os.path.exists(local_pq):
            try:
                table = pq.read_table(local_pq, memory_map=True)
                df = table.to_pandas()
                if "day" in df.columns:
                    df["day"] = df["day"].astype(str)
                return _normalize_cache_df(df)
            except Exception:
                pass
        pq_path = os.path.join(_KLINE_CACHE_DIR, f"{code}_{tf}.parquet")
        if not os.path.exists(pq_path):
            alt_dir = _KLINE_CACHE_DIR.replace("kline_cache", "kline_parquet")
            alt_path = os.path.join(alt_dir, f"{code}_{tf}.parquet")
            if os.path.exists(alt_path):
                pq_path = alt_path
        if os.path.exists(pq_path):
            try:
                table = pq.read_table(pq_path, memory_map=True)
                df = table.to_pandas()
                if "day" in df.columns:
                    df["day"] = df["day"].astype(str)
                return _normalize_cache_df(df)
            except Exception:
                pass
    path = os.path.join(_KLINE_CACHE_DIR, f"{code}_{tf}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype={"day": str})
    except Exception:
        return None
    return _normalize_cache_df(df)


def _normalize_cache_df(df):
    """统一的 df 规范化：列检查 + 排序去重。"""
    if df is None or len(df) == 0:
        return None
    if "date" in df.columns and "day" not in df.columns:
        df = df.rename(columns={"date": "day"})
    # 2026-10-06: 指数CSV的date列rename后为datetime64，统一转str；
    # 下游 sealed 的 _parse_raw_timestamp 只接受 "%Y-%m-%d %H:%M[:%S]" 字符串。
    if "day" in df.columns:
        df["day"] = df["day"].astype(str)
    if not set(_REQUIRED_COLS).issubset(set(df.columns)):
        return None
    df = (
        df.sort_values("day", kind="mergesort")
        .drop_duplicates(subset=["day"], keep="last")
        .reset_index(drop=True)
    )
    # 坏行过滤 (2026-10-06 audit): 空/非数值 OHLC 或 high/low 与 open/close 矛盾的 bar
    # 会让 CanonicalBar 校验 fail-fast, 使整只标的 ERROR。只剔除坏 bar, 其余照常。
    ohlc = df[["open", "high", "low", "close"]].apply(pd.to_numeric, errors="coerce")
    ok = (
        ohlc.notna().all(axis=1)
        & (ohlc > 0).all(axis=1)
        & (ohlc["high"] >= ohlc[["open", "close"]].max(axis=1))
        & (ohlc["low"] <= ohlc[["open", "close"]].min(axis=1))
        & (ohlc["high"] >= ohlc["low"])
    )
    dropped = int((~ok).sum())
    if dropped:
        print(f"[kline_cache_shim] WARN dropped {dropped} invalid bar(s) "
              f"of {len(df)} (NaN/inconsistent OHLC)")
        df = df[ok].reset_index(drop=True)
        if len(df) == 0:
            return None
    return df

def _clean_symbol(s: str) -> str:
    """清洗畸形 symbol：去双前缀（szsh/shsh/szsz/shsz），已带前缀原样返回，否则按规则加前缀。"""
    c = str(s).strip().lower()
    # 双前缀去重
    for dp in ("szsh", "shsh", "szsz", "shsz"):
        if c.startswith(dp):
            c = c[2:]
            break
    if c.startswith(("sh", "sz", "bj")):
        return c
    # 无前缀，按首位判断
    if c.startswith(("6", "9")) or c.startswith("688"):
        return f"sh{c}"
    return f"sz{c}"


def _fallback(symbol, period, adjust, reason: str):
    """回退原 live; 缺失原引用则抛错 (不应发生)。"""
    if _ORIG is None:
        raise RuntimeError(
            "[kline_cache_shim] ak.stock_zh_a_minute 原引用缺失, 无法回退 live"
        )
    # 回退路径每次打 live, 不缓存。先清洗畸形 symbol。
    symbol = _clean_symbol(symbol)
    return _ORIG(symbol=symbol, period=period, adjust=adjust)


def cached_stock_zh_a_minute(symbol, period, adjust="", **_kw):
    """shim: 命中缓存返回 DataFrame, 否则回退 live。签名兼容 ak.stock_zh_a_minute。"""
    # 仅未复权缓存可用; 非 "" -> 回退 live (缓存是 adjust="")
    if adjust not in ("", None):
        return _fallback(symbol, period, adjust, f"adjust={adjust!r}!=empty")
    tf = _PERIOD_TO_TF.get(period)
    if tf is None:
        return _fallback(symbol, period, adjust, f"unknown period={period!r}")
    code = _code_of(symbol)
    df = _read_cache_df(code, tf)
    if df is None:
        return _fallback(symbol, period, adjust, f"cache miss {code}_{tf}")
    return df


def install_kline_cache_shim(cache_dir: str | None = None) -> bool:
    """注入 shim 到全局 ak 模块。幂等。返回是否本次新安装。"""
    global _KLINE_CACHE_DIR, _INSTALLED, _ORIG
    if cache_dir:
        _KLINE_CACHE_DIR = cache_dir
    if _ORIG is None:
        _ORIG = getattr(ak, "stock_zh_a_minute", None)
    if _INSTALLED:
        return False
    setattr(ak, "stock_zh_a_minute", cached_stock_zh_a_minute)
    _INSTALLED = True
    print(
        f"[kline_cache_shim] installed; cache_dir={_KLINE_CACHE_DIR}; "
        f"orig_live={'captured' if _ORIG is not None else 'MISSING'}"
    )
    return True
