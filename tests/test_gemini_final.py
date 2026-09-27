#!/usr/bin/env python3
"""
最终测试修复后的Gemini集成
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

def test_gemini_tradingagents():
    """测试修复后的Gemini与TradingAgents集成"""
    try:
        print("🧪 测试修复后的Gemini与TradingAgents集成")
        print("=" * 60)
        
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        
        # 检查API密钥
        google_api_key = os.getenv('GOOGLE_API_KEY')
        if not google_api_key:
            print("❌ Google API密钥未配置")
            return False
        
        print(f"✅ Google API密钥已配置: {google_api_key[:20]}...")
        
        # 创建使用Gemini的配置
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "google"
        config["deep_think_llm"] = "gemini-2.5-flash-lite-preview-06-17"
        config["quick_think_llm"] = "gemini-2.5-flash-lite-preview-06-17"
        config["online_tools"] = True
        config["memory_enabled"] = True
        
        # 修复路径
        config["data_dir"] = str(project_root / "data")
        config["results_dir"] = str(project_root / "results")
        config["data_cache_dir"] = str(project_root / "tradingagents" / "dataflows" / "data_cache")
        
        # 创建目录
        os.makedirs(config["data_dir"], exist_ok=True)
        os.makedirs(config["results_dir"], exist_ok=True)
        os.makedirs(config["data_cache_dir"], exist_ok=True)
        
        print("✅ 配置创建成功")
        print(f"   LLM提供商: {config['llm_provider']}")
        print(f"   深度思考模型: {config['deep_think_llm']}")
        print(f"   快速思考模型: {config['quick_think_llm']}")
        
        # 创建TradingAgentsGraph实例
        print("🚀 初始化TradingAgents图...")
        graph = TradingAgentsGraph(["market"], config=config, debug=False)
        
        print("✅ TradingAgents图初始化成功")
        
        # 测试简单分析
        print("📊 开始股票分析...")
        print("   这可能需要几分钟时间...")
        
        try:
            from datetime import datetime, timezone
            _run_generated_at = datetime.now(timezone.utc)
            state, decision = graph.propagate("AAPL", "2025-06-27", generated_at=_run_generated_at)
            
            if state and decision:
                print("✅ Gemini驱动的股票分析成功完成！")
                print(f"   最终决策: {decision}")
                
                # 检查各种报告
                reports = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
                for report_name in reports:
                    if report_name in state and state[report_name]:
                        report_content = state[report_name]
                        print(f"   {report_name}: {len(report_content)} 字符")
                        print(f"   预览: {report_content[:100]}...")
                        print()
                
                return True
            else:
                print("❌ 分析完成但结果为空")
                return False
                
        except Exception as e:
            print(f"❌ 股票分析失败: {e}")
            import traceback
            print(traceback.format_exc())
            return False
            
    except Exception as e:
        print(f"❌ TradingAgents集成测试失败: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def test_gemini_basic():
    """基础Gemini功能测试"""
    try:
        print("🧪 基础Gemini功能测试")
        print("=" * 50)
        
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        # 创建LangChain Gemini实例
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-flash-lite-preview-06-17",
            temperature=0.1,
            max_tokens=500,
            google_api_key=os.getenv('GOOGLE_API_KEY')
        )
        
        print("✅ Gemini实例创建成功")
        
        # 测试中文对话
        print("📝 测试中文对话...")
        response = llm.invoke("请用中文分析一下当前人工智能技术的发展趋势")
        
        if response and response.content:
            print("✅ 中文对话测试成功")
            print(f"   响应长度: {len(response.content)} 字符")
            print(f"   响应预览: {response.content[:200]}...")
            return True
        else:
            print("❌ 中文对话测试失败")
            return False
            
    except Exception as e:
        print(f"❌ 基础功能测试失败: {e}")
        return False

def main():
    """主测试函数"""
    print("🧪 Gemini最终集成测试")
    print("=" * 70)
    
    # 检查环境变量
    google_api_key = os.getenv('GOOGLE_API_KEY')
    if not google_api_key:
        print("❌ Google API密钥未配置")
        print("💡 请在.env文件中设置 GOOGLE_API_KEY")
        return
    
    # 运行测试
    results = {}
    
    print("第1步: 基础功能测试")
    print("-" * 30)
    results['基础功能'] = test_gemini_basic()
    
    print("\n第2步: TradingAgents集成测试")
    print("-" * 30)
    results['TradingAgents集成'] = test_gemini_tradingagents()
    
    # 总结结果
    print(f"\n📊 最终测试结果总结:")
    print("=" * 50)
    
    for test_name, success in results.items():
        status = "✅ 通过" if success else "❌ 失败"
        print(f"  {test_name}: {status}")
    
    successful_tests = sum(results.values())
    total_tests = len(results)
    
    print(f"\n🎯 总体结果: {successful_tests}/{total_tests} 测试通过")
    
    if successful_tests == total_tests:
        print("🎉 Gemini模型完全集成成功！")
        print("\n💡 使用建议:")
        print("   1. 在Web界面中选择'Google'作为LLM提供商")
        print("   2. 使用模型名称: gemini-2.0-flash")
        print("   3. 可以进行完整的中文股票分析")
        print("   4. 支持所有分析师类型")
        print("   5. Gemini在多语言和推理能力方面表现优秀")
    elif successful_tests > 0:
        print("⚠️ Gemini部分功能可用")
        if results['基础功能'] and not results['TradingAgents集成']:
            print("💡 基础功能正常，但TradingAgents集成有问题")
            print("   建议检查配置和依赖")
    else:
        print("❌ Gemini模型不可用")
        print("💡 请检查API密钥、网络连接和依赖安装")

if __name__ == "__main__":
    main()
