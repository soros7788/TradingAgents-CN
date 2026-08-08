"""
外部知识库 Schema — 4 个 Collection 定义 + Agent 可访问范围 + 隔离规则

Collection 命名规则：
  - 外部知识库: kb_ 前缀 (kb_chanlun_rules, kb_trade_playbook, kb_case_review, kb_workflow_docs)
  - Agent 记忆: 无前缀 (bull_memory, bear_memory, trader_memory, invest_judge_memory, risk_manager_memory)

隔离原则:
  - KnowledgeRetriever 只能访问 kb_ 前缀的 collection
  - FinancialSituationMemory 只能访问无前缀的 agent memory collection
  - 两者互不可达
"""
import os

# ============================================================
#  外部知识库 Collection 定义
# ============================================================

EXTERNAL_KB_PREFIX = os.getenv("EXTERNAL_KB_PREFIX", "kb_")

# 4 个外部知识 collection 及其可访问的 agent 角色
EXTERNAL_KB_COLLECTIONS = {
    "kb_chanlun_rules": {
        "description": "缠论基础规则：分型/笔/线段/中枢/走势类型/背驰/盘整背驰/趋势背驰/二买三买/一二三卖",
        "source_types": ["python_rule", "markdown", "text"],
        "accessible_by": ["bull_researcher", "bear_researcher", "trader"],
    },
    "kb_trade_playbook": {
        "description": "系统交易手册：执行清单/仓位规则/止损规则/加减仓规则/禁入条件/操作模板",
        "source_types": ["excel", "markdown", "text"],
        "accessible_by": ["trader", "risk_manager", "research_manager"],
    },
    "kb_case_review": {
        "description": "历史案例：成功失败交易/周复盘/心态日志/候选池历史/典型结构",
        "source_types": ["excel", "text", "json"],
        "accessible_by": ["bull_researcher", "bear_researcher", "trader", "risk_manager"],
    },
    "kb_workflow_docs": {
        "description": "工作流文档：daily_workflow命令说明/compliance规则/scan流程/intraday逻辑",
        "source_types": ["python_code", "markdown"],
        "accessible_by": ["trader", "risk_manager", "research_manager"],
    },
}

# ============================================================
#  Agent 记忆 Collection（现有，不可被 KnowledgeRetriever 访问）
# ============================================================

AGENT_MEMORY_COLLECTIONS = {
    "bull_memory",
    "bear_memory",
    "trader_memory",
    "invest_judge_memory",
    "risk_manager_memory",
}

# ============================================================
#  Metadata 字段规范
# ============================================================

# 所有外部知识 chunk 的 metadata 必填字段
REQUIRED_METADATA_FIELDS = [
    "source_type",      # python_rule / excel / markdown / model_metadata / python_code
    "source_path",      # 原始文件路径
    "domain",           # chanlun / trading_params / case_review / workflow / chanlun_ml
    "version",          # phase1 / phase2 等
    "chunk_index",      # 切分后的序号
    "content_hash",     # 内容哈希，用于去重
    "created_by",       # external_kb_ingestion
]

# Optional metadata 字段
OPTIONAL_METADATA_FIELDS = [
    "rule_name",        # 规则名称（kb_chanlun_rules）
    "sheet_name",       # Excel工作表名（kb_trade_playbook / kb_case_review）
    "row_start",        # Excel起始行
    "row_end",          # Excel结束行
    "model_name",       # 模型名（kb_ml_model_docs）
    "model_type",       # 模型类型
    "command",          # 命令名（kb_workflow_docs）
]


def get_agent_accessible_collections(agent_role: str) -> list:
    """获取指定 agent 角色可访问的外部知识 collection 列表"""
    accessible = []
    for coll_name, config in EXTERNAL_KB_COLLECTIONS.items():
        if agent_role in config.get("accessible_by", []):
            accessible.append(coll_name)
    return accessible


def is_agent_memory(collection_name: str) -> bool:
    """判断 collection 是否属于 agent 记忆（禁止 KnowledgeRetriever 访问）"""
    return collection_name in AGENT_MEMORY_COLLECTIONS


def is_external_kb(collection_name: str) -> bool:
    """判断 collection 是否属于外部知识库"""
    return collection_name.startswith(EXTERNAL_KB_PREFIX)
