import time
import json
import re

# 导入统一日志系统
from tradingagents.utils.logging_init import get_logger
from tradingagents.agents.utils.instrument_utils import build_instrument_context
logger = get_logger("default")


# ============================================================
# 动态仓位风控拦截 (P2, 2026-08-02)
#
# Gemini PDF建议: 增加 MoneyLosingScore/MaxDrawdown 程序化拦截
# 在LLM决策前注入风险提示, 在LLM决策后拦截违规操作
#
# MoneyLosingScore: 连续亏损次数评分 (0-5)
#   0-2: 正常, 3+: 仓位压制, 5: 禁止新建仓
# MaxDrawdown: 历史最大回撤百分比
#   >15%: 禁止新建仓, 10-15%: 谨慎, <10%: 正常
# ============================================================

def _compute_risk_gates(past_memories):
    """
    从历史记忆中计算风险指标

    参数:
        past_memories: memory.get_memories() 返回的列表

    返回: {
        "money_losing_score": int (0-5),
        "max_consecutive_losses": int,
        "max_drawdown_pct": float,
        "risk_level": "高"/"中"/"低",
        "warning": str,
        "block_buy": bool,
    }
    """
    consecutive_losses = 0
    max_consecutive_losses = 0
    max_drawdown_pct = 0.0

    loss_keywords = ["亏损", "止损", "赔", "亏", "loss", "止损清仓", "破位"]

    for rec in past_memories:
        recommendation = rec.get("recommendation", "") if isinstance(rec, dict) else str(rec)

        # 检测连续亏损
        if any(kw in recommendation for kw in loss_keywords):
            consecutive_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, consecutive_losses)
        else:
            consecutive_losses = 0

        # 提取回撤百分比
        dd_match = re.search(r'回撤[：:\s]*(\d+\.?\d*)\s*%', recommendation)
        if dd_match:
            dd = float(dd_match.group(1))
            max_drawdown_pct = max(max_drawdown_pct, dd)

    # MoneyLosingScore: 0-5
    money_losing_score = min(5, max_consecutive_losses)

    # 风控判定
    if max_drawdown_pct > 15 or money_losing_score >= 5:
        block_buy = True
        risk_level = "高"
    elif max_drawdown_pct > 10 or money_losing_score >= 3:
        block_buy = False
        risk_level = "中"
    else:
        block_buy = False
        risk_level = "低"

    # 构建风险提示
    warnings = []
    if money_losing_score >= 3:
        warnings.append(
            f"MoneyLosingScore={money_losing_score}/5 "
            f"(连续亏损{max_consecutive_losses}次), 建议降低仓位至50%以下"
        )
    if max_drawdown_pct > 15:
        warnings.append(
            f"MaxDrawdown={max_drawdown_pct:.1f}%超过15%阈值, 禁止新建仓"
        )
    elif max_drawdown_pct > 10:
        warnings.append(
            f"MaxDrawdown={max_drawdown_pct:.1f}%接近阈值, 谨慎建仓"
        )

    warning = "\n".join(warnings) if warnings else "无特殊风险提示"

    return {
        "money_losing_score": money_losing_score,
        "max_consecutive_losses": max_consecutive_losses,
        "max_drawdown_pct": max_drawdown_pct,
        "risk_level": risk_level,
        "warning": warning,
        "block_buy": block_buy,
    }


def create_risk_manager(llm, memory, knowledge_retriever=None):
    def risk_manager_node(state) -> dict:

        company_name = state["company_of_interest"]
        instrument_context = build_instrument_context(company_name)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        market_research_report = state["market_report"]
        news_report = state["news_report"]
        fundamentals_report = state["fundamentals_report"]
        sentiment_report = state["sentiment_report"]
        trader_plan = state["investment_plan"]

        curr_situation = f"{market_research_report}\n\n{sentiment_report}\n\n{news_report}\n\n{fundamentals_report}"

        # 安全检查：确保memory不为None
        if memory is not None:
            past_memories = memory.get_memories(curr_situation, n_matches=2)
        else:
            logger.warning(f"⚠️ [DEBUG] memory为None，跳过历史记忆检索")
            past_memories = []

        past_memory_str = ""
        for i, rec in enumerate(past_memories, 1):
            past_memory_str += rec["recommendation"] + "\n\n"

        # P2 (2026-08-02): 动态仓位风控拦截 — MoneyLosingScore/MaxDrawdown
        risk_gates = _compute_risk_gates(past_memories)
        risk_gate_str = ""
        if risk_gates["risk_level"] != "低":
            risk_gate_str = f"""
**动态风控拦截 (P2):**
- 风险等级: {risk_gates['risk_level']}
- MoneyLosingScore: {risk_gates['money_losing_score']}/5 (连续亏损{risk_gates['max_consecutive_losses']}次)
- MaxDrawdown: {risk_gates['max_drawdown_pct']:.1f}%
- 风控提示: {risk_gates['warning']}
- 买入拦截: {'是' if risk_gates['block_buy'] else '否'}

**注意**: 若风险等级为"高", 禁止建议买入; 若为"中", 建议降低仓位至50%以下。
"""

        # RAG 升级: 检索外部知识库
        kb_context_str = ""
        if knowledge_retriever is not None:
            try:
                kb_results = knowledge_retriever.retrieve_for_agent(
                    query=curr_situation, agent_role="risk_manager"
                )
                kb_context_str = knowledge_retriever.format_context(kb_results)
            except Exception as e:
                logger.warning(f"⚠️ [RAG] risk_manager 知识检索失败: {e}")

        prompt = f"""作为风险管理委员会主席和辩论主持人，您的目标是评估三位风险分析师——激进、中性和安全/保守——之间的辩论，并确定交易员的最佳行动方案。您的决策必须产生明确的建议：买入、卖出或持有。只有在有具体论据强烈支持时才选择持有，而不是在所有方面都似乎有效时作为后备选择。力求清晰和果断。

决策指导原则：
1. **总结关键论点**：提取每位分析师的最强观点，重点关注与背景的相关性。
2. **提供理由**：用辩论中的直接引用和反驳论点支持您的建议。
3. **完善交易员计划**：从交易员的原始计划**{trader_plan}**开始，根据分析师的见解进行调整。
4. **从过去的错误中学习**：使用**{past_memory_str}**中的经验教训来解决先前的误判，改进您现在做出的决策，确保您不会做出错误的买入/卖出/持有决定而亏损。
{kb_context_str}
{risk_gate_str}

交付成果：
- 明确且可操作的建议：买入、卖出或持有。
- 基于辩论和过去反思的详细推理。

标的约束：
{instrument_context}

---

**分析师辩论历史：**
{history}

---

专注于可操作的见解和持续改进。建立在过去经验教训的基础上，批判性地评估所有观点，确保每个决策都能带来更好的结果。请用中文撰写所有分析内容和建议。"""

        # 📊 统计 prompt 大小
        prompt_length = len(prompt)
        # 粗略估算 token 数量（中文约 1.5-2 字符/token，英文约 4 字符/token）
        estimated_tokens = int(prompt_length / 1.8)  # 保守估计

        logger.info(f"📊 [Risk Manager] Prompt 统计:")
        logger.info(f"   - 辩论历史长度: {len(history)} 字符")
        logger.info(f"   - 交易员计划长度: {len(trader_plan)} 字符")
        logger.info(f"   - 历史记忆长度: {len(past_memory_str)} 字符")
        logger.info(f"   - 总 Prompt 长度: {prompt_length} 字符")
        logger.info(f"   - 估算输入 Token: ~{estimated_tokens} tokens")

        # 增强的LLM调用，包含错误处理和重试机制
        max_retries = 3
        retry_count = 0
        response_content = ""

        while retry_count < max_retries:
            try:
                logger.info(f"🔄 [Risk Manager] 调用LLM生成交易决策 (尝试 {retry_count + 1}/{max_retries})")

                # ⏱️ 记录开始时间
                start_time = time.time()

                response = llm.invoke(prompt)

                # ⏱️ 记录结束时间
                elapsed_time = time.time() - start_time
                
                if response and hasattr(response, 'content') and response.content:
                    response_content = response.content.strip()

                    # 📊 统计响应信息
                    response_length = len(response_content)
                    estimated_output_tokens = int(response_length / 1.8)

                    # 尝试获取实际的 token 使用情况（如果 LLM 返回了）
                    usage_info = ""
                    if hasattr(response, 'response_metadata') and response.response_metadata:
                        metadata = response.response_metadata
                        if 'token_usage' in metadata:
                            token_usage = metadata['token_usage']
                            usage_info = f", 实际Token: 输入={token_usage.get('prompt_tokens', 'N/A')} 输出={token_usage.get('completion_tokens', 'N/A')} 总计={token_usage.get('total_tokens', 'N/A')}"

                    logger.info(f"⏱️ [Risk Manager] LLM调用耗时: {elapsed_time:.2f}秒")
                    logger.info(f"📊 [Risk Manager] 响应统计: {response_length} 字符, 估算~{estimated_output_tokens} tokens{usage_info}")

                    if len(response_content) > 10:  # 确保响应有实质内容
                        logger.info(f"✅ [Risk Manager] LLM调用成功")
                        break
                    else:
                        logger.warning(f"⚠️ [Risk Manager] LLM响应内容过短: {len(response_content)} 字符")
                        response_content = ""
                else:
                    logger.warning(f"⚠️ [Risk Manager] LLM响应为空或无效")
                    response_content = ""

            except Exception as e:
                elapsed_time = time.time() - start_time
                logger.error(f"❌ [Risk Manager] LLM调用失败 (尝试 {retry_count + 1}): {str(e)}")
                logger.error(f"⏱️ [Risk Manager] 失败前耗时: {elapsed_time:.2f}秒")
                response_content = ""
            
            retry_count += 1
            if retry_count < max_retries and not response_content:
                logger.info(f"🔄 [Risk Manager] 等待2秒后重试...")
                time.sleep(2)
        
        # 如果所有重试都失败，生成默认决策
        if not response_content:
            logger.error(f"❌ [Risk Manager] 所有LLM调用尝试失败，使用默认决策")
            response_content = f"""**默认建议：持有**

由于技术原因无法生成详细分析，基于当前市场状况和风险控制原则，建议对{company_name}采取持有策略。

**理由：**
1. 市场信息不足，避免盲目操作
2. 保持现有仓位，等待更明确的市场信号
3. 控制风险，避免在不确定性高的情况下做出激进决策

**建议：**
- 密切关注市场动态和公司基本面变化
- 设置合理的止损和止盈位
- 等待更好的入场或出场时机

注意：此为系统默认建议，建议结合人工分析做出最终决策。"""

        new_risk_debate_state = {
            "judge_decision": response_content,
            "history": risk_debate_state["history"],
            "risky_history": risk_debate_state["risky_history"],
            "safe_history": risk_debate_state["safe_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_risky_response": risk_debate_state["current_risky_response"],
            "current_safe_response": risk_debate_state["current_safe_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        logger.info(f"📋 [Risk Manager] 最终决策生成完成，内容长度: {len(response_content)} 字符")

        # P2 (2026-08-02): 后置风控拦截 — block_buy时禁止买入建议
        if risk_gates["block_buy"]:
            # 检测LLM是否建议买入
            buy_keywords = ["买入", "建仓", "加仓", "买进"]
            if any(kw in response_content for kw in buy_keywords):
                logger.warning(
                    f"⛔ [Risk Manager] 风控拦截: block_buy=True "
                    f"(MoneyLosingScore={risk_gates['money_losing_score']}, "
                    f"MaxDrawdown={risk_gates['max_drawdown_pct']:.1f}%), "
                    f"LLM建议买入 → 降级为持有"
                )
                response_content = (
                    response_content +
                    f"\n\n---\n**⚠️ 风控系统拦截 (P2)**\n"
                    f"MoneyLosingScore={risk_gates['money_losing_score']}/5, "
                    f"MaxDrawdown={risk_gates['max_drawdown_pct']:.1f}%\n"
                    f"原建议包含买入操作, 因连续亏损/回撤超限, "
                    f"系统强制降级为**持有**, 建议等待风险释放后再评估。\n"
                    f"风控提示: {risk_gates['warning']}"
                )

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": response_content,
        }

    return risk_manager_node
