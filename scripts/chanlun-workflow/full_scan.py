"""
Codex — 全市场候选扫描模块（完整版）
封装到工作流, 支持: 沪A全量(~1580只) + 深市全量(~1280只) + 高价股资金转入机制
输出: 确认信号 + 接近确认(前30) + 资金需求
"""
import sys, os, urllib.request, json, time
# 导入背驰分析器(兼容本地和GitHub Actions环境)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from beichi_analyzer import analyze_beichi

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
    except:
        return None
    if "error" in r:
        return None
    close = r["C"][-1] if r.get("C") else price
    # 修复: 日K数据与市场现价偏差>10倍时使用市场现价(深市数据源复权问题)
    if price > 0 and close > 0 and (close / price > 10 or price / close > 10):
        close = price
    best = None
    for sig in r.get("signals", []):
        if sig["op"] != "一买":
            continue
        ratio = sig["ratio"]
        dlp = sig["dl_prob"]
        valid = sig["valid"]
        confirmed = ratio < 60 and dlp > 0.8 and valid
        near = (ratio < 60 and dlp > 0.6 and valid) or (ratio < 85 and dlp > 0.8 and valid)
        score = 0
        if ratio < 60: score += 50
        elif ratio < 85: score += 20
        if dlp > 0.8: score += 30
        elif dlp > 0.6: score += 15
        if valid: score += 20
        if best is None or score > best["score"]:
            best = {
                "code": code, "name": name, "price": close or price,
                "ratio": ratio, "dlp": dlp, "valid": valid,
                "confirmed": confirmed, "near": near, "score": score,
            }
    return best

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
    confirmed = []
    near = []
    scanned = 0
    failed = 0

    if not silent:
        print(f"[3/3] 背驰分析 ({len(all_stocks)}只)...")
    t0 = time.time()

    for i, s in enumerate(all_stocks):
        result = scan_one(s["code"], s["name"], s["price"])
        if result is None:
            failed += 1
        else:
            scanned += 1
            if result["confirmed"]:
                confirmed.append(result)
            elif result["near"]:
                near.append(result)
        if not silent and (i+1) % 300 == 0:
            print(f"  进度: {i+1}/{len(all_stocks)} (确认{len(confirmed)} 接近{len(near)})")

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

    return {
        "total_scanned": len(all_stocks),
        "success": scanned,
        "failed": failed,
        "elapsed": round(elapsed, 1),
        "confirmed": confirmed,
        "near": unique_near[:100],
        "total_near": len(unique_near),
        "total_asset": total_asset,
        "cash": cash,
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

    print(f"\n{'='*70}")
    print(f"高价股已纳入扫描, 资金不足时提示转入金额")
