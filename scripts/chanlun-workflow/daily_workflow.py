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
import sys, subprocess, json, os
from datetime import datetime, date
from decimal import Decimal
from openpyxl import load_workbook
from openpyxl.utils import column_index_from_string

WB = '/workspace/动态仓位资金管理法则_执行版.xlsx'
RECALC = '/data/user/builtin/work/eurydice/skills/xlsx/scripts/recalc.py'
BEICHI_DIR = '/workspace/chanlun-kline'

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
    for h in holdings:
        if h['waived'] == '是':
            continue
        if h['close'] and h['stop'] and h['close'] <= h['stop']:
            issues.append(f"⚠️ {h['name']}已破止损: 现价{h['close']:.2f}<=止损{h['stop']:.2f}")
        if h['pos'] and h['pos'] > 0.35:
            issues.append(f"⚠️ {h['name']}仓位超限: {h['pos']:.1%}>35%")
    def safe(v):
        if isinstance(v, (datetime, date)): return str(v)
        if isinstance(v, Decimal): return float(v)
        return v
    print(json.dumps({"account": {k:safe(v) for k,v in account.items()}, "holdings": [{k:safe(v) for k,v in h.items()} for h in holdings], "issues": issues, "compliant": len(issues)==0}, ensure_ascii=False, indent=2))

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
    return result

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

    # 3. 持仓止损检查
    print(f"\n[3/3] 持仓止损检查...")
    holdings = get_today_holdings()
    alerts = []
    for h in holdings:
        if h['waived'] == '是':
            continue
        if h['close'] and h['stop'] and h['close'] <= h['stop']:
            alerts.append(f"⚠️ {h['name']}({h['code']}) 破止损: 现价{h['close']:.2f}<=止损{h['stop']:.2f}")
    if alerts:
        for a in alerts:
            print(f"  {a}")
    else:
        print(f"  持仓{len(holdings)}只, 止损全部合规")

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

    print(f"{'='*50}")
    return {
        "confirmed_30m": confirmed_30m,
        "near_30m": near_30m,
        "alerts": alerts,
        "scanned": len(candidates),
    }

def main():
    if len(sys.argv) < 2:
        print("用法: daily_workflow.py [compliance|scan|account|holdings]")
        return
    cmd = sys.argv[1]
    if cmd == "compliance":
        check_compliance()
    elif cmd == "scan":
        run_full_scan()
    elif cmd == "intraday":
        run_intraday_scan()
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

if __name__ == '__main__':
    main()
