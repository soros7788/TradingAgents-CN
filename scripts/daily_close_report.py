"""
A 股收盘日报主编排脚本

工作流:
    1. 加载 .env.daily_report 配置
    2. 通过 AKShare 抓取当日龙虎榜
    3. 若未发布则等待 N 分钟后重试一次
    4. 调 DeepSeek 生成全局点评 + 个股短点评
    5. 渲染 HTML 写入 REPORT_OUTPUT_DIR/YYYY-MM-DD.html

运行方式:
    python scripts/daily_close_report.py
    python scripts/daily_close_report.py --date 2026-06-19  # 指定日期
    python scripts/daily_close_report.py --skip-llm          # 跳过 LLM
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# 让脚本既能 `python scripts/daily_close_report.py` 直接跑，也能作为模块导入
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tradingagents.daily_report import (                            # noqa: E402
    fetch_lhb_today,
    annotate_stocks,
    overall_market_comment,
    save_daily_report,
    ChanlunStrategy,
)
from tradingagents.daily_report.chanlun_adapter import batch_analyze  # noqa: E402

logger = logging.getLogger("daily_close_report")


def _load_env_file(path: Path) -> None:
    """极简 .env 加载器，避免引入 python-dotenv 依赖。"""
    if not path.exists():
        logger.info(".env 文件不存在，跳过: %s", path)
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def run(date: str | None = None, skip_llm: bool = False,
        skip_chanlun: bool = False, skip_strategy: bool = False) -> str:
    """
    运行完整日报流水线，返回生成的 HTML 文件绝对路径。
    """
    output_dir = os.getenv("REPORT_OUTPUT_DIR", "/workspace/reports")
    top_n = int(os.getenv("REPORT_TOP_N", "20") or 20)
    retry_minutes = int(os.getenv("REPORT_RETRY_MINUTES", "30") or 30)

    if skip_llm:
        os.environ.pop("DEEPSEEK_API_KEY", None)

    target_date = date or datetime.now().strftime("%Y-%m-%d")
    logger.info("开始生成 %s 收盘日报", target_date)

    # 1. 抓取龙虎榜
    lhb = fetch_lhb_today(target_date)
    if not lhb["available"] and date is None:
        # 仅当跑当天时才尝试等待重试，历史日期不重试
        logger.warning("龙虎榜未发布，%d 分钟后重试一次...", retry_minutes)
        time.sleep(max(1, retry_minutes) * 60)
        lhb = fetch_lhb_today(target_date)

    # 2. 截取前 N 只参与渲染
    lhb["stocks"] = lhb.get("stocks", [])[:top_n]
    logger.info("当日上榜 %d 只，取前 %d 只渲染", lhb.get("count", 0), len(lhb["stocks"]))

    # 3. 缠论分析（对前 N 只逐只跑 K 线 + 缠论引擎）
    if lhb["stocks"] and not skip_chanlun:
        codes = [s["code"] for s in lhb["stocks"]]
        cl_results = batch_analyze(codes, days=120)
        for s in lhb["stocks"]:
            s["chanlun"] = cl_results.get(s["code"])
        logger.info("缠论分析完成: %d/%d 只", len(cl_results), len(codes))

    # 4. 区间套策略分析（对每只已跑完缠论的股票做 30min/5min/1min 多级别策略）
    if lhb["stocks"] and not skip_strategy and not skip_chanlun:
        for s in lhb["stocks"]:
            try:
                stg = ChanlunStrategy(s["code"])
                s["strategy"] = stg.run()
            except Exception as e:
                logger.warning("策略分析失败 %s: %s", s["code"], e)
                s["strategy"] = None
        logger.info("区间套策略分析完成")

    # 5. AI 点评
    if lhb["stocks"]:
        lhb["stocks"] = annotate_stocks(lhb["stocks"], top_n=top_n)
    lhb["overall_comment"] = overall_market_comment(lhb)

    # 6. 渲染 HTML
    file_path = save_daily_report(lhb, output_dir)
    logger.info("✅ 日报已生成: %s", file_path)
    return file_path


def main():
    parser = argparse.ArgumentParser(description="A 股收盘日报生成器")
    parser.add_argument("--date", default=None, help="指定日期 YYYY-MM-DD，默认今天")
    parser.add_argument("--skip-llm", action="store_true", help="跳过 DeepSeek 调用")
    parser.add_argument("--skip-chanlun", action="store_true", help="跳过缠论分析（网络受限环境）")
    parser.add_argument("--skip-strategy", action="store_true", help="跳过区间套策略分析（分钟级数据慢）")
    parser.add_argument("--env-file", default=str(ROOT / ".env.daily_report"),
                        help="配置文件路径，默认 .env.daily_report")
    args = parser.parse_args()

    _setup_logging()
    _load_env_file(Path(args.env_file))

    try:
        path = run(date=args.date, skip_llm=args.skip_llm,
                   skip_chanlun=args.skip_chanlun, skip_strategy=args.skip_strategy)
        print(path)
    except Exception as e:
        logger.exception("生成日报失败: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
