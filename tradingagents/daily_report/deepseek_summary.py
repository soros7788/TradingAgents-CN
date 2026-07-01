"""
DeepSeek 短点评模块

为龙虎榜个股生成 ~100 字的 AI 点评。失败时返回占位文本，不影响整体流程。

依赖:
    pip install httpx

环境变量:
    DEEPSEEK_API_KEY        必填，未填则跳过 LLM 调用
    DEEPSEEK_BASE_URL       可选，默认 https://api.deepseek.com
    DEEPSEEK_MODEL          可选，默认 deepseek-chat
"""

from __future__ import annotations

import logging
import os
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-chat"
PLACEHOLDER = "AI 点评不可用（未配置 DEEPSEEK_API_KEY 或调用失败）"


def _build_prompt(stock: Dict[str, Any]) -> str:
    """构造单只个股的提示词。"""
    parts = [
        f"股票: {stock.get('name', '')}（{stock.get('code', '')}）",
        f"涨跌幅: {stock.get('change_pct', 'N/A')}%",
        f"成交额: {stock.get('turnover', 'N/A')}",
        f"龙虎榜净买入: {stock.get('net_buy', 'N/A')}",
        f"上榜原因: {stock.get('reason', '')}",
    ]
    if stock.get("buy_seats"):
        parts.append("买入席位: " + "、".join(stock["buy_seats"][:5]))
    if stock.get("sell_seats"):
        parts.append("卖出席位: " + "、".join(stock["sell_seats"][:5]))

    body = "\n".join(parts)
    return (
        "你是一位严谨的中国 A 股投资研究员。请根据以下龙虎榜数据，"
        "用不超过 100 个汉字写一条简洁点评，覆盖资金性质（机构/游资/散户）"
        "、可能催化逻辑、以及次日观察重点。不要给出买卖建议，不要使用免责声明。\n\n"
        f"{body}"
    )


def _call_deepseek(prompt: str, api_key: str, base_url: str, model: str, timeout: float = 30.0) -> str:
    """同步调用 DeepSeek Chat Completions 接口。"""
    import httpx  # 延迟导入

    url = f"{base_url.rstrip('/')}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "你是中国 A 股研究员，输出简体中文，控制在 100 字内。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 200,
    }
    resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"].strip()


def annotate_stocks(stocks: List[Dict[str, Any]], top_n: int = 20) -> List[Dict[str, Any]]:
    """
    为前 top_n 只个股添加 ai_comment 字段。

    参数:
        stocks: lhb_provider.fetch_lhb_today 返回的 stocks 列表
        top_n:  仅对前 N 只调用 LLM，节省 token

    返回:
        新列表（与输入等长），每项包含 ai_comment
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)

    annotated = []
    for idx, stock in enumerate(stocks):
        new_item = dict(stock)
        if not api_key:
            new_item["ai_comment"] = PLACEHOLDER
        elif idx >= top_n:
            new_item["ai_comment"] = ""  # 仅前 N 个调用，其余留空
        else:
            try:
                new_item["ai_comment"] = _call_deepseek(
                    _build_prompt(stock), api_key, base_url, model
                )
            except Exception as e:
                logger.warning("DeepSeek 调用失败 %s: %s", stock.get("code"), e)
                new_item["ai_comment"] = PLACEHOLDER
        annotated.append(new_item)
    return annotated


def overall_market_comment(lhb_data: Dict[str, Any]) -> str:
    """
    生成全局市场点评（约 150 字）。无 API Key 时返回占位文本。
    """
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return PLACEHOLDER

    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL)
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL)

    top_list = lhb_data.get("stocks", [])[:10]
    summary_lines = [
        f"日期: {lhb_data.get('date')}",
        f"上榜数: {lhb_data.get('count')}",
        "净买入前 10:",
    ]
    for s in top_list:
        summary_lines.append(
            f"- {s.get('name')}({s.get('code')}) 涨跌 {s.get('change_pct')}% 净买入 {s.get('net_buy')}"
        )
    prompt = (
        "你是 A 股市场观察员。基于以下龙虎榜概况，用不超过 150 字输出当日市场情绪与"
        "资金风格判断（游资活跃 / 机构主导 / 北向参与等），并指出明日值得跟进的方向。"
        "不要给出买卖建议。\n\n" + "\n".join(summary_lines)
    )
    try:
        return _call_deepseek(prompt, api_key, base_url, model)
    except Exception as e:
        logger.warning("DeepSeek 全局点评失败: %s", e)
        return PLACEHOLDER


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sample = [{
        "code": "000001",
        "name": "平安银行",
        "change_pct": 5.12,
        "turnover": 1.2e9,
        "net_buy": 1.2e8,
        "reason": "日涨幅偏离值达7%",
        "buy_seats": ["机构专用"],
        "sell_seats": ["XX游资"],
    }]
    print(annotate_stocks(sample))
