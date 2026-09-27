from langchain_core.messages import AIMessage
import time
import json

# 导入统一日志系统
from tradingagents.utils.logging_init import get_logger
logger = get_logger("default")


def create_bear_researcher(llm, memory):
    def bear_node(state) -> dict:
        investment_debate_state = state["investment_debate_state"]
        history = investment_debate_state.get("history", "")
        bear_history = investment_debate_state.get("bear_history", "")

        current_response = investment_debate_state.get("current_response", "")
        market_research_report = state["market_report"]
        sentiment_report = state["sentiment_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]

        # 使用统一的股票类型检测
        ticker = state.get('company_of_interest', 'Unknown')
        from tradingagents.utils.stock_utils import StockUtils
        market_info = StockUtils.get_market_info(ticker)
        is_china = market_info['is_china']

        # 获取公司名称
        def _get_company_name(ticker_code: str, market_info_dict: dict) -> str:
            """根据股票代码获取公司名称"""
            try:
                if market_info_dict['is_china']:
                    from tradingagents.dataflows.interface import get_china_stock_info_unified
                    stock_info = get_china_stock_info_unified(ticker_code)
                    if stock_info and "股票名称:" in stock_info:
                        name = stock_info.split("股票名称:")[1].split("\n")[0].strip()
                        logger.info(f"✅ [空头研究员] 成功获取中国股票名称: {ticker_code} -> {name}")
                        return name
                    else:
                        # 降级方案
                        try:
                            from tradingagents.dataflows.data_source_manager import get_china_stock_info_unified as get_info_dict
                            info_dict = get_info_dict(ticker_code)
                            if info_dict and info_dict.get('name'):
                                name = info_dict['name']
                                logger.info(f"✅ [空头研究员] 降级方案成功获取股票名称: {ticker_code} -> {name}")
                                return name
                        except Exception as e:
                            logger.error(f"❌ [空头研究员] 降级方案也失败: {e}")
                elif market_info_dict['is_hk']:
                    try:
                        from tradingagents.dataflows.providers.hk.improved_hk import get_hk_company_name_improved
                        name = get_hk_company_name_improved(ticker_code)
                        return name
                    except Exception:
                        clean_ticker = ticker_code.replace('.HK', '').replace('.hk', '')
                        return f"港股{clean_ticker}"
                elif market_info_dict['is_us']:
                    us_stock_names = {
                        'AAPL': '苹果公司', 'TSLA': '特斯拉', 'NVDA': '英伟达',
                        'MSFT': '微软', 'GOOGL': '谷歌', 'AMZN': '亚马逊',
                        'META': 'Meta', 'NFLX': '奈飞'
                    }
                    return us_stock_names.get(ticker_code.upper(), f"美股{ticker_code}")
            except Exception as e:
                logger.error(f"❌ [空头研究员] 获取公司名称失败: {e}")
            return f"股票代码{ticker_code}"

        company_name = _get_company_name(ticker, market_info)
        is_hk = market_info['is_hk']
        is_us = market_info['is_us']

        currency = market_info['currency_name']
        currency_symbol = market_info['currency_symbol']

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"

        # A4-R9: optional precomputed Chan context (shared neutral text, injected verbatim).
        # 有效性只做字符串存在性/空白判断；注入必须使用原始 raw 字符串（byte-identical）。
        _raw_chan = state.get("chan_debate_context")
        _has_chan = isinstance(_raw_chan, str) and bool(_raw_chan.strip())
        chan_context_block = (
            f"缠论结构上下文：\n{_raw_chan}\n" if _has_chan else ""
        )

        # DEFECT-2026-09-05-A 方案 B：统计 B 引擎(INTERVAL)真买卖点，替代无效的 A 引擎 direction 统计
        # 依据：formatter 每行输出 "EVIDENCE {id} engine={e} level={l} type={t} direction={d} status={s}"
        #   engine=INTERVAL → B 引擎；type=BUY_POINT/SELL_POINT → 真买卖点（唯一可信信号）
        #   A 引擎(RECURSIVE)的 direction 逐段交替、恒 50/50，无指示性，不可用作方向依据
        # 不 import sealed Chan 模块，保持 A4-R9 "No Chan schema import" 约束
        import re as _re
        _dir_block = ""
        if _has_chan:
            _ev = _re.findall(
                r"EVIDENCE\s+(\S+)\s+engine=(\w+)\s+level=(\S+)\s+type=(\w+)\s+direction=(\w+)\s+status=(\w+)",
                _raw_chan,
            )
            _buys, _sells = [], []
            for _eid, _engine, _level, _type, _dir, _status in _ev:
                if _engine.upper() != "INTERVAL":
                    continue  # 只信 B 引擎
                if _type.upper() == "BUY_POINT":
                    _buys.append(_level)
                elif _type.upper() == "SELL_POINT":
                    _sells.append(_level)
            _weight = {"30min": 3, "5min": 2, "1min": 1, "15min": 2, "60min": 3}
            _b_score = sum(_weight.get(lv, 1) for lv in _buys)
            _s_score = sum(_weight.get(lv, 1) for lv in _sells)
            _lines = [
                "缠论买卖点汇总（B 引擎区间套，纯统计非 LLM 解读）：",
                f"  买点 BUY_POINT={len(_buys)} (加权 {_b_score})  明细 {sorted(set(_buys)) or '无'}",
                f"  卖点 SELL_POINT={len(_sells)} (加权 {_s_score})  明细 {sorted(set(_sells)) or '无'}",
            ]
            if _buys and not _sells:
                _lines.append("  → B 引擎单向给出买点（背驰底）✅")
            elif _sells and not _buys:
                _lines.append("  → B 引擎单向给出卖点（背驰顶）✅")
            elif _buys and _sells:
                _lines.append("  → B 引擎买卖点并存（级别打架，方向未定）⚠️")
            else:
                _lines.append("  → B 引擎本轮无买卖点（无背驰结构）")
            if "30min" in _buys or "60min" in _buys:
                _lines.append("  ★ 含 30min/60min 高级别买点，信号强度最高")
            if "30min" in _sells or "60min" in _sells:
                _lines.append("  ★ 含 30min/60min 高级别卖点，回避优先级最高")
            _dir_block = "\n" + "\n".join(_lines) + "\n"

        # 安全检查：确保memory不为None
        if memory is not None:
            past_memories = memory.get_memories(curr_situation, n_matches=2)
        else:
            logger.warning(f"⚠️ [DEBUG] memory为None，跳过历史记忆检索")
            past_memories = []

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        prompt = f"""你是一位看跌分析师，负责论证不投资股票 {company_name}（股票代码：{ticker}）的理由。

⚠️ 重要提醒：当前分析的是 {market_info['market_name']}，所有价格和估值请使用 {currency}（{currency_symbol}）作为单位。
⚠️ 在你的分析中，请始终使用公司名称"{company_name}"而不是股票代码"{ticker}"来称呼这家公司。

你的目标是提出合理的论证，强调风险、挑战和负面指标。利用提供的研究和数据来突出潜在的不利因素并有效反驳看涨论点。

请用中文回答，重点关注以下几个方面：

- 风险和挑战：突出市场饱和、财务不稳定或宏观经济威胁等可能阻碍股票表现的因素
- 竞争劣势：强调市场地位较弱、创新下降或来自竞争对手威胁等脆弱性
- 负面指标：使用财务数据、市场趋势或最近不利消息的证据来支持你的立场
- 反驳看涨观点：用具体数据和合理推理批判性分析看涨论点，揭露弱点或过度乐观的假设
- 参与讨论：以对话风格呈现你的论点，直接回应看涨分析师的观点并进行有效辩论，而不仅仅是列举事实

可用资源：

市场研究报告：{market_research_report}
社交媒体情绪报告：{sentiment_report}
最新世界事务新闻：{news_report}
公司基本面报告：{fundamentals_report}
{chan_context_block}{_dir_block}辩论对话历史：{history}
最后的看涨论点：{current_response}
类似情况的反思和经验教训：{past_memory_str}

请使用这些信息提供令人信服的看跌论点，反驳看涨声明，并参与动态辩论，展示投资该股票的风险和弱点。你还必须处理反思并从过去的经验教训和错误中学习。

请确保所有回答都使用中文。
"""

        response = llm.invoke(prompt)

        argument = f"Bear Analyst: {response.content}"

        new_count = investment_debate_state["count"] + 1
        logger.info(f"🐻 [空头研究员] 发言完成，计数: {investment_debate_state['count']} -> {new_count}")

        new_investment_debate_state = {
            "history": history + "\n" + argument,
            "bear_history": bear_history + "\n" + argument,
            "bull_history": investment_debate_state.get("bull_history", ""),
            "current_response": argument,
            "count": new_count,
        }

        return {"investment_debate_state": new_investment_debate_state}

    return bear_node
