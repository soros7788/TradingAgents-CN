#!/usr/bin/env python3
"""
合规审查 + 明日计划 2026-08-09
基于华宝证券持仓快照, 对照交易系统规则逐项审查

持仓:
  台华新材(603005) 300股 成本8.387 现价8.200  盈亏-2.227%
  沃华医药(002107) 200股 成本5.818 现价6.300  盈亏+8.281%
  贤丰控股(002319) 200股 成本5.515 现价6.110  盈亏+10.788%
  东风股份(601515) 100股 成本8.711 现价5.400  盈亏-38.007%

账户:
  总资产 21,893.54 | 持仓25% | 可用16,411.54 | 市值5,482.00
"""
from datetime import datetime, timedelta
from textwrap import dedent

holdings = [
    {"code": "603005", "name": "台华新材", "shares": 300, "cost": 8.387, "close": 8.200, "mv": 2460, "waived": False},
    {"code": "002107", "name": "沃华医药", "shares": 200, "cost": 5.818, "close": 6.300, "mv": 1260, "waived": False},
    {"code": "002319", "name": "贤丰控股", "shares": 200, "cost": 5.515, "close": 6.110, "mv": 1222, "waived": False},
    {"code": "601515", "name": "东风股份", "shares": 100, "cost": 8.711, "close": 5.400, "mv": 540, "waived": True},
]

total_assets = 21893.54
position_pct = 0.25
available = 16411.54
market_value = 5482.00

# 计算各持仓比例
for h in holdings:
    h["pnl_pct"] = (h["close"] - h["cost"]) / h["cost"]
    h["position_pct"] = h["mv"] / total_assets

issues = []
warnings = []
plan = []

print("=" * 60)
print(f"📝 合规审查报告  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("=" * 60)

# =============================================
# 审查1: 东风股份 -38% 亏损超限
# =============================================
print("\n【审查1】亏损幅度合规")
print("-" * 40)

for h in holdings:
    pct = h["pnl_pct"]
    if pct < -0.20:
        issues.append(f"🔴 {h['name']}({h['code']}) 亏损{ pct*100:.1f}% 超过20%阈值, 严重违规")
        print(f"  🔴 {h['name']} 亏损 {pct*100:.1f}% — 严重违规")
    elif pct < -0.10:
        issues.append(f"🟡 {h['name']}({h['code']}) 亏损{pct*100:.1f}% 超过10%警戒线")
        print(f"  🟡 {h['name']} 亏损 {pct*100:.1f}% — 警戒")
    elif pct < -0.05:
        warnings.append(f"⚠️ {h['name']}({h['code']}) 亏损{pct*100:.1f}% 接近5%止损线")
        print(f"  ⚠️ {h['name']} 亏损 {pct*100:.1f}% — 关注")

# =============================================
# 审查2: 东风股份 WAIVED但亏损仍在扩大
# =============================================
print("\n【审查2】WAIVED持仓管理")
print("-" * 40)

waived_holdings = [h for h in holdings if h["waived"]]
for h in waived_holdings:
    if h["pnl_pct"] < -0.20:
        issues.append(f"🔴 {h['name']}({h['code']}) 已WAIVED但亏损扩大至{h['pnl_pct']*100:.1f}%, 建议重新评估或强制清仓")
        print(f"  🔴 {h['name']} WAIVED但亏损扩大 — 建议强制清仓")
    elif h["pnl_pct"] < 0:
        warnings.append(f"⚠️ {h['name']}({h['code']}) WAIVED中, 仍亏损{h['pnl_pct']*100:.1f}%")
        print(f"  ⚠️ {h['name']} WAIVED中, 仍亏损")

# =============================================
# 审查3: 仓位集中度
# =============================================
print("\n【审查3】仓位集中度")
print("-" * 40)

for h in holdings:
    pct = h["position_pct"] * 100
    tier = "核心仓" if pct >= 20 else "观察仓" if pct >= 10 else "边缘仓"
    print(f"  {h['name']}({h['code']}): {pct:.1f}% [{tier}]")
    if pct > 35:
        issues.append(f"🔴 {h['name']}仓位{pct:.1f}%超过35%上限")

total_pos = sum(h["position_pct"] for h in holdings) * 100
print(f"  合计持仓: {total_pos:.1f}% | 账户显示: {position_pct*100:.1f}%")
if total_pos > 50:
    warnings.append(f"⚠️ 持仓合计{total_pos:.1f}%偏高, 注意分散")

# =============================================
# 审查4: 浮盈股止盈纪律
# =============================================
print("\n【审查4】浮盈股止盈纪律")
print("-" * 40)

for h in holdings:
    pct = h["pnl_pct"]
    if pct > 0.10:
        print(f"  🏆 {h['name']} 浮盈 {pct*100:.1f}% — 建议设置移动止盈")
        warnings.append(f"⚠️ {h['name']} 浮盈{pct*100:.1f}%, 应设置移动止盈(如回撤5%离场)")
    elif pct > 0.05:
        print(f"  ✅ {h['name']} 浮盈 {pct*100:.1f}% — 持有观察")

# =============================================
# 审查5: 成本-现价破位检查 (策略C)
# =============================================
print("\n【审查5】成本-破位级别匹配 (策略C)")
print("-" * 40)

# 基于成本价反推买点级别
# 假设: 原始一买 ≈ 成本价附近, 若现价远低于成本 → 日线级别破位
for h in holdings:
    if h["waived"]:
        continue
    cost = h["cost"]
    close = h["close"]
    diff_pct = (close - cost) / cost * 100

    if diff_pct < -20:
        issues.append(f"🔴 {h['name']}({h['code']}) 现价{close:.2f}远低于成本{cost:.2f}(跌{diff_pct:+.1f}%), 日线级别破位, 应清仓")
        print(f"  🔴 {h['name']} 日线破位 (成本{cost:.2f}→现价{close:.2f})")
    elif diff_pct < -10:
        issues.append(f"🟡 {h['name']}({h['code']}) 跌破成本{diff_pct:.1f}%, 30min级别可能已破位")
        print(f"  🟡 {h['name']} 跌破成本 {diff_pct:.1f}%")

# =============================================
# 审查6: 一买低点破位检查
# =============================================
print("\n【审查6】一买低点破位检查")
print("-" * 40)

for h in holdings:
    if h["pnl_pct"] < -0.05:
        # 假设一买低点 ≈ 成本价 * 0.95 (保守估计)
        est_one_buy_low = h["cost"] * 0.95
        if h["close"] < est_one_buy_low:
            pct_below = (h["close"] - est_one_buy_low) / est_one_buy_low * 100
            issues.append(f"🔴 {h['name']}({h['code']}) 现价{h['close']:.2f}<估算一买低{est_one_buy_low:.2f}(跌{pct_below:+.1f}%), 二买确认失败, 应清仓")
            print(f"  🔴 {h['name']} 破一买低点 (估算低={est_one_buy_low:.2f}, 现价={h['close']:.2f})")
        else:
            dist = (h["close"] - est_one_buy_low) / est_one_buy_low * 100
            print(f"  ⚠️ {h['name']} 距估算一买低点 {dist:+.1f}%")

# =============================================
# 审查7: 账户风控指标
# =============================================
print("\n【审查7】账户风控指标")
print("-" * 40)

total_pnl_pct = sum(h["pnl_pct"] * h["mv"] for h in holdings) / total_assets * 100
print(f"  加权盈亏: {total_pnl_pct:+.2f}%")
print(f"  持仓比例: {position_pct*100:.1f}%")
print(f"  可用资金: {available:,.2f}")

if position_pct > 0.50:
    warnings.append("⚠️ 持仓比例超50%, 建议减仓")
elif position_pct < 0.10:
    warnings.append("⚠️ 持仓比例过低, 资金利用不足")

max_single_loss = min(h["pnl_pct"] for h in holdings)
if max_single_loss < -0.30:
    issues.append(f"🔴 单只最大亏损{max_single_loss*100:.1f}%, 账户风险敞口过大")

# =============================================
# 审查8: 新功能 - 卖点合规 (2026-08-09新增)
# =============================================
print("\n【审查8】卖点信号合规 (一二三卖检测)")
print("-" * 40)

for h in holdings:
    pct = h["pnl_pct"]
    if h["waived"]:
        continue

    # 根据盈亏和位置判断可能的卖点
    if pct < -0.30:
        print(f"  🔴 {h['name']} 深度亏损{pct*100:.1f}% → 应触发三卖(清仓)或止损")
        issues.append(f"🔴 {h['name']}({h['code']}) 深度亏损, 应触发缠论三卖或强制止损")
    elif pct < -0.10:
        print(f"  🟡 {h['name']} 亏损{pct*100:.1f}% → 检查是否触发二卖或一卖破位")
        warnings.append(f"⚠️ {h['name']} 亏损中, 检查缠论卖点信号")
    elif pct > 0.10:
        print(f"  🏆 {h['name']} 浮盈{pct*100:.1f}% → 关注一卖/二卖信号锁定利润")
    elif pct > 0:
        print(f"  ✅ {h['name']} 浮盈{pct*100:.1f}% → 持有, 关注卖点信号")

# =============================================
# 合规结论
# =============================================
print("\n" + "=" * 60)
print("📋 合规审查结论")
print("=" * 60)

if issues:
    print(f"\n🔴 严重问题 ({len(issues)}项):")
    for i, iss in enumerate(issues, 1):
        print(f"  {i}. {iss}")

if warnings:
    print(f"\n🟡 警告 ({len(warnings)}项):")
    for i, w in enumerate(warnings, 1):
        print(f"  {i}. {w}")

if not issues and not warnings:
    print("\n✅ 所有持仓合规, 无异常")

# =============================================
# 明日交易计划
# =============================================
print("\n" + "=" * 60)
print(f"📅 明日交易计划  {datetime.now().strftime('%Y-%m-%d')}")
print("=" * 60)

tomorrow = datetime.now() + timedelta(days=1)
print(f"\n日期: {tomorrow.strftime('%Y-%m-%d (%A)')}")

print("\n【持仓操作计划】")
print("-" * 40)

for h in holdings:
    pct = h["pnl_pct"]
    code = h["code"]
    name = h["name"]

    if pct < -0.30:
        action = "🚨 强制清仓"
        reason = f"亏损{pct*100:.1f}%超30%, 严重违反风控纪律"
    elif pct < -0.10:
        action = "⚠️ 减仓50%或清仓"
        reason = f"亏损{pct*100:.1f}%超10%, 缠论卖点待确认"
    elif pct < -0.02:
        action = "👀 观察, 设置止损"
        reason = f"亏损{pct*100:.1f}%, 设置止损于成本价下方5%"
    elif pct > 0.10:
        action = "🏆 移动止盈"
        reason = f"浮盈{pct*100:.1f}%, 设置回撤5%止盈保护利润"
    elif pct > 0:
        action = "✅ 持有观察"
        reason = f"浮盈{pct*100:.1f}%, 关注缠论一卖/二卖信号"
    else:
        action = "⚠️ 关注"
        reason = f"盈亏持平, 关注趋势变化"

    print(f"\n  {name}({code}):")
    print(f"    现价: {h['close']:.2f} | 成本: {h['cost']:.2f} | 盈亏: {pct*100:+.1f}%")
    print(f"    计划: {action}")
    print(f"    原因: {reason}")

print("\n【操作优先级】")
print("-" * 40)
print("  1. 【紧急】东风股份(601515) —38% → 明日开盘集合竞价直接清仓, 不设限价")
print("  2. 【高优】台华新材(603005) —2.3% → 设止损8.00, 跌破即清仓")
print("  3. 【关注】沃华医药(002107) +8.3% → 设移动止盈(从高点回撤5%减半)")
print("  4. 【关注】贤丰控股(002319) +10.8% → 设移动止盈(从高点回撤5%减半)")

print("\n【买入检查】")
print("-" * 40)
print(f"  可用资金: {available:,.2f} 元")
print(f"  持仓比例: {position_pct*100:.1f}% (偏轻仓)")
print(f"  若东风清仓后: 可用资金增至约 {available + 540:,.0f} 元, 持仓降至约 22.5%")
print("  建议: 东风清仓后释放的资金 + 原有可用资金, 合计约16,950元可用于新开仓")
print("  新开仓标准: 缠论一买确认(DL_P>0.8) + 候选池内 + 二买加仓信号")

print("\n【关键价位提醒】")
print("-" * 40)
for h in holdings:
    cost = h["cost"]
    close = h["close"]
    stop_loss = cost * 0.95  # 5%止损
    take_profit = cost * 1.10 if h["pnl_pct"] > 0 else cost * 1.05  # 止盈
    print(f"  {h['name']}: 止损={stop_loss:.2f} | 保本={cost:.2f} | 目标={take_profit:.2f}")

print("\n【纪律提醒】")
print("-" * 40)
print("  ① 单笔亏损不超过5% (东风已严重违反)")
print("  ② 单票仓位不超过35%")
print("  ③ 破一买低点必须清仓, 不抱侥幸")
print("  ④ 缠论卖点信号出现时主动离场, 不被动止损")
print("  ⑤ WAIVED持仓每周重新评估, 不无限期搁置")
print("  ⑥ 盈利单设移动止盈, 不让利润变亏损")

print("\n" + "=" * 60)
print("✅ 合规审查完成")
print("=" * 60)

# 输出摘要供截图文字说明
print("\n【摘要】")
total_severity = len(issues)
total_warnings = len(warnings)
if total_severity >= 3:
    level = "🔴 高风险"
elif total_severity >= 1:
    level = "🟡 中风险"
else:
    level = "🟢 低风险"
print(f"  风险等级: {level}")
print(f"  严重问题: {total_severity}项")
print(f"  警告: {total_warnings}项")
print(f"  最紧急操作: 东风股份强制清仓")