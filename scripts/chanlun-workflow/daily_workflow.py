#!/usr/bin/env python3
"""每日交易工作流 — 主入口
用法:
  python daily_workflow.py screenshot [截图路径1 截图路径2 ...]
  python daily_workflow.py compliance
  python daily_workflow.py scan            # 日线全市场扫描 + 写入候选池
  python daily_workflow.py intraday        # 盘中30min扫描候选池 + 持仓止损检查
  python daily_workflow.py account
  python daily_workflow.py holdings
"""
import sys, subprocess, json, os, time
import urllib.request
from datetime import datetime, date
from decimal import Decimal
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WB = os.environ.get('TRADE_WB', os.path.join(SCRIPT_DIR, '动态仓位资金管理法则_执行版.xlsx'))
RECALC = os.path.join(SCRIPT_DIR, 'recalc.py')
BEICHI_DIR = SCRIPT_DIR

def recalc():
    r = subprocess.run(['python', RECALC, WB, '30'], capture_output=True, text=True)
    return json.loads(r.stdout) if r.stdout else {"status": "error"}

def get_today_holdings():
    wb = load_workbook(WB, data_only=True)
    ws = wb['持仓表']
    holdings = []
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        code = ws.cell(row=r, column=3).value
        waived = ws.cell(row=r, column=4).value
        shares = ws.cell(row=r, column=7).value
        entry = ws.cell(row=r, column=8).value
        close = ws.cell(row=r, column=9).value
        stop = ws.cell(row=r, column=16).value
        action = ws.cell(row=r, column=28).value
        profit = ws.cell(row=r, column=13).value
        pos = ws.cell(row=r, column=14).value
        holdings.append({
            "name": name, "code": code, "waived": waived,
            "shares": shares, "entry": entry, "close": close,
            "stop": stop, "action": action, "profit": profit, "pos": pos
        })
    return holdings

def get_account_summary():
    wb = load_workbook(WB, data_only=True)
    ws = wb['账户总表']
    latest_row = 2
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value:
            latest_row = r
    return {
        "date": ws.cell(row=latest_row, column=1).value,
        "total_asset": ws.cell(row=latest_row, column=2).value,
        "cash": ws.cell(row=latest_row, column=3).value,
        "position_ratio": ws.cell(row=latest_row, column=5).value,
        "stage": ws.cell(row=latest_row, column=33).value,
        "monthly_target": ws.cell(row=latest_row, column=28).value,
        "deviation": ws.cell(row=latest_row, column=31).value,
        "status": ws.cell(row=latest_row, column=20).value,
        "allow_new": ws.cell(row=latest_row, column=22).value,
    }

def check_compliance():
    holdings = get_today_holdings()
    account = get_account_summary()
    issues = []

    # === 合规审查首项: 成本价-破位级别匹配检查 (策略C, 2026-07-26) ===
    # 规则: 以成本价确定买点级别, 只执行该级别的卖点/破位
    #       现价小级别买点不构成加仓/持有理由
    #       破位级别必须与成本买点级别匹配才触发操作
    print(f"{'='*50}")
    print(f"📝 合规核查 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{'='*50}")
    print(f"【首项】成本价-破位级别匹配检查 (策略C)")
    print(f"  规则: 成本价定买点级别 → 只执行该级别卖点/破位")
    print(f"  原则: 买入卖出同级别闭环, 避免级别错配")

    level_mismatch_count = 0
    for h in holdings:
        if h['waived'] == '是':
            continue
        code = str(h['code'])
        cost = h['entry'] or 0
        close = h['close'] or 0
        if cost <= 0:
            continue

        entry_data = detect_entry_level(code, cost)
        entry_level = entry_data["entry_level"]
        sell_levels = entry_data["sell_levels"]
        zone = entry_data["entry_info"].get(entry_level, {}).get("zone", "未知") if entry_level else "未确定"

        # 检查成本对应级别是否破位
        level_bd = False
        for level in sell_levels:
            try:
                r = analyze_beichi(code, level=level)
                if "error" in r or not r.get("zss"):
                    continue
                last_zs = r["zss"][-1]
                if close < last_zs["zd"]:
                    pct = ((close - last_zs["zd"]) / last_zs["zd"]) * 100
                    issues.append(f"🔴 {h['name']}({code}) {level}中枢破位{pct:+.1f}% (成本{cost:.2f}对应{entry_level}买点, 现价{close:.2f}<下沿{last_zs['zd']:.2f})")
                    level_bd = True
            except:
                pass

        # 检查是否有更小级别买点(策略C明确忽略)
        if entry_level and level_bd:
            level_idx = ["日线", "30min", "5min", "1min"].index(entry_level)
            smaller_levels = ["日线", "30min", "5min", "1min"][level_idx+1:]
            for level in smaller_levels:
                try:
                    r = analyze_beichi(code, level=level)
                    if "error" in r:
                        continue
                    for sig in r.get("signals", []):
                        if sig["op"] == "一买" and sig["ratio"] < 60 and sig["dl_prob"] > 0.8 and sig["valid"]:
                            issues.append(f"⚠️ {h['name']}({code}) {level}有确认一买但非成本对应级别, 不构成加仓理由(策略C)")
                except:
                    pass

    print(f"  成本级别匹配检查: {'⚠️发现'+str(len([i for i in issues if '中枢破位' in i]))+'个破位' if issues else '✓ 无破位'}")

    # === 其他合规检查 ===
    for h in holdings:
        if h['waived'] == '是':
            continue
        if h['close'] and h['stop'] and h['close'] <= h['stop']:
            issues.append(f"⚠️ {h['name']}已破止损: 现价{h['close']:.2f}<=止损{h['stop']:.2f}")
        if h['pos'] and h['pos'] > 0.35:
            issues.append(f"⚠️ {h['name']}仓位超限: {h['pos']:.1%}>35%")

    # 输出合规摘要
    print(f"\n账户总资产: ¥{account.get('total_asset', 0):.2f}" if account.get('total_asset') else "账户总资产: N/A")
    print(f"现金: ¥{account.get('cash', 0):.2f}" if account.get('cash') else "现金: N/A")
    print(f"仓位比例: {account.get('position_ratio', 0):.1%}" if account.get('position_ratio') else "仓位比例: N/A")
    print(f"持仓数量: {len(holdings)}只")
    if issues:
        print(f"\n⚠️ 合规告警 ({len(issues)}项):")
        for issue in issues:
            print(f"  {issue}")
    else:
        print("\n✓ 持仓合规, 无告警")

    print(f"\n📝 心态日志提醒:")
    print(f"  评分维度(0-5分):")
    print(f"  • 非工作流看盘次数 (0=0次最好)")
    print(f"  • 是否追价操作 (0=没有最好)")
    print(f"  • 是否按系统执行 (5=完全最好)")
    print(f"  • 情绪状态 (5=平静最好)")
    print(f"  • 止损外操作 (0=没有最好)")
    print(f"  达标线: 80分")
    print(f"{'='*50}")

    # 同时输出 JSON (供程序化处理)
    def safe(v):
        if isinstance(v, (datetime, date)): return str(v)
        if isinstance(v, Decimal): return float(v)
        return v
    print(json.dumps({"account": {k:safe(v) for k,v in account.items()}, "holdings": [{k:safe(v) for k,v in h.items()} for h in holdings], "issues": issues, "compliant": len(issues)==0}, ensure_ascii=False, indent=2))

    return account, holdings, issues

def safe_val(v):
    if isinstance(v, (datetime, date)): return str(v)
    if isinstance(v, Decimal): return float(v)
    return v

def run_full_scan():
    """全市场候选扫描: 沪A主板全量 + 深市全量(000/002) + 写入候选池(排除持仓股)"""
    sys.path.insert(0, BEICHI_DIR)
    from full_scan import full_scan, calc_funding
    account = get_account_summary()
    result = full_scan(
        total_asset=account["total_asset"] or 20326.12,
        cash=account["cash"] or 7847.12,
        silent=False,
    )

    # 排除持仓股(已持有的不再推荐)
    holdings = get_today_holdings()
    held_codes = {str(h['code']) for h in holdings if h.get('code')}
    all_near = [r for r in result["near"] if r["code"] not in held_codes]
    if held_codes:
        excluded = len(result["near"]) - len(all_near)
        if excluded:
            print(f"  排除持仓股: {excluded}只 ({', '.join(sorted(held_codes))})")

    if not all_near:
        print("\n候选池: 无接近确认标的(排除持仓后), 跳过写入")
        return result

    sha_near = sorted([r for r in all_near if r["code"].startswith("6")], key=lambda x: (-x["score"], x["ratio"]))
    sz_near = sorted([r for r in all_near if r["code"].startswith("0")], key=lambda x: (-x["score"], x["ratio"]))
    selected = sha_near[:15] + sz_near[:15]
    selected.sort(key=lambda x: (-x["score"], x["ratio"]))

    wb = load_workbook(WB)
    ws = wb['候选池']

    # 清空旧数据(保留表头和公式列)
    for r in range(2, ws.max_row + 1):
        for c in range(1, ws.max_column + 1):
            cell = ws.cell(row=r, column=c)
            if not cell.value or str(cell.value).startswith('='):
                continue
            cell.value = None

    today = date.today()
    for idx, stock in enumerate(selected):
        row = 2 + idx
        price = stock["price"]
        fund = calc_funding(price, result["total_asset"], result["cash"])
        missing = []
        if stock["ratio"] >= 60: missing.append("ratio=%d%%" % stock["ratio"])
        if stock["dlp"] <= 0.8: missing.append("DL_P=%.2f" % stock["dlp"])
        note = "接近确认,缺:" + "+".join(missing)
        if fund["need_transfer"]: note += " | 需转入%.0f元" % fund["transfer"]

        ws.cell(row=row, column=1, value=today)
        ws.cell(row=row, column=2, value=stock.get("name", ""))
        ws.cell(row=row, column=3, value=stock["code"])
        ws.cell(row=row, column=4, value=price)
        ws.cell(row=row, column=5, value=price)
        ws.cell(row=row, column=6, value=round(price * 0.95, 2))
        ws.cell(row=row, column=7, value=stock["ratio"] / 100)
        ws.cell(row=row, column=8, value=stock["dlp"])
        ws.cell(row=row, column=9, value=str(stock["valid"]))
        ws.cell(row=row, column=10, value="一买")
        ws.cell(row=row, column=31, value=note)

    wb.save(WB)
    recalc_result = recalc()
    errors = recalc_result.get("total_errors", -1)

    print(f"\n候选池: 写入{len(selected)}只 (沪A{min(15,len(sha_near))}+深市{min(15,len(sz_near))})")
    print(f"公式重算: {errors}个错误")
    return {
        "scan_result": result,
        "selected": selected,
        "errors": errors,
    }

def detect_entry_level(code, cost):
    """根据成本价判断属于哪个级别的买点区间, 返回对应卖点级别列表

    策略C (2026-07-26修订):
    逻辑: 成本落在某级别中枢区间内 → 该级别为买点级别 → 只监控该级别卖点/破位
          成本在最新中枢下方 → 该级别一买区 → 只监控该级别卖点/破位
          成本在所有中枢上方 → 基础级别买点 → 默认监控日线级别

    核心原则: 买入逻辑和卖出逻辑在同一级别闭环, 避免级别错配
    - 不扩展到更小级别(避免1min破位噪音干扰日线买点持仓)
    - 不看现价小级别买点(避免"现价有1min买点所以可以加仓"的误判)
    """
    sys.path.insert(0, BEICHI_DIR)
    from beichi_analyzer import analyze_beichi
    levels_priority = ["日线", "30min", "5min", "1min"]
    entry_info = {}

    for level in levels_priority:
        try:
            r = analyze_beichi(code, level=level)
            if "error" in r or not r.get("zss"):
                continue
            zss = r["zss"]
            last_zs = zss[-1]
            first_zs = zss[0]

            if last_zs["zd"] <= cost <= last_zs["zg"]:
                entry_info[level] = {"zone": "中枢内", "zs": last_zs}
            elif cost < last_zs["zd"]:
                entry_info[level] = {"zone": "中枢下方(一买区)", "zs": last_zs}
            elif cost < first_zs["zd"]:
                entry_info[level] = {"zone": "全中枢下方(深度一买区)", "zs": first_zs}
            else:
                entry_info[level] = {"zone": "中枢上方", "zs": last_zs}
        except:
            pass

    # 确定买点级别: 找成本最接近中枢区间内的级别(优先大级别)
    best_entry_level = None
    for level in levels_priority:
        info = entry_info.get(level)
        if info and info["zone"] in ("中枢内", "中枢下方(一买区)", "全中枢下方(深度一买区)"):
            best_entry_level = level
            break

    # 卖点监控级别: 只监控成本对应的买点级别(策略C)
    # 不扩展到更小级别, 避免小级别噪音造成过度交易
    if best_entry_level:
        sell_levels = [best_entry_level]  # 只监控该级别
    else:
        sell_levels = ["日线"]  # 无法确定则默认日线

    return {
        "entry_level": best_entry_level,
        "entry_info": entry_info,
        "sell_levels": sell_levels,
    }

def get_candidate_pool():
    """从Excel候选池读取标的列表"""
    wb = load_workbook(WB, data_only=True)
    ws = wb['候选池']
    candidates = []
    for r in range(2, ws.max_row + 1):
        code = ws.cell(row=r, column=3).value
        if not code:
            continue
        name = ws.cell(row=r, column=2).value or ""
        price = ws.cell(row=r, column=4).value or 0
        candidates.append({"code": str(code), "name": name, "price": float(price)})
    return candidates

def run_intraday_scan():
    """盘中扫描: 30min级别扫描候选池(排除持仓股) + 5min确认 + 持仓止损检查"""
    sys.path.insert(0, BEICHI_DIR)
    from beichi_analyzer import analyze_beichi
    import time as _time

    now = datetime.now()
    print(f"=== 盘中扫描 {now.strftime('%Y-%m-%d %H:%M')} ===\n")

    # 候选池排除持仓股
    holdings = get_today_holdings()
    held_codes = {str(h['code']) for h in holdings if h.get('code')}

    # 1. 候选池30min扫描
    candidates = get_candidate_pool()
    candidates = [c for c in candidates if c["code"] not in held_codes]
    if not candidates:
        print("候选池为空(排除持仓后), 跳过盘中扫描")
        return {"confirmed_30m": [], "near_30m": [], "alerts": [], "scanned": 0}
    print(f"[1/3] 候选池30min扫描 ({len(candidates)}只)...")
    t0 = _time.time()
    confirmed_30m = []
    near_30m = []
    for s in candidates:
        try:
            r = analyze_beichi(s["code"], level="30min")
            if "error" in r:
                continue
            close = r["C"][-1] if r.get("C") else s["price"]
            if s["price"] > 0 and close > 0 and (close / s["price"] > 10 or s["price"] / close > 10):
                close = s["price"]
            for sig in r.get("signals", []):
                if sig["op"] != "一买":
                    continue
                ratio = sig["ratio"]
                dlp = sig["dl_prob"]
                valid = sig["valid"]
                confirmed = ratio < 60 and dlp > 0.8 and valid
                near = (ratio < 60 and dlp > 0.6 and valid) or (ratio < 85 and dlp > 0.8 and valid)
                entry = {
                    "code": s["code"], "name": s["name"], "price": close or s["price"],
                    "ratio": ratio, "dlp": dlp, "valid": valid,
                }
                if confirmed:
                    confirmed_30m.append(entry)
                elif near:
                    near_30m.append(entry)
        except:
            pass
    elapsed_30m = _time.time() - t0
    print(f"  30min: 确认{len(confirmed_30m)}只, 接近{len(near_30m)}只, 耗时{elapsed_30m:.0f}s")

    # 2. 30min确认标的 → 5min精确买点
    confirmed_5m = []
    if confirmed_30m:
        print(f"\n[2/3] 5min精确买点扫描 ({len(confirmed_30m)}只)...")
        for s in confirmed_30m:
            try:
                r = analyze_beichi(s["code"], level="5min")
                if "error" in r:
                    continue
                for sig in r.get("signals", []):
                    if sig["op"] != "一买":
                        continue
                    ratio = sig["ratio"]
                    dlp = sig["dl_prob"]
                    valid = sig["valid"]
                    confirmed_5m = ratio < 60 and dlp > 0.8 and valid
                    if confirmed_5m or (ratio < 85 and dlp > 0.6 and valid):
                        print(f"  ★ {s['name']} {s['code']} 5min: ratio={ratio:.0f}% DL_P={dlp:.2f} valid={valid}")
            except:
                pass
    else:
        print(f"\n[2/3] 5min扫描: 跳过(30min无确认)")

    # 3. 持仓止损 + 背驰卖点 + 中枢破位检查(根据成本自动确定级别)
    print(f"\n[3/3] 持仓检查(止损+背驰卖点+中枢破位)...")
    holdings = get_today_holdings()
    alerts = []
    sell_signals = []
    zs_breakdowns = []
    entry_reports = []
    for h in holdings:
        code = str(h['code'])
        name = h['name']
        waived = h['waived']
        close_price = h['close'] or 0
        cost = h['entry'] or 0

        # 3a. 自动确定买点级别和对应卖点监控级别
        if cost > 0:
            entry_data = detect_entry_level(code, cost)
            entry_level = entry_data["entry_level"]
            sell_levels = entry_data["sell_levels"]
            entry_info = entry_data["entry_info"].get(entry_level, {}) if entry_level else {}
            zone_desc = entry_info.get("zone", "未知") if entry_info else "未知"
            entry_reports.append({
                "name": name, "code": code, "cost": cost,
                "entry_level": entry_level or "未确定",
                "zone": zone_desc,
                "sell_levels": sell_levels,
            })
            print(f"  📌 {name}({code}) 成本{cost:.2f} → 买点级别: {entry_level or '未确定'}({zone_desc}) → 监控卖点: {'+'.join(sell_levels)}")
        else:
            sell_levels = ["日线", "30min"]
            print(f"  📌 {name}({code}) 无成本价 → 默认监控: 日线+30min")

        # 3b. 止损检查
        if waived != '是':
            if close_price and h['stop'] and close_price <= h['stop']:
                alerts.append(f"⚠️ {name}({code}) 破止损: 现价{close_price:.2f}<=止损{h['stop']:.2f}")

        # 3c. 背驰卖点 + 中枢破位检查(动态级别)
        for level in sell_levels:
            try:
                r = analyze_beichi(code, level=level)
                if "error" in r:
                    continue

                # 中枢破位检查: 现价跌破最新中枢下沿
                zss = r.get("zss", [])
                if zss and close_price > 0:
                    last_zs = zss[-1]
                    zd = last_zs["zd"]
                    zg = last_zs["zg"]
                    if close_price < zd:
                        pct = ((close_price - zd) / zd) * 100
                        zs_breakdowns.append({
                            "name": name, "code": code, "level": level,
                            "price": close_price, "zd": zd, "zg": zg, "pct": pct,
                            "waived": waived,
                        })

                # 背驰卖点检查
                for sig in r.get("signals", []):
                    if "卖" not in sig["op"]:
                        continue
                    ratio = sig["ratio"]
                    dlp = sig["dl_prob"]
                    valid = sig["valid"]
                    confirmed_sell = ratio < 60 and dlp > 0.8 and valid
                    near_sell = (ratio < 60 and dlp > 0.6 and valid) or (ratio < 85 and dlp > 0.8 and valid)
                    if confirmed_sell:
                        sell_signals.append({
                            "name": name, "code": code, "level": level,
                            "op": sig["op"], "ratio": ratio, "dlp": dlp, "valid": valid,
                            "type": "确认卖点"
                        })
                        print(f"  🔴 {name}({code}) {level}确认卖点: {sig['op']} ratio={ratio:.0f}% DL_P={dlp:.2f}")
                    elif near_sell:
                        sell_signals.append({
                            "name": name, "code": code, "level": level,
                            "op": sig["op"], "ratio": ratio, "dlp": dlp, "valid": valid,
                            "type": "接近卖点"
                        })
            except:
                pass

    if alerts:
        for a in alerts:
            print(f"  {a}")

    # 中枢破位汇总
    if zs_breakdowns:
        print(f"  🔻 中枢破位: {len(zs_breakdowns)}个")
        for z in zs_breakdowns:
            waived_tag = " [WAIVED]" if z["waived"] == "是" else ""
            print(f"    {z['name']}({z['code']}) {z['level']} 现价{z['price']:.2f} 跌破下沿{z['zd']:.2f} ({z['pct']:+.1f}%){waived_tag}")

    # 卖点汇总
    confirmed_sells = [s for s in sell_signals if s["type"] == "确认卖点"]
    near_sells = [s for s in sell_signals if s["type"] == "接近卖点"]
    if confirmed_sells:
        print(f"  🔴 确认卖点: {len(confirmed_sells)}个")
    if near_sells:
        print(f"  🟡 接近卖点: {len(near_sells)}个")
        for s in near_sells[:5]:
            missing = []
            if s["ratio"] >= 60: missing.append("ratio=%d%%" % s["ratio"])
            if s["dlp"] <= 0.8: missing.append("DL_P=%.2f" % s["dlp"])
            print(f"    {s['name']}({s['code']}) {s['level']} {s['op']} 缺:{'+'.join(missing)}")
    if not alerts and not confirmed_sells and not near_sells and not zs_breakdowns:
        print(f"  持仓{len(holdings)}只, 止损合规, 无背驰卖点, 无中枢破位")

    # 汇总
    print(f"\n{'='*50}")
    if confirmed_30m:
        print(f"★ 30min确认信号: {len(confirmed_30m)}只")
        for s in confirmed_30m:
            print(f"  {s['name']} {s['code']} ¥{s['price']:.2f} ratio={s['ratio']:.0f}% DL_P={s['dlp']:.2f}")
    else:
        print("★ 30min确认信号: 0只")

    if near_30m:
        print(f"\n◆ 30min接近确认: {len(near_30m)}只")
        for s in near_30m[:5]:
            missing = []
            if s["ratio"] >= 60: missing.append("ratio=%d%%" % s["ratio"])
            if s["dlp"] <= 0.8: missing.append("DL_P=%.2f" % s["dlp"])
            print(f"  {s['name']} {s['code']} ¥{s['price']:.2f} 缺:{'+'.join(missing)}")

    if alerts:
        print(f"\n⚠️ 止损告警: {len(alerts)}只需处理")

    if zs_breakdowns:
        non_waived_bd = [z for z in zs_breakdowns if z["waived"] != "是"]
        print(f"\n🔻 中枢破位: {len(zs_breakdowns)}个 (非WAIVED {len(non_waived_bd)}个)")
        if non_waived_bd:
            print("  → 跌破中枢下沿但卖点未确认, 建议人工评估是否减仓")

    if confirmed_sells:
        print(f"\n🔴 确认卖点: {len(confirmed_sells)}个 (建议卖出)")
    if near_sells:
        print(f"\n🟡 接近卖点: {len(near_sells)}个")

    print(f"{'='*50}")
    return {
        "confirmed_30m": confirmed_30m,
        "near_30m": near_30m,
        "alerts": alerts,
        "confirmed_sells": confirmed_sells,
        "near_sells": near_sells,
        "zs_breakdowns": zs_breakdowns,
        "entry_reports": entry_reports,
        "scanned": len(candidates),
    }

def send_telegram(text, title=""):
    """Send message to Telegram. Splits long messages automatically."""
    # Try all possible env var names
    token = (os.environ.get('TELEGRAM_BOT_TOKEN')
             or os.environ.get('TG_TOKEN')
             or os.environ.get('TELEGRAM_TOKEN'))
    chat_id = (os.environ.get('TELEGRAM_CHAT_ID')
               or os.environ.get('TG_CHAT_ID')
               or os.environ.get('TELEGRAM_CHATID'))

    # Debug: show what env vars exist
    env_keys = sorted(os.environ.keys())
    tg_keys = [k for k in env_keys if 'TELEGRAM' in k.upper() or 'TG_' in k.upper()]
    print(f"[TG] Telegram env vars found: {tg_keys}")
    print(f"[TG] token set: {'YES' if token else 'NO'}")
    print(f"[TG] chat_id set: {'YES' if chat_id else 'NO'}")

    if not token or not chat_id:
        print("[TG] SKIP: token or chat_id missing, printing to stdout instead")
        print("=" * 40)
        if title:
            print(title)
        print(text)
        print("=" * 40)
        return

    # Build message with title
    full_text = f"{title}\n{text}" if title else text

    # Split into chunks of 4000 chars (Telegram limit is 4096)
    chunks = []
    while full_text:
        if len(full_text) <= 4000:
            chunks.append(full_text)
            break
        split_at = full_text.rfind('\n', 0, 4000)
        if split_at < 2000:
            split_at = 4000
        chunks.append(full_text[:split_at])
        full_text = full_text[split_at:].lstrip('\n')

    print(f"[TG] Sending {len(chunks)} message(s), total chars={len(text)}")

    for i, chunk in enumerate(chunks):
        if i > 0:
            chunk = f"续({i+1}/{len(chunks)}):\n{chunk}"
        data = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }).encode('utf-8')
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=15)
            resp_body = resp.read().decode('utf-8')
            print(f"[TG] chunk {i+1} OK: {resp_body[:100]}")
        except urllib.error.HTTPError as e:
            err_body = e.read().decode('utf-8') if hasattr(e, 'read') else str(e)
            print(f"[TG] chunk {i+1} HTTP ERROR {e.code}: {err_body}")
        except Exception as e:
            print(f"[TG] chunk {i+1} ERROR: {type(e).__name__}: {e}")
        if i < len(chunks) - 1:
            time.sleep(1)


def format_intraday_summary(result, ts):
    """Format intraday scan results into clean Telegram message."""
    lines = [f"📡 盘中扫描 {ts}", ""]

    scanned = result.get("scanned", 0)
    lines.append(f"📊 扫描: {scanned}只候选")
    lines.append("")

    # 30min确认
    confirmed = result.get("confirmed_30m", [])
    if confirmed:
        lines.append(f"★ 30min确认: {len(confirmed)}只")
        for s in confirmed:
            lines.append(f"  {s['name']} {s['code']} ¥{s['price']:.2f} ratio={s['ratio']:.0f}% DL_P={s['dlp']:.2f}")
        lines.append("")

    # 30min接近
    near = result.get("near_30m", [])
    if near:
        lines.append(f"◆ 30min接近: {len(near)}只")
        for s in near[:8]:
            missing = []
            if s["ratio"] >= 60: missing.append(f"ratio={s['ratio']:.0f}%")
            if s["dlp"] <= 0.8: missing.append(f"DL_P={s['dlp']:.2f}")
            lines.append(f"  {s['name']} {s['code']} ¥{s['price']:.2f} 缺:{'+'.join(missing)}")
        if len(near) > 8:
            lines.append(f"  ...还有{len(near)-8}只")
        lines.append("")

    # 持仓买点级别
    entry_reports = result.get("entry_reports", [])
    if entry_reports:
        lines.append("📌 持仓买点级别:")
        for e in entry_reports:
            lines.append(f"  {e['name']}({e['code']}) {e['entry_level']} → 监控:{'+'.join(e['sell_levels'])}")
        lines.append("")

    # 止损告警
    alerts = result.get("alerts", [])
    if alerts:
        lines.append(f"⚠️ 止损告警: {len(alerts)}只")
        for a in alerts:
            lines.append(f"  {a}")
        lines.append("")

    # 中枢破位
    breakdowns = result.get("zs_breakdowns", [])
    if breakdowns:
        non_waived = [z for z in breakdowns if z["waived"] != "是"]
        lines.append(f"🔻 中枢破位: {len(breakdowns)}个 (非WAIVED {len(non_waived)}个)")
        for z in non_waived[:5]:
            lines.append(f"  {z['name']}({z['code']}) {z['level']} ¥{z['price']:.2f} 跌破下沿{z['zd']:.2f} ({z['pct']:+.1f}%)")
        if len(non_waived) > 5:
            lines.append(f"  ...还有{len(non_waived)-5}个")
        lines.append("")

    # 卖点
    confirmed_sells = result.get("confirmed_sells", [])
    near_sells = result.get("near_sells", [])
    if confirmed_sells:
        lines.append(f"🔴 确认卖点: {len(confirmed_sells)}个 (建议卖出)")
        for s in confirmed_sells:
            lines.append(f"  {s['name']}({s['code']}) {s['level']} {s['op']} ratio={s['ratio']:.0f}% DL_P={s['dlp']:.2f}")
        lines.append("")
    if near_sells:
        lines.append(f"🟡 接近卖点: {len(near_sells)}个")
        for s in near_sells[:5]:
            missing = []
            if s["ratio"] >= 60: missing.append(f"ratio={s['ratio']:.0f}%")
            if s["dlp"] <= 0.8: missing.append(f"DL_P={s['dlp']:.2f}")
            lines.append(f"  {s['name']}({s['code']}) {s['level']} {s['op']} 缺:{'+'.join(missing)}")
        lines.append("")

    if not any([confirmed, near, alerts, breakdowns, confirmed_sells, near_sells]):
        lines.append("✓ 无信号, 持仓合规")

    return '\n'.join(lines)


def format_scan_summary(scan_data, ts):
    """Format full scan results into clean Telegram message."""
    lines = [f"📊 收盘扫描报告 {ts}", ""]

    result = scan_data.get("scan_result", {})
    selected = scan_data.get("selected", [])
    errors = scan_data.get("errors", 0)

    total = result.get("total_scanned", 0) or len(result.get("near", []))
    near_count = len(result.get("near", []))

    lines.append(f"扫描: {total}只 | 接近确认: {near_count}只")
    lines.append(f"候选池写入: {len(selected)}只 | 公式错误: {errors}")
    lines.append("")

    if selected:
        lines.append("候选池明细:")
        for s in selected[:15]:
            missing = []
            if s.get("ratio", 0) >= 60: missing.append(f"ratio={s['ratio']:.0f}%")
            if s.get("dlp", 0) <= 0.8: missing.append(f"DL_P={s['dlp']:.2f}")
            note = f" 缺:{'+'.join(missing)}" if missing else " ✓确认"
            lines.append(f"  {s.get('name','')} {s['code']} ¥{s['price']:.2f}{note}")
        if len(selected) > 15:
            lines.append(f"  ...还有{len(selected)-15}只")

    return '\n'.join(lines)


def format_compliance_summary(account, holdings, issues, ts):
    """Format compliance check results into clean Telegram message."""
    lines = [f"📝 合规核查 {ts}", ""]

    total_asset = account.get("total_asset", 0) or 0
    cash = account.get("cash", 0) or 0
    pos_ratio = account.get("position_ratio", 0) or 0

    lines.append(f"总资产: ¥{total_asset:.2f}")
    lines.append(f"现金: ¥{cash:.2f}")
    lines.append(f"仓位: {pos_ratio:.1%}")
    lines.append(f"持仓: {len(holdings)}只")
    lines.append("")

    if issues:
        lines.append(f"⚠️ 合规告警 ({len(issues)}项):")
        for issue in issues:
            lines.append(f"  {issue}")
        lines.append("")
    else:
        lines.append("✓ 持仓合规")
        lines.append("")

    lines.append("📝 心态日志提醒:")
    lines.append("  • 非工作流看盘次数 (0=最好)")
    lines.append("  • 是否追价操作 (0=最好)")
    lines.append("  • 是否按系统执行 (5=最好)")
    lines.append("  • 情绪状态 (5=最好)")
    lines.append("  • 止损外操作 (0=最好)")
    lines.append("  达标线: 80分")

    return '\n'.join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: daily_workflow.py [compliance|scan|account|holdings]")
        return
    cmd = sys.argv[1]
    ts = datetime.now().strftime('%m-%d %H:%M')

    try:
        if cmd == "compliance":
            account, holdings, issues = check_compliance()
            msg = format_compliance_summary(account, holdings, issues, ts)
            send_telegram(msg)
        elif cmd == "scan":
            scan_data = run_full_scan()
            msg = format_scan_summary(scan_data, ts)
            send_telegram(msg)
        elif cmd == "intraday":
            result = run_intraday_scan()
            msg = format_intraday_summary(result, ts)
            send_telegram(msg)
        elif cmd == "account":
            a = get_account_summary()
            def safe(v):
                if isinstance(v, (datetime, date)): return str(v)
                if isinstance(v, Decimal): return float(v)
                return v
            print(json.dumps({k:safe(v) for k,v in a.items()}, ensure_ascii=False, indent=2))
        elif cmd == "holdings":
            h = get_today_holdings()
            print(json.dumps(h, ensure_ascii=False, indent=2))
        else:
            print(f"未知命令: {cmd}")
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"ERROR: {err}")
        send_telegram(f"❌ 工作流错误 ({cmd}) {ts}\n\n{str(e)}")
        raise

if __name__ == '__main__':
    main()
