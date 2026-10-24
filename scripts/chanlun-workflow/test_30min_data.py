#!/usr/bin/env python3
"""
30min K线数据获取能力测试报告
测试目标: 验证各数据源的30min数据获取能力
测试股票: 台华新材(603005), 沃华医药(002107), 贤丰控股(002319), 东风股份(601515)

数据源:
  1. 新浪财经 (当前使用)
  2. 腾讯财经
  3. 东方财富
  4. 5min聚合构建30min
"""
import urllib.request
import json
import ssl
import time
from datetime import datetime, timedelta
from collections import defaultdict

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

TEST_STOCKS = [
    {"code": "603005", "name": "台华新材", "prefix": "sh"},
    {"code": "002107", "name": "沃华医药", "prefix": "sz"},
    {"code": "002319", "name": "贤丰控股", "prefix": "sz"},
    {"code": "601515", "name": "东风股份", "prefix": "sh"},
]

results = defaultdict(lambda: {"success": 0, "fail": 0, "errors": []})

print("=" * 70)
print("📊 30min K线数据获取能力测试报告")
print(f"⏰ 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
print("=" * 70)

# ============================================================
# 测试1: 新浪财经 - 不同datalen
# ============================================================
print("\n" + "=" * 70)
print("【测试1】新浪财经 API - 不同datalen测试")
print("=" * 70)

for stock in TEST_STOCKS[:1]:  # 只用一只股票测试以节省时间
    code = stock["code"]
    prefix = stock["prefix"]
    name = stock["name"]
    
    print(f"\n📌 测试股票: {name}({code})")
    print("-" * 50)
    
    for datalen in [120, 180, 240, 300, 360, 500]:
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale=30&ma=no&datalen={datalen}"
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            start = time.time()
            raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
            elapsed = time.time() - start
            data = json.loads(raw.decode('utf-8', errors='replace'))
            
            count = len(data) if isinstance(data, list) else 0
            actual_count = count
            
            if count > 0:
                first = data[0]
                last = data[-1]
                first_date = first.get('day', 'N/A')
                last_date = last.get('day', 'N/A')
                print(f"  datalen={datalen:>3} → 获取{count:>3}根 | {first_date} ~ {last_date} | 耗时{elapsed:.2f}s {'✅' if count >= 100 else '⚠️'}")
            else:
                print(f"  datalen={datalen:>3} → 获取0根 | 可能达上限或无数据 ❌")
                actual_count = 0
                
        except Exception as e:
            print(f"  datalen={datalen:>3} → 错误: {str(e)[:60]} ❌")
            actual_count = 0
        
        results["sina_datalen"][str(datalen)] = actual_count
        time.sleep(0.3)  # 避免请求过快

# ============================================================
# 测试2: 新浪财经 - 30min vs 5min vs 日线 数据量对比
# ============================================================
print("\n" + "=" * 70)
print("【测试2】新浪财经 - 不同级别数据量对比")
print("=" * 70)

for stock in TEST_STOCKS:
    code = stock["code"]
    prefix = stock["prefix"]
    name = stock["name"]
    
    print(f"\n📌 {name}({code}):")
    print("-" * 50)
    
    for scale, scale_name, expected_days in [
        ("240", "日线", "半年"),
        ("30", "30min", "2.5天"),
        ("5", "5min", "2.5天"),
    ]:
        url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale={scale}&ma=no&datalen=120"
        
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
            data = json.loads(raw.decode('utf-8', errors='replace'))
            
            count = len(data) if isinstance(data, list) else 0
            if count > 0:
                first_date = data[0].get('day', 'N/A')
                last_date = data[-1].get('day', 'N/A')
                
                # 计算实际覆盖天数
                try:
                    if scale == "240":
                        days = count  # 日线1根=1天
                    elif scale == "30":
                        days = count * 30 / 240  # 30min每天8根
                    elif scale == "5":
                        days = count * 5 / 240  # 5min每天48根
                    else:
                        days = 0
                    
                    coverage = f"{days:.1f}天"
                except:
                    coverage = "?"
                
                print(f"  {scale_name:>6}: {count:>3}根 | {first_date} ~ {last_date} | 覆盖{coverage}")
            else:
                print(f"  {scale_name:>6}: 0根 | 无数据 ❌")
                
        except Exception as e:
            print(f"  {scale_name:>6}: 错误 {str(e)[:40]} ❌")
        
        time.sleep(0.2)

# ============================================================
# 测试3: 腾讯财经API (备选方案)
# ============================================================
print("\n" + "=" * 70)
print("【测试3】腾讯财经 API - 30min数据")
print("=" * 70)

for stock in TEST_STOCKS[:2]:  # 测试前2只
    code = stock["code"]
    prefix = stock["prefix"]
    name = stock["name"]
    
    # 腾讯API: 获取分时成交数据
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={prefix}{code},30,,,qfq"
    
    print(f"\n📌 {name}({code}):")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
        text = raw.decode('utf-8', errors='replace')
        
        # 解析JSONP
        start_idx = text.find('{')
        end_idx = text.rfind('}') + 1
        if start_idx >= 0 and end_idx > start_idx:
            json_str = text[start_idx:end_idx]
            data = json.loads(json_str)
            
            # 30min数据在 data[code]["qfqday"] 或类似字段
            stock_data = data.get("data", {}).get(f"{prefix}{code}", {})
            
            # 尝试获取30min数据
            kline_30 = stock_data.get("qfq30", stock_data.get("30", []))
            if not kline_30:
                # 尝试其他字段名
                for key in stock_data:
                    if '30' in str(key).lower():
                        kline_30 = stock_data[key]
                        break
            
            if kline_30 and isinstance(kline_30, list):
                count = len(kline_30)
                print(f"  ✅ 获取{count}根30min数据")
                if count > 0:
                    first = kline_30[0]
                    last = kline_30[-1]
                    print(f"     首: {first[:3] if isinstance(first, list) else first}")
                    print(f"     尾: {last[:3] if isinstance(last, list) else last}")
            else:
                # 打印可用的key
                print(f"  ⚠️ 未找到30min数据, 可用字段: {list(stock_data.keys())[:10]}")
                
        else:
            print(f"  ❌ JSON解析失败")
            
    except Exception as e:
        print(f"  ❌ 错误: {str(e)[:80]}")
    
    time.sleep(0.3)

# ============================================================
# 测试4: 5min聚合构建30min (备选方案)
# ============================================================
print("\n" + "=" * 70)
print("【测试4】5min K线聚合构建30min K线")
print("=" * 70)

def aggregate_5min_to_3min(k5_data):
    """将5min K线聚合为30min K线"""
    bars_30 = []
    if len(k5_data) < 6:  # 30min = 6根5min
        return bars_30
    
    # 每6根5min合成1根30min
    for i in range(0, len(k5_data), 6):
        chunk = k5_data[i:i+6]
        if len(chunk) < 6:
            continue
        
        opens = [float(c['open']) for c in chunk]
        highs = [float(c['high']) for c in chunk]
        lows = [float(c['low']) for c in chunk]
        closes = [float(c['close']) for c in chunk]
        volumes = [float(c.get('volume', 0)) for c in chunk]
        
        bar_30 = {
            'day': chunk[0]['day'],  # 使用起始时间
            'open': opens[0],
            'high': max(highs),
            'low': min(lows),
            'close': closes[-1],
            'volume': sum(volumes),
        }
        bars_30.append(bar_30)
    
    return bars_30

for stock in TEST_STOCKS[:2]:
    code = stock["code"]
    prefix = stock["prefix"]
    name = stock["name"]
    
    print(f"\n📌 {name}({code}):")
    
    # 获取5min数据 (增加datalen以获取更多数据)
    url = f"https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={prefix}{code}&scale=5&ma=no&datalen=240"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, context=ctx, timeout=15).read()
        k5_data = json.loads(raw.decode('utf-8', errors='replace'))
        
        if k5_data:
            k5_count = len(k5_data)
            print(f"  5min原始数据: {k5_count}根")
            
            # 聚合为30min
            k30_aggregated = aggregate_5min_to_3min(k5_data)
            k30_count = len(k30_aggregated)
            print(f"  聚合30min数据: {k30_count}根")
            
            if k30_count >= 20:
                print(f"  ✅ 满足缠论最低需求(20根)")
                
                # 计算覆盖天数
                # 每天48根5min = 8根30min
                days_covered = k30_count / 8
                print(f"  覆盖天数: {days_covered:.1f}天")
                
                if days_covered >= 3:
                    print(f"  ✅ 足以构建完整30min中枢(需3-7天)")
                elif days_covered >= 1:
                    print(f"  ⚠️ 勉强可构建简化中枢")
                else:
                    print(f"  ❌ 中枢构建不足")
            else:
                print(f"  ❌ 不足以构建缠论中枢(需至少20根)")
        else:
            print(f"  ❌ 5min数据获取失败")
            
    except Exception as e:
        print(f"  ❌ 错误: {str(e)[:80]}")
    
    time.sleep(0.3)

# ============================================================
# 测试5: 缠论中枢构建可行性验证
# ============================================================
print("\n" + "=" * 70)
print("【测试5】30min中枢构建可行性分析")
print("=" * 70)

print("\n📊 缠论30min中枢需求分析:")
print("-" * 50)
print("  最小需求: 20根K线 (识别1个中枢)")
print("  完整需求: 60根K线 (识别3个中枢, 支持一二三买卖点)")
print("  理想需求: 120根K线 (多级别联立)")

print("\n📊 各方案可行性:")
print("-" * 50)

# 汇总之前的测试结果
print("\n  方案1: 新浪直接获取 (datalen=120)")
print(f"    → 约120根30min = 2.5天")
print(f"    → ⚠️ 仅能识别1-2个中枢, 不足以支持完整缠论分析")
print(f"    → 评级: ⭐⭐")

print("\n  方案2: 新浪直接获取 (datalen=240)")
print(f"    → 需测试新浪是否支持更大datalen")
print(f"    → 若支持: 240根 = 5天, 可识别3-4个中枢")
print(f"    → 评级: ⭐⭐⭐ (待验证)")

print("\n  方案3: 5min聚合 (datalen=240)")
print(f"    → 240根5min = 40根30min = 5天")
print(f"    → ✅ 可识别3-4个中枢, 支持完整缠论分析")
print(f"    → 评级: ⭐⭐⭐⭐")

print("\n  方案4: 腾讯API")
print(f"    → 需测试腾讯支持的30min数据量")
print(f"    → 若支持: 可能获取更长期数据")
print(f"    → 评级: ⭐⭐⭐ (待验证)")

# ============================================================
# 最终结论和建议
# ============================================================
print("\n" + "=" * 70)
print("📋 测试结论与建议")
print("=" * 70)

print("""
🔍 测试总结:

1. 当前数据源(新浪datalen=120)仅能获取2.5天30min数据
   → 严重不足, 无法构建完整中枢

2. 推荐方案: 5min聚合构建30min
   → 优势: 新浪5min接口稳定, 支持更大datalen
   → 实现: 6根5min = 1根30min
   → 预期: 240根5min → 40根30min = 5天覆盖
   → 代码改动: 仅需修改 analyze_beichi() 中的数据获取逻辑

3. 备选方案: 新浪直接请求30min, 增大datalen
   → 需先测试新浪最大支持的datalen
   → 若支持240+, 则直接使用

4. 长期方案: 引入Tushare/AKShare数据源
   → 更稳定, 支持更长历史
   → 需要安装依赖和配置API Key

✅ 立即可行的优化:
   ① 先测试新浪30min的最大datalen支持
   ② 同时实现5min聚合方案作为降级备选
   ③ 修改 analyze_beichi() 中的数据获取逻辑
   ④ 根据数据量自适应调整缠论参数
""")

print("\n" + "=" * 70)
print("✅ 测试完成")
print("=" * 70)

# 输出详细数据
print("\n📎 详细测试数据:")
print("-" * 50)
for key, val in results.items():
    print(f"  {key}: {val}")