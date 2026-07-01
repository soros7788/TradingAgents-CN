"""
龙虎榜数据 Provider

通过 AKShare 获取当日龙虎榜数据，输出标准化结构供后续渲染与分析使用。

依赖:
    pip install akshare pandas

公开接口:
    fetch_lhb_today(date=None) -> dict
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, date as _date
from typing import Optional

logger = logging.getLogger(__name__)


def _to_date_str(d: Optional[str]) -> str:
    """将日期参数标准化为 YYYYMMDD 字符串。"""
    if d is None:
        return datetime.now().strftime("%Y%m%d")
    if isinstance(d, (datetime, _date)):
        return d.strftime("%Y%m%d")
    # 已经是字符串，去除分隔符
    return str(d).replace("-", "").replace("/", "").strip()


def _safe_call(func, *args, retries: int = 3, wait: float = 5.0, **kwargs):
    """对 AKShare 调用做简单的限流 / 异常重试。"""
    last_err = None
    for i in range(retries):
        try:
            return func(*args, **kwargs)
        except Exception as e:  # AKShare 抛出网络/限流异常时重试
            last_err = e
            logger.warning("AKShare 调用 %s 第 %d 次失败: %s", func.__name__, i + 1, e)
            time.sleep(wait)
    logger.error("AKShare 调用 %s 连续 %d 次失败，放弃。最后错误: %s", func.__name__, retries, last_err)
    return None


def fetch_lhb_today(date: Optional[str] = None) -> dict:
    """
    抓取指定日期的 A 股龙虎榜数据。

    参数:
        date: 日期，支持 'YYYY-MM-DD' / 'YYYYMMDD' / datetime / None（默认今日）

    返回:
        {
            "date": "YYYY-MM-DD",
            "available": bool,           # 当日是否已发布龙虎榜
            "count": int,                # 上榜股票数
            "stocks": [                  # 上榜个股列表
                {
                    "code": "000001",
                    "name": "平安银行",
                    "change_pct": 5.12,         # 涨跌幅
                    "turnover": 1234567890.0,   # 成交额
                    "net_buy": 12345678.0,      # 龙虎榜净买入
                    "reason": "日涨幅偏离值达7%",
                    "buy_seats": ["XX证券XX营业部", ...],
                    "sell_seats": ["YY证券YY营业部", ...],
                }
            ],
            "raw_columns": [...],        # 原始字段名，便于排错
        }

    设计说明:
        - 不抛异常，调用失败时返回 available=False 的空骨架，由上层决定是否重试
        - AKShare 的字段命名时常变动，这里做模糊匹配以增强兼容性
    """
    date_str = _to_date_str(date)
    pretty_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    result = {
        "date": pretty_date,
        "available": False,
        "count": 0,
        "stocks": [],
        "raw_columns": [],
    }

    try:
        import akshare as ak  # 延迟导入，避免在无依赖环境下崩溃
    except ImportError:
        logger.error("未安装 akshare，请先 `pip install akshare`")
        return result

    # AKShare 龙虎榜详情接口（东财），日期格式 YYYYMMDD
    df = _safe_call(ak.stock_lhb_detail_em, start_date=date_str, end_date=date_str)
    if df is None or df.empty:
        logger.info("龙虎榜数据为空，可能尚未发布: %s", pretty_date)
        return result

    result["available"] = True
    result["raw_columns"] = list(df.columns)

    # AKShare 字段在不同版本可能为：'代码'/'股票代码', '名称'/'股票名称', '涨跌幅', '成交额', '龙虎榜净买额', '上榜原因' 等
    def pick(row, *candidates, default=None):
        for c in candidates:
            if c in row.index:
                v = row[c]
                if v is not None:
                    return v
        return default

    stocks: dict = {}
    for _, row in df.iterrows():
        code = str(pick(row, "代码", "股票代码", default="")).zfill(6)
        if not code:
            continue
        name = str(pick(row, "名称", "股票名称", default=""))
        reason = str(pick(row, "上榜原因", default=""))
        change_pct = pick(row, "涨跌幅", default=None)
        turnover = pick(row, "成交额", default=None)
        net_buy = pick(row, "龙虎榜净买额", "净买额", default=None)

        item = stocks.setdefault(code, {
            "code": code,
            "name": name,
            "change_pct": _to_float(change_pct),
            "turnover": _to_float(turnover),
            "net_buy": _to_float(net_buy),
            "reason": reason,
            "buy_seats": [],
            "sell_seats": [],
        })
        # 同一只股票可能有多条上榜原因
        if reason and reason not in item["reason"]:
            item["reason"] = (item["reason"] + " / " + reason).strip(" /")

    # 尝试拉取席位明细（可选，不影响主流程）。AKShare 接口签名为
    # stock_lhb_stock_detail_em(symbol, date, flag) ，需要逐只逐方向查询，
    # 因此只对净买入排名最高的前 N 只调用，避免大量请求被限流。
    try:
        seat_top_n = int(os.getenv("LHB_SEAT_TOP_N", "5") or 5)
    except ValueError:
        seat_top_n = 5

    if seat_top_n > 0:
        sorted_for_seats = sorted(
            stocks.values(),
            key=lambda s: (s["net_buy"] or 0),
            reverse=True,
        )[:seat_top_n]
        for s in sorted_for_seats:
            for flag in ("买入", "卖出"):
                seat_df = _safe_call(
                    ak.stock_lhb_stock_detail_em,
                    symbol=s["code"], date=date_str, flag=flag,
                    retries=2, wait=2.0,
                )
                if seat_df is None or seat_df.empty:
                    continue
                for _, row in seat_df.iterrows():
                    seat_name = str(pick(row, "营业部名称", "交易营业部名称", default="")).strip()
                    if not seat_name:
                        continue
                    if flag == "买入":
                        s["buy_seats"].append(seat_name)
                    else:
                        s["sell_seats"].append(seat_name)

    # 按龙虎榜净买入金额降序排序
    sorted_stocks = sorted(
        stocks.values(),
        key=lambda s: (s["net_buy"] or 0),
        reverse=True,
    )
    result["stocks"] = sorted_stocks
    result["count"] = len(sorted_stocks)
    logger.info("龙虎榜抓取成功 %s: %d 只", pretty_date, result["count"])
    return result


def _to_float(v) -> Optional[float]:
    """安全转 float，失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    # 小型烟雾测试
    logging.basicConfig(level=logging.INFO)
    data = fetch_lhb_today()
    print(f"日期: {data['date']}, 可用: {data['available']}, 上榜数: {data['count']}")
    for s in data["stocks"][:5]:
        print(s)
