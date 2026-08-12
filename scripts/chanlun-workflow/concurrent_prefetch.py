# -*- coding: utf-8 -*-
"""
并发数据预取模块 (2026-08-09) V2 — 递归优化版
在分析前批量预取K线数据到缓存, 大幅减少串行等待时间

策略:
  1. 用线程池并发请求新浪API
  2. 只预取5min级别数据, 30min/日线由递归系统从5min合成
  3. 预取写入持久缓存, 后续analyze_beichi直接命中缓存
  4. 缓存已命中时跳过网络请求 (零成本)

递归收益:
  旧: 2886只 x 3级别(日线/30min/5min) = 8658次API请求
  新: 2886只 x 1级别(5min) = 2886次API请求 (省67%积分)
  30min和日线由 chanlun_recurisive.recurse_full_pipeline 本地合成

性能目标:
  持仓股(~30只) x 1级别(5min) = 30次请求, 并发20线程 -> ~2秒
  全市场~2886只 x 1级别(5min) = 2886次请求, 并发20线程 -> ~1.2秒

用法:
  from concurrent_prefetch import prefetch_stocks, prefetch_holdings, prefetch_candidate_pool

  # 预取持仓股 (5min)
  prefetch_holdings(holdings)

  # 预取全市场 (5min)
  prefetch_candidate_pool()

  # 自定义预取
  prefetch_stocks(["600519", "000001"], levels=["5min"])
"""

import os, sys, time, json, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from beichi_analyzer import fetch_kline_sina, _kline_cache_load, _kline_cache_save

# 级别配置 — V2: 只预取5min, 30min/日线由递归合成
LEVELS = ["5min"]
SCALE_MAP = {"5min": "5"}
DATALEN_MAP = {"5min": 3000}
CACHE_MAX_AGE = {"5min": 3600 * 4}

# 进度报告 (线程安全)
_progress_lock = threading.Lock()
_progress = {"total": 0, "done": 0, "hit": 0, "miss": 0, "fail": 0}


def _prefetch_one(args):
    """预取一只股票一个级别的K线数据"""
    code, level = args
    scale = SCALE_MAP[level]
    datalen = DATALEN_MAP[level]
    max_age = CACHE_MAX_AGE[level]

    # 检查缓存是否已命中 (避免重复网络请求)
    cached = _kline_cache_load(code, scale, max_age)
    if cached is not None and len(cached) >= datalen:
        with _progress_lock:
            _progress["hit"] += 1
            _progress["done"] += 1
        return {"code": code, "level": level, "status": "hit", "len": len(cached)}

    # 缓存未命中, 发起网络请求
    # fetch_kline_sina 内部有缓存写入逻辑
    try:
        data = fetch_kline_sina(code, scale, datalen)
        if data:
            with _progress_lock:
                _progress["miss"] += 1
                _progress["done"] += 1
            return {"code": code, "level": level, "status": "fetched", "len": len(data)}
        else:
            with _progress_lock:
                _progress["fail"] += 1
                _progress["done"] += 1
            return {"code": code, "level": level, "status": "empty", "len": 0}
    except Exception as e:
        with _progress_lock:
            _progress["fail"] += 1
            _progress["done"] += 1
        return {"code": code, "level": level, "status": "error", "error": str(e)}


def prefetch_stocks(stock_codes, levels=None, max_workers=15, silent=False):
    """
    并发预取股票K线数据到缓存

    参数:
        stock_codes: list[str] 股票代码列表
        levels: list[str] 级别列表, 默认["日线", "30min", "5min"]
        max_workers: int 并发数, 默认15
        silent: bool 是否静默模式

    返回:
        dict: {total, hit, miss, fail, elapsed, stocks, levels}
    """
    global _progress
    _progress = {"total": 0, "done": 0, "hit": 0, "miss": 0, "fail": 0}

    if levels is None:
        levels = LEVELS
    if not stock_codes:
        return {"total": 0, "hit": 0, "miss": 0, "fail": 0, "elapsed": 0, "stocks": 0, "levels": levels}

    # 构建任务列表: (code, level) 配对
    tasks = [(code, level) for code in stock_codes for level in levels]
    _progress["total"] = len(tasks)

    if not silent:
        n = len(stock_codes)
        print(f"[预取] {n}只股票 x {len(levels)}级别 = {len(tasks)}次请求, 并发{max_workers}线程")

    t0 = time.time()
    results = []

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_prefetch_one, t): t for t in tasks}

        # 【超时保护 2026-08-11】as_completed加全局30秒超时
        # 问题: 某个worker线程网络请求卡死 → as_completed无限等待 → 整个预取卡死
        # 修复: timeout=30秒, 超时后剩余任务标记为"timeout"并关闭executor
        completed_count = 0
        total_tasks = len(tasks)
        try:
            for i, future in enumerate(as_completed(futures, timeout=30)):
                try:
                    r = future.result()
                    results.append(r)
                except Exception as e:
                    code, level = futures[future]
                    results.append({"code": code, "level": level, "status": "error", "error": str(e)})
                    with _progress_lock:
                        _progress["fail"] += 1
                        _progress["done"] += 1
                completed_count = i + 1

                # 进度报告
                if not silent and (i + 1) % 100 == 0:
                    p = _progress
                    print(f"  预取: {p['done']}/{p['total']} (命中{p['hit']} 获取{p['miss']} 失败{p['fail']})", flush=True)
        except TimeoutError:
            # 超时: 标记剩余未完成的任务为timeout
            remaining = total_tasks - completed_count
            if not silent:
                print(f"  ⚠ 预取超时(30s): 已完成{completed_count}/{total_tasks}, 剩余{remaining}个任务标记为超时跳过", flush=True)
            executor.shutdown(wait=False, cancel_futures=True)
            with _progress_lock:
                _progress["fail"] += remaining
                _progress["done"] = total_tasks

    elapsed = time.time() - t0
    p = _progress

    if not silent:
        print(f"[预取] 完成: {p['total']}次请求, 耗时{elapsed:.0f}秒 | "
              f"命中{p['hit']} 获取{p['miss']} 失败{p['fail']} | "
              f"平均{elapsed/max(p['total'],1):.2f}秒/次")

    return {
        "total": p["total"],
        "hit": p["hit"],
        "miss": p["miss"],
        "fail": p["fail"],
        "elapsed": round(elapsed, 1),
        "stocks": len(stock_codes),
        "levels": levels,
    }


def prefetch_holdings(holdings, levels=None, max_workers=15):
    """
    预取持仓股数据

    参数:
        holdings: list[dict] 持仓列表, 每项含code
        levels: list[str] 级别列表
        max_workers: int 并发数

    返回: prefetch_stocks 的结果
    """
    codes = [h["code"] for h in holdings if h.get("code")]
    n = len(codes)
    print(f"[预取] 持仓股{n}只, 预取各级别数据...")
    return prefetch_stocks(codes, levels, max_workers)


def prefetch_candidate_pool(max_workers=20):
    """
    预取全市场候选池5min数据

    从full_scan获取沪A+深市列表, 只预取5min
    30min/日线由递归系统从5min合成

    返回: prefetch_stocks 的结果
    """
    from full_scan import fetch_sha_list, fetch_sza_prices
    print("[预取] 获取沪A+深市股票列表...")
    sha = fetch_sha_list()
    sza = fetch_sza_prices(silent=True)
    codes = [s["code"] for s in sha + sza]
    print(f"[预取] 共{len(codes)}只股票, 预取5min数据...")
    return prefetch_stocks(codes, levels=["5min"], max_workers=max_workers)


def prefetch_by_codes_file(filepath, levels=None, max_workers=15):
    """
    从文件读取代码列表并预取
    文件格式: 每行一个代码, 支持 # 注释
    """
    codes = []
    with open(filepath, 'r') as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#'):
                # 提取6位数字代码
                import re
                m = re.search(r'(\d{6})', line)
                if m:
                    codes.append(m.group(1))
    print(f"[预取] 从文件读取{len(codes)}只股票代码")
    return prefetch_stocks(codes, levels, max_workers)