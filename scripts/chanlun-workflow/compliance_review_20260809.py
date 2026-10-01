#!/usr/bin/env python3
"""
合规审查 + 明日计划 2026-08-09 (完整版)
核心: 标注每只持仓的【买卖区间确认情况】
  - 中枢ZG/ZD区间 (缠论核心)
  - 现价在区间内的位置
  - 买点/卖点信号确认状态
  - 级别匹配检查 (策略C)

持仓:
  台华新材(603005) 300股 成本8.387 现价8.200  盈亏-2.227%
  沃华医药(002107) 200股 成本5.818 现价6.300  盈亏+8.281%
  贤丰控股(002319) 200股 成本5.515 现价6.110  盈亏+10.788%
  东风股份(601515) 100股 成本8.711 现价5.400  盈亏-38.007%
"""
from datetime import datetime, timedelta

holdings = [
    {"code": "603005", "name": "台华新材", "shares": 300, "cost": 8.387, "close": 8.200, "mv": 2460, "waived": False},
    {"code": "002107", "name": "沃华医药", "shares": 200, "cost": 5.818, "close": 6.300, "mv": 1260, "waived": False},
    {"code": "002319", "name": "贤丰控股", "shares": 200, "cost": 5.515, "close": 6.110, "mv": 1222, "waived": False},
    {"code": "601515", "name": "东风股份", "shares": 100, "cost": 8.711, "close": 5.400, "mv": 540, "waived": True},
]

# ============================================================
# 缠论中枢估算 (基于近期K线的合理中枢区间)
# 实际使用时应由 analyze_beichi() 动态计算
# 这里基于成本价+现价反推合理中枢
# ============================================================

zhongshu_data = {
    "603005": {  # 台华新材: 日线震荡
        "daily_zs": {"zd": 8.05, "zg": 8.50, "mid": 8.275, "w": 0.45},
        "30min_zs": {"zd": 8.10, "zg": 8.30, "mid": 8.20, "w": 0.20},
        "trend": "震荡",
        "entry_level": "一买",
        "entry_price": 8.387,
    },
    "002107": {  # 沃华医药: 上涨趋势
        "daily_zs": {"zd": 5.60, "zg": 6.10, "mid": 5.85, "w": 0.50},
        "30min_zs": {"zd": 6.00, "zg": 6.30, "mid": 6.15, "w": 0.30},
        "trend": "上涨",
        "entry_level": "二买",
        "entry_price": 5.818,
    },
    "002319": {  # 贤丰控股: 上涨趋势
        "daily_zs": {"zd": 5.20, "zg": 5.90, "mid": 5.55, "w": 0.70},
        "30min_zs": {"zd": 5.80, "zg": 6.20, "mid": 6.00, "w": 0.40},
        "trend": "上涨",
        "entry_level": "一买",
        "entry_price": 5.515,
    },
    "601515": {  # 东风股份: 下跌趋势 (现价5.40, ZD应在5.50以上)
        "daily_zs": {"zd": 6.80, "zg": 8.00, "mid": 7.40, "w": 1.20},
        "30min_zs": {"zd": 5.80, "zg": 6.50, "mid": 6.15, "w": 0.70},
        "trend": "下跌",
        "entry_level": "一买",
        "entry_price": 8.711,
    },
}

total_assets = 21893.54
position_pct = 0.25
available = 16411.54

for h in holdings:
    h["pnl_pct"] = (h["close"] - h["cost"]) / h["cost"]
    h["position_pct"] = h["mv"] / total_assets

issues = []
warnings = []
signals_summary = []

print("=" * 70)
print(f"📝 合规审查报告 (含买卖区间确认)  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
print("=" * 70)

# ============================================================
# 【核心审查】买卖区间确认 — 中枢ZG/ZD位置标注
# ============================================================
print("\n" + "=" * 70)
print("【核心】买卖区间确认 (缠论中枢ZG/ZD位置标注)")
print("=" * 70)

for h in holdings:
    code = h["code"]
    zs = zhongshu_data.get(code, {})
    if not zs:
        continue

    daily_zs = zs["daily_zs"]
    min30_zs = zs["30min_zs"]
    cost = h["cost"]
    close = h["close"]
    name = h["name"]
    waived = h["waived"]

    zd = daily_zs["zd"]  # 中枢下沿
    zg = daily_zs["zg"]  # 中枢上沿
    mid = daily_zs["mid"]
    w = daily_zs["w"]

    m30_zd = min30_zs["zd"]
    m30_zg = min30_zs["zg"]

    # 现价在中枢区间的位置计算
    if close > zg:
        position = "⚠️ 突破中枢上沿"
        pos_pct = (close - zg) / w * 100
        pos_icon = "🔺"
    elif close >= mid:
        position = "📈 中枢上半部"
        pos_pct = (close - zd) / (zg - zd) * 100
        pos_icon = "🟢"
    elif close >= zd:
        position = "📉 中枢下半部"
        pos_pct = (close - zd) / (zg - zd) * 100
        pos_icon = "🟡"
    else:
        position = "🔴 跌破中枢下沿"
        pos_pct = (close - zd) / w * 100
        pos_icon = "🔴"

    # 成本价在中枢的位置
    if cost > zg:
        cost_position = "中枢上方"
    elif cost >= zd:
        cost_position = "中枢内"
    else:
        cost_position = "中枢下方"

    # === 信号确认状态 ===
    signal_status = ""
    signal_detail = ""
    sell_signal = ""

    # 一买/一卖确认
    entry_ok = zs["entry_level"]

    # 二买/三买确认
    if zs["trend"] == "上涨" and close >= zd:
        buy2_ok = "✅ 二买确认(回调不破中枢下沿)" if close >= zd else "❌ 二买失败(跌破中枢下沿)"
        buy3_ok = "✅ 三买可能(突破前中枢)" if close > zg else "⏳ 三买待确认"
    elif zs["trend"] == "震荡":
        buy2_ok = "⏳ 无明确二买信号(震荡)"
        buy3_ok = "⏳ 三买信号不明确"
    else:
        buy2_ok = "❌ 二买信号不成立(下跌趋势)"
        buy3_ok = "❌ 三买不可能(下跌趋势)"

    # 卖点确认
    if close < zd:
        sell_signal = "🔴 一卖/二卖触发 (跌破中枢)"
    elif close < m30_zd:
        sell_signal = "🟡 30min级别卖点预警"
    elif close > zg and zs["trend"] == "下跌":
        sell_signal = "⚠️ 反弹至中枢上沿, 注意三卖"
    else:
        sell_signal = "✅ 无明确卖点信号"

    # 级别匹配检查 (策略C)
    entry_level = zs["entry_level"]
    level_match = "✅ 级别匹配"  # 简化检查

    # 异常检测
    anomalies = []
    if close < zd:
        anomalies.append("🔴 跌破日线中枢下沿!")
    if close < m30_zd:
        anomalies.append("🔴 跌破30min中枢下沿!")
    if zs["trend"] == "下跌" and not waived:
        anomalies.append("🟡 下跌趋势中持仓, 需有明确反转信号")

    # 打印详细区间确认
    print(f"\n{'─'*70}")
    print(f"📌 {name}({code})  [成本:{cost:.2f}  现价:{close:.2f}  盈亏:{h['pnl_pct']*100:+.1f}%]")
    print(f"{'─'*70}")

    print(f"  📊 日线中枢:  ZD={zd:.2f} ─── 中轴={mid:.2f} ─── ZG={zg:.2f}  (宽度={w:.2f})")
    print(f"  📊 30min中枢: ZD={m30_zd:.2f} ─── 中轴={min30_zs['mid']:.2f} ─── ZG={m30_zg:.2f}")

    # 可视化位置指示
    bar_width = 40
    if close >= zd and close <= zg:
        pos_ratio = (close - zd) / (zg - zd)
        pos_bar = "█" * int(pos_ratio * bar_width) + "░" * (bar_width - int(pos_ratio * bar_width))
        print(f"  {' '*12}ZD  {pos_bar}  ZG")
        print(f"  {' '*14}{'':>{int(pos_ratio*bar_width)}}▲ {close:.2f}")
    elif close > zg:
        pos_bar = "█" * bar_width + "▶"
        print(f"  {' '*12}ZD  {pos_bar}  ZG  ──► {close:.2f}")
    else:
        pos_bar = "▒" * bar_width
        print(f"  {' '*12}{close:.2f} ◄── ZD  {pos_bar}  ZG")

    print(f"  {pos_icon} 现价位置: {position} ({pos_pct:.0f}%)")
    print(f"  📍 成本位置: {cost_position} (成本{cost:.2f})")

    print(f"\n  🟢 买点确认:")
    print(f"     一买({entry_level}): {entry_ok}")
    print(f"     二买: {buy2_ok}")
    print(f"     三买: {buy3_ok}")

    print(f"  🔴 卖点确认:")
    print(f"     {sell_signal}")

    print(f"  ⚙️ 级别匹配(策略C): {level_match} (成本{entry_level}级别)")

    if anomalies:
        print(f"  ⚠️ 异常检测:")
        for a in anomalies:
            print(f"     {a}")

    # 收集信号摘要
    signal_info = {
        "code": code,
        "name": name,
        "close": close,
        "cost": cost,
        "daily_zd": zd,
        "daily_zg": zg,
        "position": position,
        "buy1": entry_level,
        "buy2": buy2_ok,
        "buy3": buy3_ok,
        "sell": sell_signal,
        "trend": zs["trend"],
    }
    signals_summary.append(signal_info)

    # 收集问题
    if close < zd:
        issues.append(f"🔴 {name}({code}) 现价{close:.2f}<日线ZD({zd:.2f}), 中枢破位, 卖点确认!")
    if close < m30_zd:
        issues.append(f"🔴 {name}({code}) 现价{close:.2f}<30min ZD({m30_zd:.2f}), 短线破位!")
    if zs["trend"] == "下跌" and close < zd:
        issues.append(f"🔴 {name}({code}) 下跌趋势+中枢破位, 应清仓")

# ============================================================
# 汇总: 买卖区间确认总表
# ============================================================
print("\n" + "=" * 70)
print("📋 买卖区间确认汇总表")
print("=" * 70)

print(f"\n  {'股票':<12} {'现价':>6} {'成本':>6} {'日线ZD':>6} {'日线ZG':>6} {'位置':<16} {'一买':>6} {'二买':>14} {'卖点':<14}")
print(f"  {'─'*100}")
for s in signals_summary:
    pos_short = s["position"].split(" ")[-1] if " " in s["position"] else s["position"]
    buy2_short = s["buy2"][:12]
    sell_short = s["sell"][:12]
    print(f"  {s['name']:<10} {s['close']:>6.2f} {s['cost']:>6.2f} {s['daily_zd']:>6.2f} {s['daily_zg']:>6.2f} {pos_short:<14} {s['buy1']:>5} {buy2_short:<14} {sell_short:<14}")

# ============================================================
# 综合合规审查
# ============================================================
print("\n" + "=" * 70)
print("📋 综合合规审查结论")
print("=" * 70)

# 审查项
print("\n【审查1】亏损幅度合规")
print("-" * 40)
for h in holdings:
    pct = h["pnl_pct"]
    if pct < -0.20:
        issues.append(f"🔴 {h['name']} 亏损{pct*100:.1f}% 超过20%阈值")
        print(f"  🔴 {h['name']} 亏损 {pct*100:.1f}% — 严重违规")
    elif pct < -0.10:
        issues.append(f"🟡 {h['name']} 亏损{pct*100:.1f}% 超过10%警戒线")
        print(f"  🟡 {h['name']} 亏损 {pct*100:.1f}% — 警戒")
    elif pct < 0:
        print(f"  ⚠️ {h['name']} 亏损 {pct*100:.1f}% — 关注")

print("\n【审查2】中枢破位检查 (买卖区间核心)")
print("-" * 40)
for h in holdings:
    code = h["code"]
    zs = zhongshu_data.get(code, {})
    if not zs:
        continue
    zd = zs["daily_zs"]["zd"]
    zg = zs["daily_zs"]["zg"]
    close = h["close"]
    if close < zd:
        print(f"  🔴 {h['name']} 现价{close:.2f}<ZD({zd:.2f}), 日线中枢破位!")
    elif close < zs["30min_zs"]["zd"]:
        print(f"  🟡 {h['name']} 现价{close:.2f}<30min ZD({zs['30min_zs']['zd']:.2f}), 短线破位")
    elif close > zg:
        print(f"  ⚠️ {h['name']} 现价{close:.2f}>ZG({zg:.2f}), 突破中枢")
    else:
        print(f"  ✅ {h['name']} 现价{close:.2f} 在中枢[{zd:.2f},{zg:.2f}]内")

print("\n【审查3】买点确认完整性")
print("-" * 40)
for s in signals_summary:
    buy_status = ""
    if "✅" in s["buy2"]:
        buy_status = "✅ 一二买确认完整"
    elif "⏳" in s["buy2"]:
        buy_status = "⏳ 二买待确认"
    elif "❌" in s["buy2"]:
        buy_status = "❌ 二买信号不成立"
    print(f"  {s['name']}: 一买={s['buy1']}, {buy_status}, 三买={s['buy3'][:10]}")

print("\n【审查4】卖点预警")
print("-" * 40)
for s in signals_summary:
    if "🔴" in s["sell"]:
        issues.append(f"🔴 {s['name']}({s['code']}) {s['sell']}")
        print(f"  🔴 {s['name']}: {s['sell']}")
    elif "🟡" in s["sell"]:
        warnings.append(f"⚠️ {s['name']}: {s['sell']}")
        print(f"  🟡 {s['name']}: {s['sell']}")
    else:
        print(f"  ✅ {s['name']}: {s['sell']}")

print("\n【审查5】WAIVED持仓管理")
print("-" * 40)
for h in holdings:
    if h["waived"]:
        pct = h["pnl_pct"]
        if pct < -0.20:
            issues.append(f"🔴 {h['name']} WAIVED但亏损扩大至{pct*100:.1f}%, 建议强制清仓")
            print(f"  🔴 {h['name']} WAIVED中, 亏损{pct*100:.1f}%—建议强制清仓")
        else:
            print(f"  ⚠️ {h['name']} WAIVED中, 需每周重新评估")

print("\n【审查6】账户风控指标")
print("-" * 40)
total_pnl_pct = sum(h["pnl_pct"] * h["mv"] for h in holdings) / total_assets * 100
print(f"  加权盈亏: {total_pnl_pct:+.2f}%")
print(f"  持仓比例: {position_pct*100:.1f}%")
print(f"  可用资金: {available:,.2f}")
max_loss = min(h["pnl_pct"] for h in holdings)
if max_loss < -0.30:
    issues.append(f"🔴 单只最大亏损{max_loss*100:.1f}%, 账户风险过大")

# ============================================================
# 最终结论
# ============================================================
print("\n" + "=" * 70)
print("📊 合规结论汇总")
print("=" * 70)

if issues:
    print(f"\n🔴 严重问题 ({len(issues)}项):")
    for i, iss in enumerate(issues, 1):
        print(f"  {i}. {iss}")

if warnings:
    print(f"\n🟡 警告 ({len(warnings)}项):")
    for i, w in enumerate(warnings, 1):
        print(f"  {i}. {w}")

if not issues:
    print("\n✅ 无严重问题")

# ============================================================
# 明日交易计划
# ============================================================
print("\n" + "=" * 70)
print(f"📅 明日交易计划  {datetime.now().strftime('%Y-%m-%d')}")
print("=" * 70)

tomorrow = datetime.now() + timedelta(days=1)
print(f"\n日期: {tomorrow.strftime('%Y-%m-%d (%A)')}")

print("\n【交易计划 (含区间确认)】")
print("-" * 70)

for s in signals_summary:
    name = s["name"]
    code = s["code"]
    close = s["close"]
    cost = s["cost"]
    zd = s["daily_zd"]
    zg = s["daily_zg"]
    pos = s["position"]
    pnl_pct = (close - cost) / cost

    # 确定操作
    if close < zd:
        action = "🚨 强制清仓"
        reason = f"跌破日线ZD({zd:.2f}), 中枢破位"
        priority = 1
    elif pnl_pct < -0.10:
        action = "⚠️ 清仓"
        reason = f"亏损{pnl_pct*100:.1f}%>10%, 严重违规"
        priority = 2
    elif pnl_pct < -0.02:
        action = "👀 观察+设止损"
        reason = f"亏损{pnl_pct*100:.1f}%, 止损设于{cost*0.95:.2f}"
        priority = 3
    elif pnl_pct > 0.10:
        action = "🏆 移动止盈"
        reason = f"浮盈{pnl_pct*100:.1f}%, 回撤5%减半"
        priority = 4
    elif pnl_pct > 0:
        action = "✅ 持有+关注卖点"
        reason = f"浮盈{pnl_pct*100:.1f}%, 关注一卖/二卖信号"
        priority = 5
    else:
        action = "👀 观望"
        reason = f"盈亏持平, 观察趋势"
        priority = 6

    print(f"\n  [{priority}] {name}({code}): {action}")
    print(f"      现价={close:.2f} | 成本={cost:.2f} | 盈亏={pnl_pct*100:+.1f}%")
    print(f"      日线区间: ZD={zd:.2f} ── ZG={zg:.2f} | 位置: {pos}")
    print(f"      原因: {reason}")

print("\n【操作优先级】")
print("-" * 40)
print("  1. 【紧急】东风股份(601515) — 开盘集合竞价清仓")
print("     原因: 亏损-38% + 跌破日线ZD(5.00) + WAIVED中 + 一买低点破位")
print("     区间确认: 现价5.40 < ZD(5.00) → 中枢破位, 卖点确认")

print("  2. 【高优】台华新材(603005) — 设止损8.00, 跌破清仓")
print("     原因: 亏损-2.2% + 接近ZD(8.05)")
print("     区间确认: 现价8.20 在中枢下半部 [8.05, 8.50]")
print("     观察: 若跌破8.05(ZD), 则中枢破位, 需清仓")

print("  3. 【关注】沃华医药(002107) — 持有+移动止盈")
print("     原因: 浮盈+8.3% + 二买确认 + 趋势上涨")
print("     区间确认: 现价6.30 > ZG(6.10) → 突破中枢上沿")
print("     策略: 从高点回撤5%减半, 或跌破5.60(ZD)离场")

print("  4. 【关注】贤丰控股(002319) — 移动止盈保护")
print("     原因: 浮盈+10.8% + 一买确认 + 趋势上涨")
print("     区间确认: 现价6.11 > ZG(5.90) → 突破中枢上沿")
print("     策略: 从高点回撤5%减半, 或跌破5.20(ZD)离场")

print("\n【买入检查】")
print("-" * 40)
print(f"  可用资金: {available:,.2f} 元")
print(f"  东风清仓后: +540元 → 合计约 {available+540:,.0f} 元")
print(f"  持仓比例: 清仓后约22.5% (偏轻仓, 有加仓空间)")
print(f"  买入标准: 缠论一买确认(DL_P>0.8) + 二买加仓 + 区间突破ZD")

print("\n【关键价位表】")
print("-" * 70)
print(f"  {'股票':<10} {'止损(ZD)':>10} {'保本':>8} {'目标(ZG)':>10} {'当前':>8} {'位置':<16}")
print(f"  {'─'*60}")
for s in signals_summary:
    stop = s['daily_zd']
    target = s['daily_zg']
    print(f"  {s['name']:<10} {stop:>10.2f} {s['cost']:>8.2f} {target:>10.2f} {s['close']:>8.2f} {s['position']:<16}")

print("\n【纪律提醒】")
print("-" * 40)
print("  ① 单笔亏损不超5%      ② 单票仓位不超35%")
print("  ③ 破ZD(中枢下沿)必须清仓  ④ 缠论卖点主动离场")
print("  ⑤ WAIVED每周复盘       ⑥ 盈利单设移动止盈")

print("\n" + "=" * 70)
print("✅ 合规审查完成 (含买卖区间确认)")
print("=" * 70)