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
    """读取持仓表, 按代码去重(取最后一次出现的行)

    BUG修复 (2026-07-26): 万华已清仓仍显示持有
    根因: 持仓表存在多日重复录入, 旧行(100股)和新行(0股)共存
    修复: 以代码为key, 后出现的行覆盖先出现的行
    """
    wb = load_workbook(WB, data_only=True)
    ws = wb['持仓表']
    code_map = {}  # 按代码去重, 取最后一行
    for r in range(2, ws.max_row + 1):
        name = ws.cell(row=r, column=2).value
        if not name:
            continue
        code = str(ws.cell(row=r, column=3).value or '')
        waived = ws.cell(row=r, column=4).value
        shares = ws.cell(row=r, column=7).value
        entry = ws.cell(row=r, column=8).value
        close = ws.cell(row=r, column=9).value
        stop = ws.cell(row=r, column=16).value
        action = ws.cell(row=r, column=28).value
        profit = ws.cell(row=r, column=13).value
        pos = ws.cell(row=r, column=14).value
        # 始终用后出现的行覆盖 (最新数据)
        code_map[code] = {
            "name": name, "code": code, "waived": waived,
            "shares": shares, "entry": entry, "close": close,
            "stop": stop, "action": action, "profit": profit, "pos": pos
        }
    # 过滤掉0股的(已清仓)
    holdings = [h for h in code_map.values() if h.get('shares') and h['shares'] > 0]
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

def get_dynamic_position_cap(code, cost, close):
    """
    动态仓位上限计算 V3 (2026-07-26): 多级别DL_P共振

    替代单级别二买/三买检测, 用DL_P+Ratio+valid+多级别共振
    解决: 一买DL_P变动导致核心池不稳定 + ratio/级别本身有bug

    多级别共振机制:
      一买建仓: 35% (基础上限)
      二买加仓: 50% (日线一买valid + 30min+5min DL_P确认)
      三买加仓: 60% (日线趋势up + 30min+5min 双趋势背驰)

    候选池分层(核心池不再因一买DL_P每天变动而改变):
      核心池: 日线DL_P>=0.6 + 30min DL_P>=0.6 (双趋势背驰, 1-2周稳定)
      观察池: 日线DL_P>=0.6 + 30min DL_P>=0.4 (3-5天稳定)
      边缘池: 日线DL_P>=0.4 (每天变动, 仅观察)

    条件闭环:
      1. 买点升级: 多级别DL_P共振确认
      2. 浮盈护垫: 浮盈>=5%才允许加仓
    """
    global dynamic_cap_info
    dynamic_cap_info = {"entry": "一买", "pnl_pct": 0, "cap": 0.35, "tier": "边缘池"}

    if cost <= 0 or close <= 0:
        return 0.35

    pnl_pct = (close - cost) / cost
    dynamic_cap_info["pnl_pct"] = pnl_pct

    # 条件2: 浮盈护垫
    if pnl_pct < 0.05:
        return 0.35

    # 条件1: 多级别DL_P共振检测
    sys.path.insert(0, BEICHI_DIR)
    from beichi_analyzer import detect_multilevel_buy_signals

    try:
        ml = detect_multilevel_buy_signals(code, price=close)
    except:
        ml = {}

    tier = ml.get("tier", "边缘池")
    ermai = ml.get("ermai")
    sanmai = ml.get("sanmai")

    dynamic_cap_info["tier"] = tier
    dynamic_cap_info["daily_dl_p"] = ml.get("daily_dl_p", 0)
    dynamic_cap_info["30min_dl_p"] = ml.get("30min_dl_p", 0)
    dynamic_cap_info["5min_dl_p"] = ml.get("5min_dl_p", 0)

    best_entry = "一买"
    best_dl_prob = 0

    if sanmai and sanmai.get("valid"):
        best_entry = "三买"
        best_dl_prob = sanmai.get("dl_prob", 0)
    elif ermai and ermai.get("valid"):
        best_entry = "二买"
        best_dl_prob = ermai.get("dl_prob", 0)

    dynamic_cap_info["entry"] = best_entry
    dynamic_cap_info["dl_prob"] = best_dl_prob

    # 动态上限表: 买点级别 × 浮盈护垫
    cap_table = {
        "一买": 0.35,
        "二买": 0.50 if pnl_pct >= 0.05 else 0.35,
        "三买": 0.60 if pnl_pct >= 0.10 else (0.50 if pnl_pct >= 0.05 else 0.35),
    }

    cap = cap_table.get(best_entry, 0.35)
    dynamic_cap_info["cap"] = cap
    return cap


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
    # 【动态仓位上限】(2026-07-26): 修复重仓合规悖论
    # 旧逻辑: 静态35%上限 → 趋势加仓被阻止 → 错过趋势收益
    # 新逻辑: 买点升级+浮盈护垫+止损上移 → 动态提升上限
    for h in holdings:
        if h['waived'] == '是':
            continue
        if h['close'] and h['stop'] and h['close'] <= h['stop']:
            issues.append(f"⚠️ {h['name']}已破止损: 现价{h['close']:.2f}<=止损{h['stop']:.2f}")
        
        # 动态仓位上限检查
        if h['pos'] and h['pos'] > 0.35:
            code = str(h['code'])
            cost = h['entry'] or 0
            close = h['close'] or 0
            
            # 计算动态上限
            dynamic_cap = get_dynamic_position_cap(code, cost, close)
            tier = dynamic_cap_info.get("tier", "边缘池")
            if h['pos'] > dynamic_cap:
                issues.append(
                    f"⚠️ {h['name']}仓位超限: {h['pos']:.1%}>动态上限{dynamic_cap:.0%}"
                    f"(买点={dynamic_cap_info.get('entry','?')} 分层={tier} 浮盈={dynamic_cap_info.get('pnl_pct',0):.1%})"
                )
            else:
                print(f"  ✓ {h['name']}仓位{h['pos']:.1%} <= 动态上限{dynamic_cap:.0%} "
                      f"[{tier}] (多级别共振合规)")

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
    """全市场候选扫描: 沪A主板全量 + 深市全量(000/002) + 写入候选池(排除持仓股)

    分层候选池 (2026-07-26):
      核心池(DL_P>0.90+ratio<20%): 调仓首选, 1-2周稳定
      观察池(DL_P 0.85-0.90): 核心池不足时补充
      边缘池(DL_P 0.80-0.85): 仅观察不买入
    写入Excel时: 核心池优先 → 观察池补充 → 边缘池末尾, 备注列标注层级
    """
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
    all_confirmed = [r for r in result["confirmed"] if r["code"] not in held_codes]
    if held_codes:
        excluded = len(result["confirmed"]) - len(all_confirmed)
        if excluded:
            print(f"  排除持仓股: {excluded}只 ({', '.join(sorted(held_codes))})")

    if not all_confirmed:
        print("\n候选池: 无确认标的(排除持仓后), 跳过写入")
        return result

    # ============================================================
    # 婴儿级候选池 V2 (2026-07-26): 旧分层逻辑 + 沪深各5只共10只
    #
    # 保留:
    #   - 分层: 核心/观察/边缘 (ratio<20%限制)
    #   - 颜色: 黄(核心)/蓝(观察)/灰(边缘)
    #   - 列11公式自动分层
    # 仅修改:
    #   - 沪深各15只 → 沪深各5只 (共10只)
    # ============================================================
    core = [r for r in all_confirmed if r.get("tier") == "核心"]
    watch = [r for r in all_confirmed if r.get("tier") == "观察"]
    edge = [r for r in all_confirmed if r.get("tier") == "边缘"]

    # 沪深各取, 优先核心池 (10W规模以下不考虑300/301创业板)
    def split_sz_sha(stocks):
        sha = sorted([r for r in stocks if r["code"].startswith("6")], key=lambda x: (-x["dlp"], x["ratio"]))
        sza = sorted([r for r in stocks if r["code"].startswith("0")], key=lambda x: (-x["dlp"], x["ratio"]))
        return sha, sza

    # 核心池先选 (沪深各5只)
    core_sha, core_sz = split_sz_sha(core)
    selected = core_sha[:5] + core_sz[:5]

    # 核心池不足5只/边, 用观察池补
    if len(core_sha) < 5:
        watch_sha, _ = split_sz_sha(watch)
        selected += watch_sha[:5 - len(core_sha)]
    if len(core_sz) < 5:
        _, watch_sz = split_sz_sha(watch)
        selected += watch_sz[:5 - len(core_sz)]

    # 仍不足, 用边缘池按沪深分别补
    selected_codes = {s["code"] for s in selected}
    # 统计当前沪深数量
    cur_sha_cnt = sum(1 for s in selected if s["code"].startswith("6"))
    cur_sz_cnt = sum(1 for s in selected if s["code"].startswith("0"))
    sha_need = max(0, 5 - cur_sha_cnt)
    sz_need = max(0, 5 - cur_sz_cnt)
    # 边缘池按沪深分别补
    edge_sha, edge_sz = split_sz_sha(edge)
    selected += edge_sha[:sha_need]
    selected += edge_sz[:sz_need]
    # 边缘池仍不足, 用剩余观察池补
    if len(selected) < 10:
        still_need = 10 - len(selected)
        watch_remaining = [r for r in watch if r["code"] not in selected_codes]
        wr_sha, wr_sz = split_sz_sha(watch_remaining)
        selected += (wr_sha + wr_sz)[:still_need]

    selected.sort(key=lambda x: (0 if x.get("tier") == "核心" else 1 if x.get("tier") == "观察" else 2, -x["dlp"]))

    print(f"\n[婴儿级候选池] 分层: 核心{len(core)}只 + 观察{len(watch)}只 + 边缘{len(edge)}只")
    print(f"写入: {len(selected)}只 (沪深各5只, 共10只)")
    print(f"  核心{len([s for s in selected if s.get('tier')=='核心'])} + 观察{len([s for s in selected if s.get('tier')=='观察'])} + 边缘{len([s for s in selected if s.get('tier')=='边缘'])}")

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
    # 分层映射: 中文简称 → 候选池显示名称
    tier_display = {
        "核心": "核心池",
        "观察": "观察池",
        "边缘": "边缘池",
    }
    # 分层颜色 (与fix_tier.py一致): 黄/蓝/灰
    from openpyxl.styles import PatternFill, Font as XFont
    tier_fill = {
        "核心": PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid"),  # 黄
        "观察": PatternFill(start_color="D6E4F0", end_color="D6E4F0", fill_type="solid"),  # 蓝
        "边缘": PatternFill(start_color="E0E0E0", end_color="E0E0E0", fill_type="solid"),  # 灰
    }
    for idx, stock in enumerate(selected):
        row = 2 + idx
        price = stock["price"]
        fund = calc_funding(price, result["total_asset"], result["cash"])
        tier = stock.get("tier", "边缘")
        tier_name = tier_display.get(tier, "边缘池")

        # 备注列: 仅保留资金转入信息(分层已由列11公式自动计算)
        note = ""
        if fund["need_transfer"]: note = "需转入%.0f元" % fund["transfer"]

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
        # 列11: 分层Tier — 公式自动计算(ratio<20%+DL_P>0.90=核心池)
        ws.cell(row=row, column=11, value=f'=IF($A{row}="","",IF(AND(G{row}<20%,H{row}>0.90),"核心池",IF(AND(H{row}>=0.85),"观察池",IF(AND(H{row}>=0.80),"边缘池",""))))')
        ws.cell(row=row, column=11).font = XFont(bold=True, size=11)
        # 行颜色: 按分层着色 (黄/蓝/灰)
        fill = tier_fill.get(tier)
        if fill:
            for c in range(1, 32):
                ws.cell(row=row, column=c).fill = fill
        # 列31备注: 仅资金信息
        if note:
            ws.cell(row=row, column=31, value=note)

    wb.save(WB)
    recalc_result = recalc()
    errors = recalc_result.get("total_errors", -1)

    print(f"\n候选池: 写入{len(selected)}只 (核心{len([s for s in selected if s.get('tier')=='核心'])} + 观察{len([s for s in selected if s.get('tier')=='观察'])} + 边缘{len([s for s in selected if s.get('tier')=='边缘'])})")
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

    【BUG-8修复 (2026-07-27)】跳过1min级别
    问题: 1min级别需要build_1min_from_5min → 2次网络请求/只
          7只持仓 × 2 = 14次额外请求, 每次最多10秒timeout
          → GitHub Actions 10分钟超时, 工作流卡死
    修复: intraday scan中跳过1min级别(持仓级别判断不需要1min精度)
    """
    sys.path.insert(0, BEICHI_DIR)
    from beichi_analyzer import analyze_beichi
    levels_priority = ["日线", "30min", "5min"]  # BUG-8: 跳过1min(网络开销过大)
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
    from beichi_analyzer import detect_sell_signals
    import time as _time

    now = datetime.now()
    print(f"=== 盘中扫描 {now.strftime('%Y-%m-%d %H:%M')} ===\n")

    # 候选池排除持仓股
    holdings = get_today_holdings()
    held_codes = {str(h['code']) for h in holdings if h.get('code')}

    # 1. 候选池30min扫描
    # 【BUG-9修复 (2026-07-27)】候选池为空时不再跳过持仓检查
    # 旧代码: candidates为空 → return early → 持仓止损/卖点检查全部跳过
    # 根因: BUG-9导致scan命令从未执行 → 候选池可能为空 → 持仓监控静默失效
    # 修复: 候选池为空时跳过候选扫描, 但继续执行持仓检查(步骤3)
    candidates = get_candidate_pool()
    candidates = [c for c in candidates if c["code"] not in held_codes]
    confirmed_30m = []
    near_30m = []
    if not candidates:
        print("[1/3] 候选池为空(排除持仓后), 跳过候选扫描, 继续持仓检查")
    else:
        print(f"[1/3] 候选池30min扫描 ({len(candidates)}只)...")
        t0 = _time.time()
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

        # 【BUG修复 (2026-07-26)】一买区持仓中枢破位误报
        #
        # BUG根因:
        #   detect_entry_level判断成本在中枢下方 → zone="中枢下方(一买区)"
        #   即: 成本 < last_zs["zd"] (成本本就低于中枢下沿)
        #   此时若现价也低于zd(正常,因为一买区持仓本就在中枢下方)
        #   中枢破位检查 close_price < zd 会触发误报
        #
        #   日志证据(2026-07-27 07:29运行):
        #     创力集团 成本8.02 中枢下方(一买区) 现价8.03 跌破下沿8.18
        #     新五丰 成本4.80 中枢下方(一买区) 现价4.73 跌破下沿4.88
        #     方盛制药 成本9.27 中枢下方(一买区) 现价8.96 跌破下沿9.89
        #     建研院 成本3.97 中枢下方(一买区) 现价4.02 跌破下沿4.03
        #     曲美家居 成本3.14 中枢下方(一买区) 现价3.09 跌破下沿3.18
        #   → 全部是一买区持仓, 现价<zd是正常状态, 不是破位
        #
        # 修复策略:
        #   一买区持仓(cost < zd): 中枢破位判定改为"现价跌破成本价"而非"跌破中枢下沿"
        #   中枢内持仓(zd <= cost <= zg): 维持原逻辑"跌破中枢下沿"
        #   中枢上方持仓(cost > zg): 维持原逻辑"跌破中枢下沿"
        is_one_buy_zone = zone_desc in ("中枢下方(一买区)", "全中枢下方(深度一买区)") if cost > 0 else False

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
                # 【BUG修复】一买区持仓: 改为跌破成本价才算破位
                zss = r.get("zss", [])
                if zss and close_price > 0:
                    last_zs = zss[-1]
                    zd = last_zs["zd"]
                    zg = last_zs["zg"]
                    if is_one_buy_zone:
                        # 一买区持仓: 现价跌破成本价才是破位
                        if cost > 0 and close_price < cost:
                            pct = ((close_price - cost) / cost) * 100
                            zs_breakdowns.append({
                                "name": name, "code": code, "level": level,
                                "price": close_price, "zd": cost, "zg": zg, "pct": pct,
                                "waived": waived,
                                "breakdown_type": "一买区跌破成本",
                            })
                    else:
                        # 正常持仓: 现价跌破中枢下沿才是破位
                        if close_price < zd:
                            pct = ((close_price - zd) / zd) * 100
                            zs_breakdowns.append({
                                "name": name, "code": code, "level": level,
                                "price": close_price, "zd": zd, "zg": zg, "pct": pct,
                                "waived": waived,
                                "breakdown_type": "跌破中枢下沿",
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

        # 3d. 【BUG-7修复 (2026-07-27)】接入 detect_sell_signals 综合卖出评估
        #
        # 问题: detect_sell_signals 函数存在但从未被调用
        #   包含5条规则: 日线down+30min看空→减仓, 弱信号+密集看空→清仓, 亏损+down→清仓
        #   这些规则比 ratio/dlp 硬阈值更贴近实战, 能捕获"卖点未确认但风险已高"的场景
        #
        # 修复: 对每只持仓调用 detect_sell_signals, should_clear 自动升级为确认卖点
        if cost > 0 and close_price > 0 and waived != '是':
            try:
                sell_eval = detect_sell_signals(code, cost, close_price)
                if sell_eval["should_clear"]:
                    # should_clear 最高优先级 → 直接升级为确认卖点
                    sell_signals.append({
                        "name": name, "code": code, "level": "综合",
                        "op": "清仓", "ratio": 0, "dlp": 0, "valid": True,
                        "type": "确认卖点",
                        "reason": sell_eval["reason"],
                        "risk_level": sell_eval["risk_level"],
                        "source": "detect_sell_signals",
                    })
                    print(f"  🔴 {name}({code}) 综合清仓信号: {sell_eval['reason']} [风险={sell_eval['risk_level']}]")
                elif sell_eval["should_reduce"]:
                    # should_reduce → 接近卖点(附带原因)
                    sell_signals.append({
                        "name": name, "code": code, "level": "综合",
                        "op": "减仓", "ratio": 0, "dlp": 0, "valid": True,
                        "type": "接近卖点",
                        "reason": sell_eval["reason"],
                        "risk_level": sell_eval["risk_level"],
                        "source": "detect_sell_signals",
                    })
                    print(f"  🟡 {name}({code}) 综合减仓信号: {sell_eval['reason']} [风险={sell_eval['risk_level']}]")
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
            bd_type = z.get("breakdown_type", "跌破中枢下沿")
            print(f"    {z['name']}({z['code']}) {z['level']} 现价{z['price']:.2f} {bd_type}{z['zd']:.2f} ({z['pct']:+.1f}%){waived_tag}")

    # 【加仓信号检测】(2026-07-26): 二买/三买 → 动态仓位上限
    # 【BUG-9修复 (2026-07-27)】dynamic_cap_info未初始化导致NameError
    # 旧代码: 若所有持仓cost=0或close=0, get_dynamic_position_cap从未被调用
    #         → dynamic_cap_info全局变量未定义 → NameError崩溃 → Telegram推送失败
    # 修复: 在循环前初始化默认值
    dynamic_cap_info = {"entry": "一买", "pnl_pct": 0, "cap": 0.35, "tier": "边缘池",
                         "daily_dl_p": 0, "30min_dl_p": 0, "5min_dl_p": 0}
    add_signals = []
    print(f"\n  📈 加仓信号检测(多级别DL_P共振):")
    for h in holdings:
        code = str(h['code'])
        cost = h['entry'] or 0
        close = h['close'] or 0
        if cost <= 0 or close <= 0:
            continue

        pnl_pct = (close - cost) / cost
        dynamic_cap = get_dynamic_position_cap(code, cost, close)
        entry_level = dynamic_cap_info.get("entry", "一买")
        tier = dynamic_cap_info.get("tier", "边缘池")
        d_dp = dynamic_cap_info.get("daily_dl_p", 0)
        m30_dp = dynamic_cap_info.get("30min_dl_p", 0)
        m5_dp = dynamic_cap_info.get("5min_dl_p", 0)

        if entry_level in ("二买", "三买") and pnl_pct >= 0.05:
            add_signals.append({
                "name": h['name'], "code": code, "entry": entry_level,
                "pnl_pct": pnl_pct, "dynamic_cap": dynamic_cap,
                "current_pos": h.get('pos', 0), "tier": tier,
            })
            remaining = dynamic_cap - (h.get('pos', 0) or 0)
            print(f"    ★ {h['name']}({code}) {entry_level}信号 [{tier}] → 动态上限{dynamic_cap:.0%} "
                  f"当前仓位{h.get('pos',0):.1%} 浮盈{pnl_pct:.1%} 可加仓空间{remaining:.1%}")
            print(f"      DL_P: 日线={d_dp:.2f} 30min={m30_dp:.2f} 5min={m5_dp:.2f}")
        else:
            print(f"    · {h['name']}({code}) [{tier}] 上限{dynamic_cap:.0%} "
                  f"DL_P: 日线={d_dp:.2f} 30min={m30_dp:.2f} 5min={m5_dp:.2f}")

    if not add_signals:
        print(f"    无多级别共振信号, 所有持仓维持35%静态上限")

    # 卖点汇总
    # 【BUG-7修复 (2026-07-27)】中枢破位 + 接近卖点 = 自动升级为确认卖点
    #
    # 问题: 跌破中枢后一卖/二卖因 ratio/dlp/valid 未同时达标而停在"接近卖点"
    #       等待确认期间价格继续下跌 → 大幅回撤
    #
    # 修复: 同一股票同时出现"中枢破位"和"接近卖点"时, 自动升级为确认卖点
    #       理由: 中枢破位 = 趋势结构已破坏, 接近卖点 = 卖点正在形成
    #             两者叠加 = 不需等 ratio/dlp 完全确认, 应立即行动
    confirmed_sells = [s for s in sell_signals if s["type"] == "确认卖点"]
    near_sells = [s for s in sell_signals if s["type"] == "接近卖点"]

    # 中枢破位 + 接近卖点 → 自动升级
    if zs_breakdowns and near_sells:
        breakdown_codes = {z["code"] for z in zs_breakdowns if z["waived"] != "是"}
        upgraded = []
        remaining_near = []
        for s in near_sells:
            if s["code"] in breakdown_codes:
                s["type"] = "确认卖点"
                s["upgraded_from"] = "中枢破位+接近卖点自动升级"
                upgraded.append(s)
            else:
                remaining_near.append(s)
        if upgraded:
            near_sells = remaining_near
            confirmed_sells.extend(upgraded)
            print(f"  ⚡ 自动升级: {len(upgraded)}只接近卖点(中枢破位叠加) → 确认卖点")
            for s in upgraded:
                print(f"    ↗ {s['name']}({s['code']}) {s['level']} {s['op']} → 确认卖出")

    if confirmed_sells:
        print(f"  🔴 确认卖点: {len(confirmed_sells)}个")
    if near_sells:
        print(f"  🟡 接近卖点: {len(near_sells)}个")
        for s in near_sells[:5]:
            missing = []
            if s.get("ratio", 0) >= 60: missing.append("ratio=%d%%" % s["ratio"])
            if s.get("dlp", 0) <= 0.8: missing.append("DL_P=%.2f" % s["dlp"])
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
        "add_signals": add_signals,
        "scanned": len(candidates),
    }

def send_telegram(text, title=""):
    """Send message to Telegram. Splits long messages automatically.

    彻底修复 (2026-07-26): 5次失败的根因分析
    1. parse_mode=HTML导致消息中<被当作HTML标签 → HTTP 400 "can't parse entities"
       修复: 完全移除parse_mode, 用纯文本发送
    2. send_telegram失败时只print不raise → 工作流显示"success"但消息没发出
       修复: 失败时raise RuntimeError, 确保错误传播到workflow
    3. 工作流挂起(analyze_beichi无缓存) → 40+分钟未到达send_telegram
       修复: beichi_analyzer.py添加内存缓存 + workflow.yml添加timeout

    本函数不再静默失败: token缺失或HTTP错误都会raise异常
    """
    token = (os.environ.get('TELEGRAM_BOT_TOKEN')
             or os.environ.get('TG_TOKEN')
             or os.environ.get('TELEGRAM_TOKEN'))
    chat_id = (os.environ.get('TELEGRAM_CHAT_ID')
               or os.environ.get('TG_CHAT_ID')
               or os.environ.get('TELEGRAM_CHATID'))

    tg_keys = [k for k in sorted(os.environ.keys()) if 'TELEGRAM' in k.upper() or 'TG_' in k.upper()]
    print(f"[TG] env vars: {tg_keys}")
    print(f"[TG] token: {'YES' if token else 'NO'}, chat_id: {'YES' if chat_id else 'NO'}")

    if not token or not chat_id:
        msg = f"Telegram token或chat_id缺失 (token={'YES' if token else 'NO'}, chat_id={'YES' if chat_id else 'NO'})"
        print(f"[TG] FAIL: {msg}")
        print("=" * 40)
        if title:
            print(title)
        print(text)
        print("=" * 40)
        raise RuntimeError(msg)

    # 转义<>为全角字符 (不使用parse_mode, 纯文本模式)
    # 注意: 不替换& — 不用parse_mode时&不需要HTML转义, 替换会导致显示&amp;
    def escape_tg(s):
        if not s:
            return s
        s = s.replace('<', '＜')
        s = s.replace('>', '＞')
        return s

    full_text = f"{title}\n{text}" if title else text
    full_text = escape_tg(full_text)

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

    errors = []
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
            err_msg = f"chunk {i+1} HTTP {e.code}: {err_body}"
            print(f"[TG] ERROR: {err_msg}")
            errors.append(err_msg)
        except Exception as e:
            err_msg = f"chunk {i+1} {type(e).__name__}: {e}"
            print(f"[TG] ERROR: {err_msg}")
            errors.append(err_msg)
        if i < len(chunks) - 1:
            time.sleep(1)

    if errors:
        raise RuntimeError(f"Telegram发送失败({len(errors)}/{len(chunks)}): {'; '.join(errors)}")


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
            bd_type = z.get("breakdown_type", "跌破中枢下沿")
            lines.append(f"  {z['name']}({z['code']}) {z['level']} ¥{z['price']:.2f} {bd_type}{z['zd']:.2f} ({z['pct']:+.1f}%)")
        if len(non_waived) > 5:
            lines.append(f"  ...还有{len(non_waived)-5}个")
        lines.append("")

    # 卖点
    confirmed_sells = result.get("confirmed_sells", [])
    near_sells = result.get("near_sells", [])
    if confirmed_sells:
        lines.append(f"🔴 确认卖点: {len(confirmed_sells)}个 (建议卖出)")
        for s in confirmed_sells:
            src = s.get("source", "")
            reason = s.get("reason", "")
            upgraded = s.get("upgraded_from", "")
            if src == "detect_sell_signals":
                lines.append(f"  {s['name']}({s['code']}) {s['op']} {reason}")
            elif upgraded:
                lines.append(f"  {s['name']}({s['code']}) {s['level']} {s['op']} [{upgraded}]")
            else:
                lines.append(f"  {s['name']}({s['code']}) {s['level']} {s['op']} ratio={s.get('ratio',0):.0f}% DL_P={s.get('dlp',0):.2f}")
        lines.append("")
    if near_sells:
        lines.append(f"🟡 接近卖点: {len(near_sells)}个")
        for s in near_sells[:5]:
            reason = s.get("reason", "")
            if reason:
                lines.append(f"  {s['name']}({s['code']}) {s['op']} {reason}")
            else:
                missing = []
                if s.get("ratio", 0) >= 60: missing.append(f"ratio={s['ratio']:.0f}%")
                if s.get("dlp", 0) <= 0.8: missing.append(f"DL_P={s['dlp']:.2f}")
                lines.append(f"  {s['name']}({s['code']}) {s['level']} {s['op']} 缺:{'+'.join(missing)}")
        lines.append("")

    if not any([confirmed, near, alerts, breakdowns, confirmed_sells, near_sells]):
        lines.append("✓ 无信号, 持仓合规")

    # 加仓信号
    add_signals = result.get("add_signals", [])
    if add_signals:
        lines.append("")
        lines.append(f"📈 趋势加仓信号(动态仓位): {len(add_signals)}只")
        for s in add_signals:
            remaining = s["dynamic_cap"] - s["current_pos"]
            lines.append(f"  ★ {s['name']}({s['code']}) {s['entry']} → 上限{s['dynamic_cap']:.0%} "
                         f"浮盈{s['pnl_pct']:.1%} 可加{remaining:.1%}")

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


def run_weekly_review():
    """周复盘: 复利目标 + R值统计 + 账户增长 + 做T/建清仓 → 写入周复盘表W-AK列

    列映射:
      W(23) 日复利目标完成    X(24) 周复利目标完成    Y(25) 月复利目标完成
      Z(26) 当下累计复利率    AA(27) 当下正期望R值    AB(28) 系统盈亏比
      AC(29) 账户规模增长     AD(30) 增长目标进度      AE(31) 复利追踪备注
      AF(32) 做T次数/成功率   AG(33) 建清仓成功率       AH(34) 平均持仓天数

    修正 (2026-07-26):
      - 本金取值: 列27(AA)=硬编码值, 改用 总资产-净入金后盈利(列43) = 实际投入本金
      - 新增做T统计: 同日买卖识别 + 成功率
      - 新增建清仓成功率: FIFO买卖配对完整周期
    """
    from openpyxl.styles import PatternFill, Font as XFont, Alignment, Border, Side

    SUMMARY_START = 204  # 交易记录统计区域起始行

    # ============================================================
    # 1. 读取账户数据
    # ============================================================
    wb_d = load_workbook(WB, data_only=True)
    ws_acc = wb_d['账户总表']
    latest_row = 2
    for r in range(2, ws_acc.max_row + 1):
        if ws_acc.cell(row=r, column=1).value:
            latest_row = r

    current_asset = ws_acc.cell(row=latest_row, column=2).value or 0
    if isinstance(current_asset, str): current_asset = 0
    compound_dev = ws_acc.cell(row=latest_row, column=31).value or 0
    if isinstance(compound_dev, str): compound_dev = 0

    # ============================================================
    # 双复利模型 (2026-07-26修订):
    #   主用TWR: 以总资产为基数, 投入本金为基准 (合规审计/国际标准)
    #   辅用动态本金: 以持仓市值为基数 (交易信号评估)
    # ============================================================
    position_value = ws_acc.cell(row=latest_row, column=4).value or 0
    if isinstance(position_value, str): position_value = 0
    cash = ws_acc.cell(row=latest_row, column=3).value or 0
    if isinstance(cash, str): cash = 0

    # 持仓市值可能为0(历史数据缺失), 从持仓表重新计算
    if position_value == 0:
        ws_hold_d = wb_d['持仓表']
        for r in range(2, ws_hold_d.max_row + 1):
            name = ws_hold_d.cell(row=r, column=2).value
            if not name:
                continue
            shares = ws_hold_d.cell(row=r, column=7).value or 0
            close = ws_hold_d.cell(row=r, column=9).value or 0
            waived = ws_hold_d.cell(row=r, column=4).value
            if shares and close and not isinstance(shares, str) and not isinstance(close, str):
                if float(shares) > 0 and float(close) > 0:
                    position_value += float(shares) * float(close)

    # 动态本金 = 持仓市值 (复利基数 - 辅用)
    dynamic_principal = position_value
    position_ratio = position_value / current_asset if current_asset > 0 else 0

    # 净盈利(列43)
    real_pnl = ws_acc.cell(row=latest_row, column=43).value or 0
    if isinstance(real_pnl, str): real_pnl = 0

    # ============================================================
    # TWR复利模型 (总资产基数 - 国际标准/合规审计用)
    #   twr_principal = 总资产 - 净盈利 = 实际投入本金
    # ============================================================
    twr_principal = current_asset - float(real_pnl)
    if twr_principal <= 0:
        # 回退: 用列27硬编码值
        for r in range(2, latest_row + 1):
            v = ws_acc.cell(row=r, column=27).value
            if v is not None and not str(v).startswith('=') and not isinstance(v, str):
                twr_principal = float(v)
                break

    # 投入本金(参考)
    invested_capital = twr_principal

    # 运行月数
    start_date = datetime(2026, 1, 31)
    end_date = datetime.now()
    months_running = (end_date.year - start_date.year) * 12 + (end_date.month - start_date.month)
    if end_date.day < start_date.day:
        months_running -= 1
    months_precise = months_running + (end_date.day - start_date.day if end_date.day >= start_date.day else end_date.day + 31 - start_date.day) / 31.0

    # 目标年化: 基于总资产的TWR目标 (随账户阶段递减)
    #   婴儿(<3W): 3%    幼儿(3W-10W): 2.5%    成长(10W-30W): 2%    成熟(30W+): 1.5%
    if current_asset < 30000:
        target_annual = 0.03
    elif current_asset < 100000:
        target_annual = 0.025
    elif current_asset < 300000:
        target_annual = 0.02
    else:
        target_annual = 0.015

    total_profit = float(real_pnl)

    # 1. TWR复利 (总资产基数 - 主用)
    twr_total_return = (current_asset - twr_principal) / twr_principal if twr_principal > 0 else 0
    twr_annualized_return = ((current_asset / twr_principal) ** (12 / months_precise) - 1) if twr_principal > 0 and months_precise > 0 else 0

    # 2. 动态本金复利 (持仓市值基数 - 辅用)
    dynamic_total_return = total_profit / dynamic_principal if dynamic_principal > 0 else 0
    dynamic_annualized_return = ((1 + dynamic_total_return) ** (12 / months_precise) - 1) if dynamic_total_return > 0 and months_precise > 0 else 0

    # 复利目标: 日/周/月 (基于动态本金)
    daily_target = (1 + target_annual) ** (1/250) - 1
    weekly_target = (1 + target_annual) ** (5/250) - 1
    monthly_target = (1 + target_annual) ** (1/12) - 1

    # ============================================================
    # 2. 读取周复盘数据 — 双模型收益率计算
    # ============================================================
    ws_rev_d = wb_d['周复盘']
    week_start = ws_rev_d.cell(row=2, column=2).value or 0
    week_end = ws_rev_d.cell(row=2, column=3).value or 0
    week_return = ws_rev_d.cell(row=2, column=4).value or 0
    if isinstance(week_start, str): week_start = 0
    if isinstance(week_end, str): week_end = 0
    if isinstance(week_return, str): week_return = 0

    # 周利润 = 周末总资产 - 周初总资产 (排除入金)
    week_profit = week_end - week_start

    # TWR周收益率 (主用): 利润 / 周初总资产
    twr_weekly_actual = week_profit / week_start if week_start > 0 else 0
    twr_daily_actual = (1 + twr_weekly_actual) ** (1/5) - 1 if twr_weekly_actual > -1 else 0

    # 动态本金周收益率 (辅用): 利润 / 持仓市值
    dynamic_weekly_actual = week_profit / dynamic_principal if dynamic_principal > 0 else 0
    dynamic_daily_actual = (1 + dynamic_weekly_actual) ** (1/5) - 1 if dynamic_weekly_actual > -1 else 0

    # 月复利: 上月末总资产 → 当前
    jun_end_asset = 18504.0  # 默认值
    for r in range(latest_row, 1, -1):
        d = ws_acc.cell(row=r, column=1).value
        if d and hasattr(d, 'month') and d.month == end_date.month - 1:
            jun_end_asset = ws_acc.cell(row=r, column=2).value or jun_end_asset
            break
    month_profit = current_asset - jun_end_asset

    # TWR月收益率 (主用)
    twr_monthly_actual = month_profit / jun_end_asset if jun_end_asset > 0 else 0

    # 动态本金月收益率 (辅用)
    dynamic_monthly_actual = month_profit / dynamic_principal if dynamic_principal > 0 else 0

    # 目标完成情况 (以TWR为主)
    daily_complete = "是" if twr_daily_actual >= daily_target else "否"
    weekly_complete = "是" if twr_weekly_actual >= weekly_target else "否"
    monthly_complete = "是" if twr_monthly_actual >= monthly_target else "否"

    # ============================================================
    # 3. 计算R值 (从交易记录) — FIFO买卖配对 + 代理止损
    # ============================================================
    # 修复 (2026-07-26):
    #   问题1: 买入行 buy_price(列22)=None, 实际买入价在 price(列6)
    #   问题2: 买入行 stop(列12)=None, 大部分无止损记录
    #   问题3: 同代码多次买卖, buy_map覆盖导致只匹配最后一笔
    #   方案: FIFO队列配对 + price列6作买入价 + 无止损时用5%代理止损
    ws_tr = wb_d['交易记录']
    trades = []
    buy_queues = {}  # code -> [buy_trade, ...] FIFO队列

    for r in range(2, SUMMARY_START):
        name = ws_tr.cell(row=r, column=2).value
        if not name:
            continue
        code = str(ws_tr.cell(row=r, column=3).value or "")
        direction = str(ws_tr.cell(row=r, column=4).value or "")
        price = ws_tr.cell(row=r, column=6).value      # 成交价格(买入行=买入价)
        shares = ws_tr.cell(row=r, column=7).value
        total = ws_tr.cell(row=r, column=8).value       # 成交金额
        stop = ws_tr.cell(row=r, column=12).value        # 止损价
        risk_col = ws_tr.cell(row=r, column=23).value    # W列止损风险额(可能为0)
        raw_date = ws_tr.cell(row=r, column=1).value     # 交易日期

        price = float(price) if not isinstance(price, str) and price is not None else 0
        shares = float(shares) if not isinstance(shares, str) and shares is not None else 0
        total = float(total) if not isinstance(total, str) and total is not None else 0
        stop = float(stop) if not isinstance(stop, str) and stop is not None else 0
        risk_col = float(risk_col) if not isinstance(risk_col, str) and risk_col is not None else 0
        date_str = raw_date.strftime('%Y-%m-%d') if isinstance(raw_date, datetime) else str(raw_date or "")

        t = {"row": r, "name": str(name), "code": code, "direction": direction,
             "price": price, "shares": shares, "total": total,
             "stop": stop, "risk_col": risk_col, "date_str": date_str}
        trades.append(t)
        if direction == "买入":
            buy_queues.setdefault(code, []).append(t)

    # FIFO配对计算R值
    r_values = []
    r_by_row = {}
    for t in trades:
        if t["direction"] not in ("卖出", "一卖"):
            continue
        queue = buy_queues.get(t["code"])
        if not queue:
            continue
        buy = queue.pop(0)  # FIFO: 取最早的买入

        # 买入价: 优先用buy行price(列6), 回退到sell行buy_price(列22)
        buy_price = buy["price"] if buy["price"] > 0 else 0

        # 止损风险额: 优先用W列记录值, 回退到(stop计算), 再回退到5%代理止损
        if buy["risk_col"] > 0:
            risk = buy["risk_col"]
        elif buy["stop"] > 0 and buy_price > 0:
            risk = (buy_price - buy["stop"]) * buy["shares"]
        else:
            # 代理止损: 买入价的5% (日线级别常见止损幅度)
            proxy_stop = buy_price * 0.95
            risk = (buy_price - proxy_stop) * buy["shares"] if buy_price > 0 else 0

        if risk > 0:
            r = (t["total"] - buy["total"]) / risk
        else:
            r = 0
        r = round(r, 6)
        r_values.append(r)
        r_by_row[t["row"]] = r

    avg_r = sum(r_values) / len(r_values) if r_values else 0
    total_r = sum(r_values)
    max_r = max(r_values) if r_values else 0
    max_loss_r = min(r_values) if r_values else 0
    wins = [r for r in r_values if r > 0]
    losses = [r for r in r_values if r < 0]
    if wins and losses:
        win_loss_ratio = (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))
    elif wins:
        win_loss_ratio = float('inf')
    else:
        win_loss_ratio = 0
    win_rate = len(wins) / len(r_values) * 100 if r_values else 0
    system_status = "正期望系统 ✅" if avg_r >= 0.5 else ("边缘系统 ⚠️" if avg_r >= 0.2 else "负期望系统 ❌")

    # ============================================================
    # 3.5 做T统计 + 建清仓周期 + 持仓天数
    # ============================================================
    from collections import defaultdict
    t0_count = 0
    t0_wins = 0
    cycle_count = 0
    cycle_wins = 0
    hold_days_list = []

    # 按日期+代码分组识别做T(同日买卖)
    by_date_code = defaultdict(list)
    for t in trades:
        date_str = t.get("date_str", "")
        if not date_str:
            # 从交易记录原始行重新读取日期
            raw_date = ws_tr.cell(row=t["row"], column=1).value
            if isinstance(raw_date, datetime):
                date_str = raw_date.strftime('%Y-%m-%d')
            else:
                date_str = str(raw_date or "")
            t["date_str"] = date_str
        by_date_code[(date_str, t["code"])].append(t)

    for (d, code), group in by_date_code.items():
        buys = [g for g in group if g["direction"] == "买入"]
        sells = [g for g in group if g["direction"] in ("卖出", "一卖")]
        if buys and sells:
            buy_total = sum(b["total"] for b in buys)
            sell_total = sum(s["total"] for s in sells)
            t0_count += 1
            if sell_total > buy_total:
                t0_wins += 1
    t0_rate = t0_wins / t0_count * 100 if t0_count > 0 else 0

    # 建清仓周期: FIFO配对(复用buy_queues已消费的队列, 需重新构建)
    code_trades = defaultdict(list)
    for t in trades:
        if t["direction"] in ("买入", "卖出", "一卖"):
            code_trades[t["code"]].append(t)
    for code, ctrades in code_trades.items():
        ctrades.sort(key=lambda x: x["row"])
        current_buy = None
        for t in ctrades:
            if t["direction"] == "买入":
                if current_buy is None:
                    current_buy = dict(t)
                else:
                    current_buy["total"] += t["total"]
                    current_buy["shares"] += t["shares"]
            elif t["direction"] in ("卖出", "一卖") and current_buy:
                pnl = t["total"] - current_buy["total"]
                cycle_count += 1
                if pnl > 0:
                    cycle_wins += 1
                # 持仓天数
                try:
                    buy_d = datetime.strptime(current_buy.get("date_str", ""), '%Y-%m-%d')
                    sell_d = datetime.strptime(t.get("date_str", ""), '%Y-%m-%d')
                    hold_days_list.append((sell_d - buy_d).days)
                except:
                    pass
                current_buy = None
    cycle_rate = cycle_wins / cycle_count * 100 if cycle_count > 0 else 0
    avg_hold_days = sum(hold_days_list) / len(hold_days_list) if hold_days_list else 0

    # ============================================================
    # 4. 账户增长 (2026-07-26修订: 围绕"幼儿"目标重新定义阶段)
    #   婴儿: <3W   (当前, 今年目标养到幼儿)
    #   幼儿: 3W-10W (今年目标, 围绕幼儿执行)
    #   成长: 10W-30W
    #   成熟: 30W+
    # ============================================================
    growth_target_1 = 30000   # 婴儿→幼儿
    growth_target_2 = 100000  # 幼儿→成长
    growth_target_3 = 300000  # 成长→成熟
    if current_asset < 30000:
        current_stage = "婴儿"
        growth_progress = (current_asset / growth_target_1) * 100
        next_target = growth_target_1
        next_stage = "幼儿"
    elif current_asset < 100000:
        current_stage = "幼儿"
        growth_progress = (current_asset / growth_target_2) * 100
        next_target = growth_target_2
        next_stage = "成长"
    elif current_asset < 300000:
        current_stage = "成长"
        growth_progress = (current_asset / growth_target_3) * 100
        next_target = growth_target_3
        next_stage = "成熟"
    else:
        current_stage = "成熟"
        growth_progress = 100
        next_target = current_asset
        next_stage = "成熟"

    # ============================================================
    # 5. 写入Excel
    # ============================================================
    wb = load_workbook(WB)
    ws_rev = wb['周复盘']

    # 新列表头 (如果不存在则添加)
    new_headers = {
        23: "日复利目标完成", 24: "周复利目标完成", 25: "月复利目标完成",
        26: "当下累计复利率", 27: "当下正期望R值", 28: "系统盈亏比",
        29: "账户规模增长", 30: "增长目标进度", 31: "复利追踪备注",
        32: "做T统计", 33: "建清仓成功率", 34: "平均持仓天数",
    }
    header_font = XFont(bold=True, size=11, color="FFFFFF")
    header_fill = PatternFill(start_color="2F5496", end_color="2F5496", fill_type="solid")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )
    for col, title in new_headers.items():
        cell = ws_rev.cell(row=1, column=col)
        if not cell.value:
            cell.value = title
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = thin_border

    # 写数据行 (行2 = 当前周)
    row = 2
    data_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    data_font = XFont(size=10)
    green_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")

    # 列23-25: 以TWR为主的目标完成, 备注中标注动态本金
    ws_rev.cell(row=row, column=23, value=f'{daily_complete} (TWR{twr_daily_actual*100:.3f}% vs目标{daily_target*100:.4f}%)')
    ws_rev.cell(row=row, column=24, value=f'{weekly_complete} (TWR{twr_weekly_actual*100:.3f}% vs目标{weekly_target*100:.4f}%)')
    ws_rev.cell(row=row, column=25, value=f'{monthly_complete} (TWR{twr_monthly_actual*100:.3f}% vs目标{monthly_target*100:.4f}%)')
    # 列26: 双复利累计收益率 [主]TWR + [辅]动态本金
    ws_rev.cell(row=row, column=26, value=f'[主]TWR:{twr_total_return*100:.1f}%年化{twr_annualized_return*100:.1f}% [辅]动态:{dynamic_total_return*100:.1f}%年化{dynamic_annualized_return*100:.1f}%')
    ws_rev.cell(row=row, column=27, value=f'平均R={avg_r:.4f}, 累计R={total_r:.4f}, 最大R={max_r:.4f}, 最大亏损R={max_loss_r:.4f}')
    ws_rev.cell(row=row, column=28, value=round(win_loss_ratio, 2) if win_loss_ratio != float('inf') else "∞")
    ws_rev.cell(row=row, column=29, value=f'{current_stage}阶段, ¥{current_asset:,.0f}/¥{next_target:,}目标({next_stage})')
    ws_rev.cell(row=row, column=30, value=f'{growth_progress:.1f}%')
    # 列31: 双复利基数详情
    ws_rev.cell(row=row, column=31, value=f'主:TWR本金¥{twr_principal:,.0f} 辅:动态¥{dynamic_principal:,.0f}({position_ratio:.0%}仓位) 系统={system_status} 胜率{win_rate:.0f}% 距{next_stage}差¥{next_target-current_asset:,.0f}')

    # 做T/建清仓/持仓天数 (新增3列)
    t0_rate_str = f'{t0_rate:.0f}%' if t0_count > 0 else 'N/A'
    ws_rev.cell(row=row, column=32, value=f'{t0_count}次, 成功率{t0_rate_str} ({t0_wins}/{t0_count})' if t0_count > 0 else '0次')
    ws_rev.cell(row=row, column=33, value=f'{cycle_rate:.0f}% ({cycle_wins}/{cycle_count})' if cycle_count > 0 else 'N/A')
    ws_rev.cell(row=row, column=34, value=f'{avg_hold_days:.0f}天' if avg_hold_days > 0 else 'N/A')

    # 绿色标注完成
    for col, complete in [(23, daily_complete), (24, weekly_complete), (25, monthly_complete)]:
        if complete == "是":
            ws_rev.cell(row=row, column=col).fill = green_fill
        ws_rev.cell(row=row, column=col).alignment = data_align
        ws_rev.cell(row=row, column=col).font = data_font

    for col in range(26, 35):
        ws_rev.cell(row=row, column=col).alignment = data_align
        ws_rev.cell(row=row, column=col).font = data_font
        ws_rev.cell(row=row, column=col).border = thin_border

    # 修复交易记录R值 (值模式, 覆盖公式)
    ws_tr_fix = wb['交易记录']
    cumul_r = 0
    for t in trades:
        r = t["row"]
        if t["direction"] in ("卖出", "一卖") and r in r_by_row:
            ws_tr_fix.cell(row=r, column=24, value=r_by_row[r])
            cumul_r += r_by_row[r]
        else:
            ws_tr_fix.cell(row=r, column=24, value=0)
        ws_tr_fix.cell(row=r, column=25, value=round(cumul_r, 6))

    # 统计区域写入正确值
    last_trade_row = max(t["row"] for t in trades) if trades else 19
    ws_tr_fix.cell(row=209, column=2, value=round(avg_r, 6))
    ws_tr_fix.cell(row=210, column=2, value=round(total_r, 6))
    ws_tr_fix.cell(row=211, column=2, value=round(max_r, 6))
    ws_tr_fix.cell(row=212, column=2, value=round(max_loss_r, 6))
    ws_tr_fix.cell(row=215, column=2, value=0)
    ws_tr_fix.cell(row=216, column=2, value=round(win_loss_ratio, 6) if win_loss_ratio != float('inf') else 999)
    ws_tr_fix.cell(row=217, column=2, value=system_status)

    wb.save(WB)
    recalc_result = recalc()

    print(f"\n周复盘完成 (双复利模型):")
    print(f"  [主] TWR复利(总资产基数): 本金¥{twr_principal:,.0f}")
    print(f"       累计{twr_total_return*100:.1f}% 年化{twr_annualized_return*100:.1f}%")
    print(f"       日{daily_complete} 周{weekly_complete} 月{monthly_complete}")
    print(f"  [辅] 动态本金(持仓市值): ¥{dynamic_principal:,.0f} (仓位{position_ratio:.0%})")
    print(f"       累计{dynamic_total_return*100:.1f}% 年化{dynamic_annualized_return*100:.1f}%")
    print(f"  R值: 平均R={avg_r:.4f}, 累计R={total_r:.4f}, 盈亏比={win_loss_ratio:.2f}")
    print(f"  胜率: {win_rate:.0f}% ({len(wins)}/{len(r_values)})")
    print(f"  做T: {t0_count}次, 成功率{t0_rate:.0f}%")
    print(f"  建清仓: {cycle_count}次, 成功率{cycle_rate:.0f}%")
    print(f"  平均持仓: {avg_hold_days:.0f}天")
    print(f"  账户增长: {current_stage}, {growth_progress:.1f}% → ¥{next_target:,}({next_stage})")

    return {
        "daily": {"complete": daily_complete, "actual": twr_daily_actual, "target": daily_target,
                  "dynamic_actual": dynamic_daily_actual},
        "weekly": {"complete": weekly_complete, "actual": twr_weekly_actual, "target": weekly_target,
                   "dynamic_actual": dynamic_weekly_actual},
        "monthly": {"complete": monthly_complete, "actual": twr_monthly_actual, "target": monthly_target,
                    "dynamic_actual": dynamic_monthly_actual},
        "compound": {
            "twr_total_return": twr_total_return, "twr_annualized": twr_annualized_return,
            "dynamic_total_return": dynamic_total_return, "dynamic_annualized": dynamic_annualized_return,
            "deviation": compound_dev, "dynamic_principal": dynamic_principal,
            "twr_principal": twr_principal,
            "position_ratio": position_ratio, "invested_capital": invested_capital},
        "r_values": {"avg": avg_r, "total": total_r, "max": max_r, "max_loss": max_loss_r,
                      "win_loss_ratio": win_loss_ratio, "win_rate": win_rate, "system": system_status},
        "trading": {"t0_count": t0_count, "t0_wins": t0_wins, "t0_rate": t0_rate,
                     "cycle_count": cycle_count, "cycle_wins": cycle_wins, "cycle_rate": cycle_rate,
                     "avg_hold_days": avg_hold_days},
        "growth": {"stage": current_stage, "progress": growth_progress,
                    "next_target": next_target, "next_stage": next_stage,
                    "current_asset": current_asset},
    }


def format_weekly_review_summary(data, ts):
    """格式化周复盘摘要为Telegram消息"""
    lines = [f"📊 周复盘报告 {ts}", ""]

    # 复利目标
    lines.append("【复利目标完成情况 (以TWR为主)】")
    d = data["daily"]
    w = data["weekly"]
    m = data["monthly"]
    lines.append(f"  日复利: {d['complete']} (TWR{d['actual']*100:.3f}% vs目标{d['target']*100:.4f}%)")
    lines.append(f"  周复利: {w['complete']} (TWR{w['actual']*100:.3f}% vs目标{w['target']*100:.4f}%)")
    lines.append(f"  月复利: {m['complete']} (TWR{m['actual']*100:.3f}% vs目标{m['target']*100:.4f}%)")
    lines.append("")

    # 双复利累计
    c = data["compound"]
    lines.append("【双复利水平】")
    lines.append(f"  [主] TWR(总资产基数): 本金¥{c.get('twr_principal', 0):,.0f}")
    lines.append(f"       累计{c['twr_total_return']*100:.1f}% 年化{c['twr_annualized']*100:.1f}%")
    lines.append(f"  [辅] 动态本金(持仓市值): ¥{c.get('dynamic_principal', 0):,.0f} (仓位{c.get('position_ratio', 0):.0%})")
    lines.append(f"       累计{c['dynamic_total_return']*100:.1f}% 年化{c['dynamic_annualized']*100:.1f}%")
    lines.append("")

    # R值
    r = data["r_values"]
    lines.append("【正期望值R统计】")
    lines.append(f"  平均R: {r['avg']:.4f}")
    lines.append(f"  累计R: {r['total']:.4f}")
    lines.append(f"  最大R: {r['max']:.4f}")
    lines.append(f"  最大亏损R: {r['max_loss']:.4f}")
    lines.append(f"  盈亏比: {r['win_loss_ratio']:.2f}")
    lines.append(f"  胜率: {r['win_rate']:.0f}%")
    lines.append(f"  系统: {r['system']}")
    lines.append("")

    # 交易行为
    t = data.get("trading", {})
    lines.append("【交易行为统计】")
    lines.append(f"  做T: {t.get('t0_count', 0)}次, 成功率{t.get('t0_rate', 0):.0f}%")
    lines.append(f"  建清仓: {t.get('cycle_count', 0)}次, 成功率{t.get('cycle_rate', 0):.0f}%")
    lines.append(f"  平均持仓: {t.get('avg_hold_days', 0):.0f}天")
    lines.append("")

    # 账户增长
    g = data["growth"]
    lines.append("【账户规模增长目标】")
    lines.append(f"  当前阶段: {g['stage']}")
    lines.append(f"  当前资产: ¥{g['current_asset']:,.0f}")
    lines.append(f"  下阶段目标: ¥{g['next_target']:,} ({g['next_stage']})")
    lines.append(f"  完成进度: {g['progress']:.1f}%")
    lines.append(f"  距离目标: ¥{g['next_target'] - g['current_asset']:,.0f}")

    return '\n'.join(lines)


def main():
    if len(sys.argv) < 2:
        print("用法: daily_workflow.py [compliance|scan|intraday|account|holdings|weekly]")
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
        elif cmd == "weekly":
            review_data = run_weekly_review()
            msg = format_weekly_review_summary(review_data, ts)
            send_telegram(msg)
        else:
            print(f"未知命令: {cmd}")
    except Exception as e:
        import traceback
        err = traceback.format_exc()
        print(f"ERROR: {err}")
        try:
            send_telegram(f"❌ 工作流错误 ({cmd}) {ts}\n\n{str(e)}")
        except Exception:
            pass
        raise

if __name__ == '__main__':
    main()
