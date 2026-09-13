"""
Codex — 全市场候选扫描模块（完整版）
封装到工作流, 支持: 沪A全量(~1580只) + 深市全量(~1280只) + 高价股资金转入机制
输出: 确认信号 + 接近确认(前30) + 资金需求
"""
import sys, os, urllib.request, json, time, socket
# 2026-09-03: 全局 socket 超时。曾因行情源无超时挂起, 扫描卡死8小时(CPU仅27s)。
socket.setdefaulttimeout(20)
# 导入背驰分析器(兼容本地和GitHub Actions环境)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from beichi_analyzer import (analyze_beichi, validate_zhongshu_nesting,
                             correct_zhongshu_nesting)


def _dump_partial(confirmed, near, done, failed, path):
    """增量落盘: 每 N 只写一次快照, 防止长任务中途中断后前功尽弃"""
    try:
        payload = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "done": done,
            "failed": failed,
            "confirmed": confirmed,
            "near": near,
        }
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception as e:
        print(f"[dump] 落盘失败: {e}", flush=True)

def _gen_sz_codes():
    """生成深市全量代码: 000主板 + 002中小板 (不含3开头创业板)"""
    main = [f"{i:06d}" for i in range(1, 1000)]       # 000001-000999
    smb  = [f"002{i:03d}" for i in range(1, 1000)]    # 002001-002999
    return main + smb

def fetch_sha_list():
    stocks = []
    for page in range(1, 40):
        url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=50&sort=code&asc=0&node=hs_a&_s_r_a=page"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://finance.sina.com.cn/"
            })
            resp = urllib.request.urlopen(req, timeout=10)
            text = resp.read().decode('gbk', errors='replace')
            if not text.startswith('['):
                break
            data = json.loads(text)
            for item in data:
                code = item.get("code", "")
                name = item.get("name", "")
                price = float(item.get("trade", 0) or 0)
                if price <= 0:
                    price = float(item.get("settlement", 0) or 0)  # 盘前/停牌用昨收兜底
                if code.startswith("6") and not code.startswith("688") and len(code) == 6 and "ST" not in name and price > 0:
                    stocks.append({"code": code, "name": name, "price": price})
        except:
            break
    return stocks

def fetch_sza_prices(silent=False):
    """深市全量: 生成000/002/300代码 → 新浪批量获取现价 → 过滤ST和停牌"""
    codes = _gen_sz_codes()
    stocks = {}  # code -> {"code", "name", "price"}
    batch_size = 50
    total = len(codes)

    for start in range(0, total, batch_size):
        batch = codes[start:start + batch_size]
        codes_str = ",".join([f"sz{c}" for c in batch])
        url = f"http://hq.sinajs.cn/list={codes_str}"
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0", "Referer": "https://finance.sina.com.cn/"
            })
            resp = urllib.request.urlopen(req, timeout=10)
            text = resp.read().decode('gbk', errors='replace')
            for line in text.strip().split('\n'):
                if '=' not in line or '""' in line:
                    continue
                parts = line.split('=')
                code_full = parts[0].split('_')[-1] if '_' in parts[0] else parts[0]
                code = code_full[2:]
                vals = parts[1].strip('"').split(',')
                if len(vals) >= 4 and vals[0]:
                    name = vals[0]
                    price = float(vals[3] or 0)
                    if price <= 0 and len(vals) >= 3:
                        price = float(vals[2] or 0)  # 盘前用昨收兜底
                    if price > 0 and 'ST' not in name and 'st' not in name:
                        stocks[code] = {"code": code, "name": name, "price": price}
        except:
            pass
        if not silent and (start // batch_size + 1) % 20 == 0:
            print(f"  深市进度: {min(start + batch_size, total)}/{total}, 已获取{len(stocks)}只有效")

    if not silent:
        print(f"  深市代码总数: {total}, 有效(非ST非停牌): {len(stocks)}只")
    return list(stocks.values())

def scan_one(code, name, price):
    try:
        r = analyze_beichi(code, level="日线")
    except Exception as e:
        print(f"[scan_one] 异常 code={code} err={e}")
        return None
    if "error" in r:
        return None
    # 【Bug2 修复】替换静默 10 倍 fallback: 用 data_quality 字段显式判定
    dq = r.get("data_quality", {"ok": True, "deviation_pct": 0})
    close = r["C"][-1] if r.get("C") else price
    use_price = close or price
    if not dq["ok"]:
        print(f"[数据质量] code={code} dev={dq['deviation_pct']:.2f}% 使用市场价 {price}")
        use_price = price
    best_buy = None
    best_sell = None
    for sig in r.get("signals", []):
        op = sig.get("op")
        ratio = sig["ratio"]
        dlp = sig["dl_prob"]
        valid = sig["valid"]
        confirmed = ratio < 60 and dlp > 0.8 and valid
        # 【Bug2 修复】数据不一致时 confirmed 强制降级
        if not dq["ok"]:
            confirmed = False
        near = (ratio < 60 and dlp > 0.6 and valid) or (ratio < 85 and dlp > 0.8 and valid)
        score = 0
        if ratio < 60: score += 50
        elif ratio < 85: score += 20
        if dlp > 0.8: score += 30
        elif dlp > 0.6: score += 15
        if valid: score += 20
        if not dq["ok"]: score -= 20  # 数据不一致扣分
        if op == "一买":
            if best_buy is None or score > best_buy["score"]:
                best_buy = {"ratio": ratio, "dlp": dlp, "valid": valid,
                            "confirmed": confirmed, "near": near, "score": score}
        elif op == "一卖":
            if best_sell is None or score > best_sell["score"]:
                best_sell = {"ratio": ratio, "dlp": dlp, "valid": valid,
                             "confirmed": confirmed, "near": near, "score": score}
    if best_buy is None and best_sell is None:
        return None
    result = {
        "code": code, "name": name, "price": use_price,
        "data_quality": dq,
    }
    if best_buy is not None:
        result.update({"ratio": best_buy["ratio"], "dlp": best_buy["dlp"],
                       "valid": best_buy["valid"], "confirmed": best_buy["confirmed"],
                       "near": best_buy["near"], "score": best_buy["score"]})
    else:
        result.update({"ratio": None, "dlp": None, "valid": None,
                       "confirmed": None, "near": None, "score": None})
    result["slp"] = best_sell["dlp"] if best_sell is not None else None
    result["slp_score"] = best_sell["score"] if best_sell is not None else None
    result["slp_valid"] = best_sell["valid"] if best_sell is not None else None
    return result

def check_nesting(code):
    """B4: 对候选股进行区间套嵌套校验(日线/30min/5min)
    只对确认信号调用, 避免全市场3倍网络开销。
    返回 {"ok": bool, "violations": [...], "corrected": dict或None}
    """
    level_zhongshu = {}
    for level in ["日线", "30min", "5min"]:
        try:
            r = analyze_beichi(code, level=level)
            if "error" in r or not r.get("zss"):
                continue
            level_zhongshu[level] = r["zss"][-1]
        except:
            pass
    if len(level_zhongshu) < 2:
        return {"ok": True, "violations": [], "corrected": None}
    res = validate_zhongshu_nesting(level_zhongshu, ["日线", "30min", "5min"])
    corrected = None
    if not res["ok"]:
        corrected = correct_zhongshu_nesting(
            level_zhongshu, ["日线", "30min", "5min"], res["violations"])
    return {"ok": res["ok"], "violations": res["violations"], "corrected": corrected}

def calc_funding(price, total_asset, cash):
    cost = round(price * 100, 2)
    gap = max(0, cost - cash)
    return {
        "cost": cost, "cash": cash, "gap": gap,
        "need_transfer": gap > 0, "transfer": gap,
    }

def full_scan(total_asset=20326.12, cash=7847.12, silent=False):
    """全市场扫描主入口: 沪A全量 + 深市全量"""
    if not silent:
        print("[1/3] 获取沪A列表...")
    sha = fetch_sha_list()
    if not silent:
        print(f"  沪A: {len(sha)}只")

    if not silent:
        print("[2/3] 获取深市现价(全量)...")
    sza = fetch_sza_prices(silent=silent)
    if not silent:
        print(f"  深市: {len(sza)}只")

    all_stocks = sha + sza
    
    # 🏗️ VM 分流: MARKET=sh(沪)/deep(深)/all(全)
    _market = os.environ.get('MARKET', 'all')
    if _market == 'sh':
        all_stocks = [ss for ss in all_stocks if ss['code'].startswith('6') and not ss['code'].startswith('688')]
    elif _market == 'deep':
        all_stocks = [ss for ss in all_stocks if ss['code'][0] in ('0','3')]
    print(f'🎯 MARKET={_market}: {len(all_stocks)} 只')
    confirmed = []
    near = []
    all_signals = []   # 所有有一买信号的 (不管 confirmed/near, 给双引擎全量门禁用)
    scanned = 0
    failed = 0

    if not silent:
        print(f"[3/3] 背驰分析 ({len(all_stocks)}只)...")
    t0 = time.time()

    # 2026-09-03: 串行 -> 并发(ThreadPoolExecutor)。实测串行 0.88s/只,
    # 并发8 约 0.3s/只, 全市场从 ~105 分钟降至 ~15 分钟。
    from concurrent.futures import ThreadPoolExecutor, as_completed
    WORKERS = int(os.environ.get("SCAN_WORKERS", "8"))
    DUMP_EVERY = 200
    out_dir = os.path.join(_SCRIPT_DIR, "scan_output")
    os.makedirs(out_dir, exist_ok=True)
    partial_path = os.path.join(out_dir, "partial_scan.json")

    scanned = 0
    failed = 0
    done = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(scan_one, s["code"], s["name"], s["price"]): s
                for s in all_stocks}
        for fu in as_completed(futs):
            done += 1
            try:
                result = fu.result()
            except Exception:
                result = None
            if result is None:
                failed += 1
            else:
                scanned += 1
                all_signals.append(result)  # 所有有信号的都进全量候选池
                if result["confirmed"]:
                    confirmed.append(result)
                elif result["near"]:
                    near.append(result)
            if not silent and done % 300 == 0:
                el = time.time() - t0
                print(f"  进度: {done}/{len(all_stocks)} "
                      f"(确认{len(confirmed)} 接近{len(near)} 失败{failed}) "
                      f"{el:.0f}s", flush=True)
            if done % DUMP_EVERY == 0:
                _dump_partial(confirmed, near, done, failed, partial_path)
    _dump_partial(confirmed, near, done, failed, partial_path)

    elapsed = time.time() - t0

    # 去重接近确认, 按score降序+ratio升序(同分时背驰更强的优先)
    seen = set()
    unique_near = []
    for r in sorted(near, key=lambda x: (-x["score"], x["ratio"])):
        if r["code"] not in seen:
            seen.add(r["code"])
            unique_near.append(r)

    if not silent:
        print(f"完成: {len(all_stocks)}只, 耗时{elapsed:.0f}秒")

    # ============================================================
    # 分层候选池 (2026-07-26)
    # 问题: DL_P跨0.8阈值的标的每天进出候选池, 无法用于调仓
    # 方案: 按DL_P+ratio分3层, 不同层不同稳定性
    #   核心池: DL_P>0.90 + ratio<20% → 1-2周稳定, 调仓首选
    #   观察池: DL_P 0.85-0.90       → 3-5天稳定, 核心池不足时补充
    #   边缘池: DL_P 0.80-0.85       → 每天变动, 仅观察不买入
    # ============================================================
    def assign_tier(stock):
        dlp = stock["dlp"]
        ratio = stock["ratio"]
        if dlp > 0.90 and ratio < 20:
            return "核心"
        elif dlp >= 0.85:
            return "观察"
        else:
            return "边缘"

    for s in confirmed:
        s["tier"] = assign_tier(s)

    core = [s for s in confirmed if s["tier"] == "核心"]
    watch = [s for s in confirmed if s["tier"] == "观察"]
    edge = [s for s in confirmed if s["tier"] == "边缘"]

    if not silent:
        print(f"分层: 核心{len(core)}只 + 观察{len(watch)}只 + 边缘{len(edge)}只")

    # B4: 对确认信号进行区间套嵌套校验(只查confirmed, 避免全市场3倍开销)
    nesting_stats = {"checked": 0, "violations": 0, "corrected": 0}
    for s in confirmed:
        nr = check_nesting(s["code"])
        s["nesting"] = nr
        nesting_stats["checked"] += 1
        if not nr["ok"]:
            nesting_stats["violations"] += 1
            nesting_stats["corrected"] += 1
    if not silent and nesting_stats["checked"] > 0:
        print(f"区间套校验: {nesting_stats['checked']}只, 违反{nesting_stats['violations']}只, 已修正{nesting_stats['corrected']}只")

    # 所有有一买信号的 (不管 confirmed/near 标记, 双引擎全量门禁用)
    # 设计意图: 双引擎优先级最高 → 先跑双引擎, DL_P 只做 PASS 里的排序
    return {
        "total_scanned": len(all_stocks),
        "success": scanned,
        "failed": failed,
        "elapsed": round(elapsed, 1),
        "confirmed": confirmed,
        "near": unique_near[:100],
        "total_near": len(unique_near),
        "all_signals": all_signals,  # 所有有一买信号的标的
        "total_asset": total_asset,
        "cash": cash,
        "core": core,
        "watch": watch,
        "edge": edge,
        "nesting_stats": nesting_stats,
    }


if __name__ == "__main__":
    result = full_scan(silent=False)

    print(f"\n{'='*70}")
    print("扫描报告")
    print(f"{'='*70}")
    print(f"覆盖: 沪A+深市全量 {result['total_scanned']}只 | 耗时{result['elapsed']}秒")

    if result["confirmed"]:
        print(f"\n★ 确认买入信号 ({len(result['confirmed'])}只):")
        for r in sorted(result["confirmed"], key=lambda x: -x["score"]):
            f = calc_funding(r["price"], result["total_asset"], result["cash"])
            print(f"  {r['name']} {r['code']} | ¥{r['price']:.2f} | ratio={r['ratio']:.0f}% DL_P={r['dlp']:.2f}")
            if f["need_transfer"]:
                print(f"    → 需转入{f['transfer']:,.0f}元 (1手={f['cost']:,.0f}元)")
            else:
                print(f"    → 可买1手={f['cost']:,.0f}元")
    else:
        print("\n★ 确认信号: 0只")

    if result["near"]:
        print(f"\n◆ 接近确认 (前{len(result['near'])}只 / 共{result['total_near']}只):")
        for r in result["near"][:15]:
            f = calc_funding(r["price"], result["total_asset"], result["cash"])
            missing = []
            if r["ratio"] >= 60: missing.append(f"ratio={r['ratio']:.0f}%")
            if r["dlp"] <= 0.8: missing.append(f"DL_P={r['dlp']:.2f}")
            xfer = f" (需转入{f['transfer']:,.0f}元)" if f["need_transfer"] else ""
            print(f"  {r['name']} {r['code']} | ¥{r['price']:.2f} | ratio={r['ratio']:.0f}% DL_P={r['dlp']:.2f} | 缺:{'+'.join(missing)}{xfer}")

    ns = result.get("nesting_stats", {})
    if ns.get("checked", 0) > 0:
        print(f"\n区间套嵌套: 校验{ns['checked']}只 | 违反{ns['violations']}只 | 已修正{ns['corrected']}只")

    print(f"\n{'='*70}")
    print(f"高价股已纳入扫描, 资金不足时提示转入金额")
