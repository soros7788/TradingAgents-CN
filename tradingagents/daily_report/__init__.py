"""轻量收盘日报子包 - 不依赖 MongoDB / Redis / toml 等主包重依赖。"""

from .lhb_provider import fetch_lhb_today
from .deepseek_summary import annotate_stocks, overall_market_comment
from .html_report import render_daily_report, save_daily_report
from .chanlun_strategy import ChanlunStrategy

__all__ = [
    "fetch_lhb_today",
    "annotate_stocks",
    "overall_market_comment",
    "render_daily_report",
    "save_daily_report",
    "ChanlunStrategy",
]
