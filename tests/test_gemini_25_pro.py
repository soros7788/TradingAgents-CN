#!/usr/bin/env python3
"""
测试Gemini 2.5 Pro模型
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

def test_gemini_25_pro_basic():
    """测试Gemini 2.5 Pro基础功能"""
    try:
        print("🧪 测试Gemini 2.5 Pro基础功能")
        print("=" * 60)
        
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        # 检查API密钥
        google_api_key = os.getenv('GOOGLE_API_KEY')
        if not google_api_key:
            print("❌ Google API密钥未配置")
            return False
        
        print(f"✅ Google API密钥已配置: {google_api_key[:20]}...")
        
        # 创建Gemini 2.5 Pro实例
        print("🚀 创建Gemini 2.5 Pro实例...")
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-pro",
            temperature=0.1,
            max_tokens=1500,
            google_api_key=google_api_key
        )
        
        print("✅ Gemini 2.5 Pro实例创建成功")
        
        # 测试中文股票分析
        print("📊 测试中文股票分析...")
        response = llm.invoke("""
        请用中文分析苹果公司(AAPL)的投资价值。请从以下几个方面进行分析：
        
        1. 公司基本面分析
        2. 技术创新能力
        3. 市场竞争地位
        4. 财务健康状况
        5. 投资风险评估
        6. 投资建议
        
        请提供详细的分析和推理过程。
        """)
        
        if response and response.content:
            print("✅ 中文股票分析成功")
            print(f"   响应长度: {len(response.content)} 字符")
            print(f"   响应预览: {response.content[:300]}...")
            return True
        else:
            print("❌ 中文股票分析失败")
            return False
            
    except Exception as e:
        print(f"❌ Gemini 2.5 Pro基础测试失败: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def test_gemini_25_pro_tradingagents():
    """测试Gemini 2.5 Pro在TradingAgents中的使用"""
    try:
        print("\n🧪 测试Gemini 2.5 Pro在TradingAgents中的使用")
        print("=" * 60)
        
        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        
        # 创建配置
        config = DEFAULT_CONFIG.copy()
        config["llm_provider"] = "google"
        config["deep_think_llm"] = "gemini-2.5-pro"
        config["quick_think_llm"] = "gemini-2.5-pro"
        config["online_tools"] = False  # 避免API限制
        config["memory_enabled"] = True  # 启用内存功能
        config["max_debate_rounds"] = 1
        config["max_risk_discuss_rounds"] = 1
        
        # 修复路径
        config["data_dir"] = str(project_root / "data")
        config["results_dir"] = str(project_root / "results")
        config["data_cache_dir"] = str(project_root / "tradingagents" / "dataflows" / "data_cache")
        
        # 创建目录
        os.makedirs(config["data_dir"], exist_ok=True)
        os.makedirs(config["results_dir"], exist_ok=True)
        os.makedirs(config["data_cache_dir"], exist_ok=True)
        
        print("✅ 配置创建成功")
        print(f"   模型: gemini-2.5-pro")
        print(f"   内存功能: {config['memory_enabled']}")
        
        # 创建TradingAgentsGraph实例
        print("🚀 初始化TradingAgents图...")
        graph = TradingAgentsGraph(["market"], config=config, debug=False)
        
        print("✅ TradingAgents图初始化成功")
        
        # 测试分析
        print("📊 开始Gemini 2.5 Pro股票分析...")
        print("   这可能需要几分钟时间...")
        
        try:
            from datetime import datetime, timezone
            _run_generated_at = datetime.now(timezone.utc)
            state, decision = graph.propagate("AAPL", "2025-06-27", generated_at=_run_generated_at)
            
            if state and decision:
                print("✅ Gemini 2.5 Pro驱动的股票分析成功！")
                print(f"   最终决策: {decision}")
                
                # 检查各种报告
                reports = ["market_report", "sentiment_report", "news_report", "fundamentals_report"]
                for report_name in reports:
                    if report_name in state and state[report_name]:
                        report_content = state[report_name]
                        print(f"   {report_name}: {len(report_content)} 字符")
                        if len(report_content) > 100:
                            print(f"   预览: {report_content[:150]}...")
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
        print(f"❌ TradingAgents测试失败: {e}")
        import traceback
        print(traceback.format_exc())
        return False

def test_gemini_25_pro_complex_reasoning():
    """测试Gemini 2.5 Pro的复杂推理能力"""
    try:
        print("\n🧪 测试Gemini 2.5 Pro复杂推理能力")
        print("=" * 60)
        
        from langchain_google_genai import ChatGoogleGenerativeAI
        
        # 创建实例
        llm = ChatGoogleGenerativeAI(
            model="gemini-2.5-pro",
            temperature=0.1,
            max_tokens=2000,
            google_api_key=os.getenv('GOOGLE_API_KEY')
        )
        
        # 复杂推理测试
        complex_prompt = """
        请进行复杂的投资分析推理：
        
        场景设定：
        - 时间：2025年6月
        - 美联储政策：刚刚降息25个基点
        - 通胀率：2.8%，呈下降趋势
        - 中美关系：贸易紧张局势有所缓解
        - AI发展：ChatGPT和其他AI工具快速普及
        - 地缘政治：俄乌冲突持续，中东局势紧张
        
        请分析在这种复杂的宏观环境下，以下三只股票的投资价值排序：
        1. 苹果公司(AAPL) - 消费电子+AI
        2. 英伟达(NVDA) - AI芯片领导者
        3. 微软(MSFT) - 云计算+AI软件
        
        要求：
        1. 分析每只股票在当前环境下的优势和劣势
        2. 考虑宏观经济因素对各股票的影响
        3. 评估AI发展对各公司的长期影响
        4. 提供投资优先级排序和理由
        5. 给出具体的投资建议和风险提示
        
        请用中文提供详细的逻辑推理过程。
        """
        
        print("🧠 开始复杂推理测试...")
        response = llm.invoke(complex_prompt)
        
        if response and response.content and len(response.content) > 800:
            print("✅ 复杂推理测试成功")
            print(f"   响应长度: {len(response.content)} 字符")
            print(f"   响应预览: {response.content[:400]}...")
            return True
        else:
            print("❌ 复杂推理测试失败：响应过短或无内容")
            return False
            
    except Exception as e:
        print(f"❌ 复杂推理测试失败: {e}")
        return False

def main():
    """主测试函数"""
    print("🧪 Gemini 2.5 Pro完整测试")
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
    results['基础功能'] = test_gemini_25_pro_basic()
    
    print("\n第2步: 复杂推理测试")
    print("-" * 30)
    results['复杂推理'] = test_gemini_25_pro_complex_reasoning()
    
    print("\n第3步: TradingAgents集成测试")
    print("-" * 30)
    results['TradingAgents集成'] = test_gemini_25_pro_tradingagents()
    
    # 总结结果
    print(f"\n📊 Gemini 2.5 Pro测试结果总结:")
    print("=" * 50)
    
    for test_name, success in results.items():
        status = "✅ 通过" if success else "❌ 失败"
        print(f"  {test_name}: {status}")
    
    successful_tests = sum(results.values())
    total_tests = len(results)
    
    print(f"\n🎯 总体结果: {successful_tests}/{total_tests} 测试通过")
    
    if successful_tests == total_tests:
        print("🎉 Gemini 2.5 Pro完全可用！")
        print("\n💡 Gemini 2.5 Pro优势:")
        print("   🧠 更强的推理能力")
        print("   📊 更好的复杂分析")
        print("   🌍 优秀的多语言支持")
        print("   💰 更准确的金融分析")
        print("   🔍 更深入的洞察力")
        print("\n🚀 使用建议:")
        print("   1. 在Web界面中选择'Google'作为LLM提供商")
        print("   2. 使用模型名称: gemini-2.5-pro")
        print("   3. 适合复杂的投资分析任务")
        print("   4. 可以处理多因素综合分析")
    elif successful_tests >= 2:
        print("⚠️ Gemini 2.5 Pro大部分功能可用")
        print("💡 可以用于基础分析，部分高级功能可能需要调整")
    else:
        print("❌ Gemini 2.5 Pro不可用")
        print("💡 请检查API密钥权限和网络连接")

if __name__ == "__main__":
    main()
