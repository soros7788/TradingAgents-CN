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


def _read_cache_df(code: str, tf: str):
    """读缓存 CSV; 不存在/损坏返回 None。day 强制字符串, 防御性按 day 排序去重。"""
    path = os.path.join(_KLINE_CACHE_DIR, f"{code}_{tf}.csv")
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_csv(path, dtype={"day": str})
    except Exception:
        return None
    if df is None or len(df) == 0:
        return None
    if not set(_REQUIRED_COLS).issubset(set(df.columns)):
        return None
    # 防御性: 保证 day 严格递增且唯一 (normalizer fail-fast 契约)。
    # 固定宽度 ISO 字符串按字典序即时间序, 无需解析。
    df = (
        df.sort_values("day", kind="mergesort")
        .drop_duplicates(subset=["day"], keep="last")
        .reset_index(drop=True)
    )
    return df


def _fallback(symbol, period, adjust, reason: str):
    """回退原 live; 缺失原引用则抛错 (不应发生)。"""
    if _ORIG is None:
        raise RuntimeError(
            "[kline_cache_shim] ak.stock_zh_a_minute 原引用缺失, 无法回退 live"
        )
    # 回退路径每次打 live, 不缓存。
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
