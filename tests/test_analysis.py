#!/usr/bin/env python3
"""
简化的分析测试脚本
用于验证TradingAgents核心功能是否正常工作
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# 添加项目根目录到Python路径
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

# 加载环境变量
load_dotenv(project_root / ".env", override=True)

def test_basic_imports():
    """测试基本导入"""
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        print("✅ 基本导入成功")
        return True
    except Exception as e:
        print(f"❌ 基本导入失败: {e}")
        return False

def test_environment_variables():
    """测试环境变量"""
    dashscope_key = os.getenv("DASHSCOPE_API_KEY")
    finnhub_key = os.getenv("FINNHUB_API_KEY")
    
    print(f"DASHSCOPE_API_KEY: {'已设置' if dashscope_key else '未设置'}")
    print(f"FINNHUB_API_KEY: {'已设置' if finnhub_key else '未设置'}")
    
    return bool(dashscope_key and finnhub_key)

def test_graph_initialization():
    """测试图初始化"""
    try:
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        
        # 创建配置
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "dashscope"
        config["deep_think_llm"] = "qwen-plus"
        config["quick_think_llm"] = "qwen-plus"
        config["memory_enabled"] = True
        config["online_tools"] = True
        
        # 修复路径
        config["data_dir"] = str(project_root / "data")
        config["results_dir"] = str(project_root / "results")
        config["data_cache_dir"] = str(project_root / "tradingagents" / "dataflows" / "data_cache")
        
        # 创建目录
        os.makedirs(config["data_dir"], exist_ok=True)
        os.makedirs(config["results_dir"], exist_ok=True)
        os.makedirs(config["data_cache_dir"], exist_ok=True)
        
        # 初始化图
        graph = TradingAgentsGraph(["market"], config=config, debug=True)
        print("✅ 图初始化成功")
        return True, graph
    except Exception as e:
        print(f"❌ 图初始化失败: {e}")
        import traceback
        print(traceback.format_exc())
        return False, None

def test_simple_analysis():
    """测试简单分析"""
    success, graph = test_graph_initialization()
    if not success:
        return False
    
    try:
        print("🚀 开始简单分析测试...")
        # 执行简单分析
        from datetime import datetime, timezone
        _run_generated_at = datetime.now(timezone.utc)
        state, decision = graph.propagate("AAPL", "2025-06-27", generated_at=_run_generated_at)
        print("✅ 分析完成")
        print(f"决策: {decision}")
        return True
    except Exception as e:
        print(f"❌ 分析失败: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def main():
    """主测试函数"""
    print("🧪 TradingAgents 功能测试")
    print("=" * 50)
    
    # 测试基本导入
    print("\n1. 测试基本导入...")
    if not test_basic_imports():
        return
    
    # 测试环境变量
    print("\n2. 测试环境变量...")
    if not test_environment_variables():
        print("❌ 环境变量未正确配置")
        return
    
    # 测试图初始化
    print("\n3. 测试图初始化...")
    success, graph = test_graph_initialization()
    if not success:
        return
    
    # 测试简单分析
    print("\n4. 测试简单分析...")
    if test_simple_analysis():
        print("\n🎉 所有测试通过！")
    else:
        print("\n❌ 分析测试失败")

if __name__ == "__main__":
    main()
