"""
Codex — 全市场候选扫描模块（完整版）
封装到工作流, 支持: 沪A全量(~1580只) + 深市全量(~1280只) + 高价股资金转入机制
输出: 确认信号 + 接近确认(前30) + 资金需求

【超时保护 2026-08-11】防止单只股票请求卡死整个扫描
  1. socket.setdefaulttimeout(30) — 全局socket级超时兜底
  2. 每只股票最多30秒, 超时自动跳过
  3. 断点续跑: 每完成100只写入进度文件, 中断后自动恢复
  4. flush=True — GitHub Actions实时输出
"""
import sys, os, urllib.request, json, time, socket
# 全局socket超时 — 任何网络请求超过30秒自动断开
socket.setdefaulttimeout(30)

# 导入背驰分析器(兼容本地和GitHub Actions环境)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)
from beichi_analyzer import analyze_beichi

# 并发预取 (2026-08-09): 可选提速, 需 pip install concurrent.futures (内置)
try:
    from concurrent_prefetch import prefetch_stocks
    _HAS_PREFETCH = True
except ImportError:
    _HAS_PREFETCH = False

# 断点续跑 — 进度文件路径
_CHECKPOINT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".scan_checkpoint")
_CHECKPOINT_INTERVAL = 100  # 每100只保存一次进度

def _save_checkpoint(index, total, codes_snapshot=None):
    """保存扫描进度到文件, 支持中断后恢复"""
    try:
        cp = {"index": index, "total": total, "ts": time.time()}
        if codes_snapshot:
            cp["codes_head"] = codes_snapshot[:50]
            cp["codes_tail"] = codes_snapshot[-50:] if len(codes_snapshot) > 50 else []
        with open(_CHECKPOINT_FILE, 'w') as f:
            json.dump(cp, f)
    except Exception:
        pass

def _load_checkpoint():
    """读取断点, 返回(index, total)或(0, 0)"""
    if not os.path.exists(_CHECKPOINT_FILE):
        return 0, 0
    try:
        with open(_CHECKPOINT_FILE) as f:
            cp = json.load(f)
        return cp.get("index", 0), cp.get("total", 0)
    except Exception:
        return 0, 0

def _clear_checkpoint():
    """扫描完成后清除进度文件"""
    try:
        if os.path.exists(_CHECKPOINT_FILE):
            os.remove(_CHECKPOINT_FILE)
    except Exception:
        pass

def _gen_sz_codes():
    """生成深市代码: 000主板 + 002中小板 (不含3开头创业板)

    婴幼儿账户规模未达到买入300开头股票资格, 保持原有扫描范围
    (2026-08-06): 回退创业板扫描范围
    """
    main = [f"{i:06d}" for i in range(1, 1000)]       # 000001-000999
    smb  = [f"002{i:03d}" for i in range(1, 1000)]    # 002001-002999
    return main + smb

def fetch_sha_list():
    stocks = []
    for page in range(1, 40):
        url = f"https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData?page={page}&num=50&sort=code&asc=1&node=sh_a&_s_r_a=page"
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
            print(f"  深市进度: {min(start + batch_size, total)}/{total}, 已获取{len(stocks)}只有效", flush=True)

    if not silent:
        print(f"  深市代码总数: {total}, 有效(非ST非停牌): {len(stocks)}只", flush=True)
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
                "sig_type": sig.get("type", ""),          # 趋势背驰/盘整背驰/无背驰
                "sig_label": sig.get("label", "ABC买卖区间"),  # 123买卖区间/ABC买卖区间
                "label": sig.get("label", "ABC买卖区间"), # 同步: 买卖区间标注
            }
    # 【Fix B: 多级别tier覆盖】2026-08-08
    # 使用detect_multilevel_buy_signals获取V5分层(30min一买准入)
    # 覆盖daily-only的简单tier分配
    try:
        from beichi_analyzer import detect_multilevel_buy_signals
        ml = detect_multilevel_buy_signals(code, price=close)
        if ml.get("tier") and ml["tier"] != "无信号":
            if best is None:
                best = {
                    "code": code, "name": name, "price": close or price,
                    "ratio": 999, "dlp": 0, "valid": False,
                    "confirmed": False, "near": True, "score": 10,
                    "sig_type": "盘整背驰",    # 默认: 单中枢盘整
                    "sig_label": "ABC买卖区间",
                    "label": "ABC买卖区间",
                }
            best["tier"] = ml["tier"]
            best["label"] = ml.get("daily_label", "ABC买卖区间")  # 买卖区间标注: 123/ABC
            best["sig_label"] = best["label"]  # 同步买卖区间标注
            # 从label推导背驰类型: 123买卖区间→趋势背驰, ABC买卖区间→盘整背驰
            best["sig_type"] = "趋势背驰" if "123" in str(best["label"]) else "盘整背驰"
            best["30min_dl_p"] = ml.get("30min_dl_p", 0)  # 近零,仅供参考
            best["30min_ep_p"] = ml.get("30min_ep_p", 0)  # 有效信号
            best["30min_first_buy_valid"] = ml.get("min30_first_buy_valid", False)
            best["ermai"] = ml.get("ermai") is not None
            best["sanmai"] = ml.get("sanmai") is not None
            # 【5.5复核修复: 提取single_stock_cap用于仓位联动】
            best["single_stock_cap"] = ml.get("single_stock_cap", 0.35)
            # 【30min几何双中枢趋势背驰】2026-08-10
            dc = ml.get("min30_double_center", {})
            best["min30_dc_has"] = dc.get("has_double_center", False)
            best["min30_dc_divergence"] = dc.get("is_divergence", False)
            best["min30_dc_confidence"] = dc.get("confidence", 0.0)
            best["min30_dc_ratio"] = dc.get("divergence_ratio", 999.0)
            best["min30_dc_reason"] = dc.get("reason", "未检测")
            # 双中枢趋势背驰 → 强制覆盖买卖区间和背驰类型
            if dc.get("is_divergence", False):
                best["sig_type"] = "趋势背驰"
                best["sig_label"] = "123买卖区间"
                best["label"] = "123买卖区间"
            # 【30min几何单中枢盘整背驰】2026-08-10
            sc = ml.get("min30_single_center", {})
            best["min30_sc_has"] = sc.get("has_single_center", False)
            best["min30_sc_divergence"] = sc.get("is_divergence", False)
            best["min30_sc_confidence"] = sc.get("confidence", 0.0)
            best["min30_sc_ratio"] = sc.get("divergence_ratio", 999.0)
            best["min30_sc_reason"] = sc.get("reason", "未检测")
            # 单中枢盘整背驰 → 仅当无双中枢时覆盖(双中枢优先级更高)
            if sc.get("is_divergence", False) and not dc.get("is_divergence", False):
                best["sig_type"] = "盘整背驰"
                best["sig_label"] = "ABC买卖区间"
                best["label"] = "ABC买卖区间"
    except:
        pass
    return best

def calc_funding(price, total_asset, cash):
    cost = round(price * 100, 2)
    gap = max(0, cost - cash)
    return {
        "cost": cost, "cash": cash, "gap": gap,
        "need_transfer": gap > 0, "transfer": gap,
    }

def full_scan(total_asset=20326.12, cash=7847.12, silent=False, prefetch=False):
    """全市场扫描主入口: 沪A全量 + 深市全量

    参数:
        total_asset: float 总资产
        cash: float 可用现金
        silent: bool 静默模式
        prefetch: bool 是否启用并发预取 (2026-08-09)
    """
    if not silent:
        print("[1/3] 获取沪A列表...", flush=True)
    sha = fetch_sha_list()
    if not silent:
        print(f"  沪A: {len(sha)}只", flush=True)

    if not silent:
        print("[2/3] 获取深市现价(全量)...", flush=True)
    sza = fetch_sza_prices(silent=silent)
    if not silent:
        print(f"  深市: {len(sza)}只", flush=True)

    all_stocks = sha + sza

    # ============================================================
    #  【并发预取 V2 (2026-08-09) 递归优化】
    #  在分析前批量预取K线数据到缓存, 大幅减少串行等待
    #  只预取5min, 30min/日线由递归系统从5min合成
    #  全市场~2886只 x 5min = 2886次请求, 并发20线程 ~1.2秒
    #  之后analyze_beichi直接命中缓存, 递归合成零成本
    # ============================================================
    if prefetch and _HAS_PREFETCH:
        codes = [s["code"] for s in all_stocks]
        if not silent:
            print(f"[预取] 并发预取{len(codes)}只股票5min数据(递归合成30min/日线)...", flush=True)
        try:
            pf_result = prefetch_stocks(codes, levels=["5min"], max_workers=20, silent=silent)
            if not silent:
                pf_hit = pf_result.get("hit", 0)
                pf_miss = pf_result.get("miss", 0)
                pf_elapsed = pf_result.get("elapsed", 0)
                print(f"  预取完成: 命中{pf_hit} 获取{pf_miss} 耗时{pf_elapsed}秒", flush=True)
        except Exception as e:
            if not silent:
                print(f"  ⚠ 预取失败(不影响扫描): {e}", flush=True)

    confirmed = []
    near = []
    scanned = 0
    failed = 0

    if not silent:
        print(f"[3/3] 背驰分析 ({len(all_stocks)}只)...", flush=True)
    t0 = time.time()

    # 【超时保护+断点续跑 2026-08-11】
    # 检查是否有断点需要恢复
    cp_idx, cp_total = _load_checkpoint()
    if cp_idx > 0 and cp_total == len(all_stocks):
        # 断点有效: 跳过已扫描部分
        skip_count = cp_idx
        if not silent:
            print(f"  ↻ 检测到断点: 跳过前{skip_count}只, 从第{skip_count+1}只继续", flush=True)
        # 已扫描股票不计入, 但不影响结果(缓存已写入)
        scanned = 0
        failed = 0
    else:
        skip_count = 0
        if cp_idx > 0 and cp_total != len(all_stocks):
            if not silent:
                print(f"  ⚠ 断点总股票数({cp_total})与当前({len(all_stocks)})不匹配, 从头扫描", flush=True)

    for i, s in enumerate(all_stocks):
        # 跳过已扫描部分
        if i < skip_count:
            continue

        # 【超时保护 V2 2026-08-12】每只股票最多20秒, 超时强制跳过
        # 【Bug修复】原版用 ThreadPoolExecutor(max_workers=1) + _fut.result(timeout=30),
        #   超时后 with 退出时 __exit__ 调用 shutdown(wait=True) 会阻塞等待那个卡死的线程完成,
        #   导致超时保护形同虚设 — 30秒超时变成无限等待, 卡住整个2886只循环.
        # 修复: 用 multiprocessing.Process + Queue 实现真杀, 超时后 terminate() 强制终止子进程.
        #   主循环不受卡死股票影响, 真正实现"超时即跳过".
        import multiprocessing
        _scan_queue = multiprocessing.Queue()
        _scan_proc = None
        try:
            _scan_proc = multiprocessing.Process(
                target=lambda q, c, n, p: q.put(scan_one(c, n, p)),
                args=(_scan_queue, s["code"], s["name"], s["price"])
            )
            _scan_proc.daemon = True
            _scan_proc.start()
            result = _scan_queue.get(timeout=20)
            _scan_proc.join(timeout=2)
        except Exception:
            result = None
            if not silent:
                print(f"  ⏰ 超时(20s): {s['name']}({s['code']}), 跳过", flush=True)
        finally:
            if _scan_proc is not None and _scan_proc.is_alive():
                _scan_proc.terminate()
                _scan_proc.join(timeout=1)

        if result is None:
            failed += 1
        else:
            scanned += 1
            if result["confirmed"]:
                confirmed.append(result)
            elif result["near"]:
                near.append(result)

        # 【2026-08-11】每只股票都打印进度, 定位卡死点
        if not silent and (i+1) % 100 == 0:
            elapsed_now = time.time() - t0
            rate = (i+1-skip_count) / max(elapsed_now, 1)
            print(f"  进度: {i+1}/{len(all_stocks)} (确认{len(confirmed)} 接近{len(near)}) 失败{failed} | 速率{rate:.1f}只/秒 | 耗时{elapsed_now:.0f}s", flush=True)
            # 保存断点
            _save_checkpoint(i+1, len(all_stocks))

        # 速率限制: 每50ms一个请求, 避免触发新浪API防火墙
        # 全市场~2860只 = 额外~143秒, 但第一次扫描后缓存命中率>90%
        # 优化 (2026-08-11): 缓存命中时跳过sleep, 节省~140秒
        if (i+1) % 5 == 0:
            _cached = False
            try:
                from beichi_analyzer import _kline_cache_load
                _cached = _kline_cache_load(s["code"], "5", 3600*4) is not None
            except:
                pass
            if not _cached:
                time.sleep(0.05)

    elapsed = time.time() - t0
    # 扫描完成, 清除断点
    _clear_checkpoint()

    # 去重接近确认, 按score降序+ratio升序(同分时背驰更强的优先)
    seen = set()
    unique_near = []
    for r in sorted(near, key=lambda x: (-x["score"], x["ratio"])):
        if r["code"] not in seen:
            seen.add(r["code"])
            unique_near.append(r)

    if not silent:
        print(f"完成: {len(all_stocks)}只, 耗时{elapsed:.0f}秒", flush=True)

    # ============================================================
    # 分层候选池 (V5, 2026-08-08)
    # 使用detect_multilevel_buy_signals的tier分层(30min一买准入)
    # 回退: 无多级别tier时使用daily DL_P
    # ============================================================
    def assign_tier(stock):
        # 优先使用多级别tier
        if stock.get("tier"):
            tier_map = {"核心池": "核心", "观察池": "观察", "边缘池": "边缘"}
            return tier_map.get(stock["tier"], "边缘")
        # 回退到daily DL_P
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
        print(f"分层: 核心{len(core)}只 + 观察{len(watch)}只 + 边缘{len(edge)}只", flush=True)

    # ============================================================
    # BUG诊断 (2026-08-06): 全市场无DL_P>0.8时自动核查
    # 规则: 全市场~2860只股票如果一只DL_P>0.8都没有, 极大概率是bug
    # 诊断流程:
    #   1. 检查数据源是否正常(沪A/深市列表是否为空)
    #   2. 检查beichi_analyzer是否全部报错(failed == total_scanned)
    #   3. 抽样验证: 取5只股票重新analyze, 确认分析器工作正常
    #   4. 输出诊断报告, 不阻断扫描流程
    # ============================================================
    scan_diagnostics = {}
    if confirmed == 0 and len(all_stocks) > 100:
        diagnostics = []
        # 诊断1: 数据源检查
        if not sha:
            diagnostics.append("🔴 沪A数据源为空(fetch_sha_list返回0只), 新浪API可能挂了")
        if not sza:
            diagnostics.append("🔴 深市数据源为空(fetch_sza_prices返回0只), 新浪API可能挂了")
        # 诊断2: 全部失败检查
        if failed == len(all_stocks):
            diagnostics.append(f"🔴 全部{len(all_stocks)}只分析失败(failed={failed}), beichi_analyzer可能全局报错")
        elif scanned > 0:
            diagnostics.append(f"🟡 分析成功{scanned}只但确认0只, 市场极弱或阈值过严")
        # 诊断3: 抽样验证
        sample_checks = []
        test_codes = [("600519", "贵州茅台"), ("000001", "平安银行"), ("600036", "招商银行")]
        for test_code, test_name in test_codes:
            try:
                r = analyze_beichi(test_code, level="日线")
                if "error" in r:
                    sample_checks.append(f"  ❌ {test_name}({test_code}): analyze_beichi返回error={r['error']}")
                else:
                    sigs = r.get("signals", [])
                    dlp_max = max([s.get("dl_prob", 0) for s in sigs], default=0)
                    close = r["C"][-1] if r.get("C") else "N/A"
                    sample_checks.append(f"  ✓ {test_name}({test_code}): 收盘价{close}, 最高DL_P={dlp_max:.2f}, 信号{len(sigs)}条")
            except Exception as e:
                sample_checks.append(f"  ❌ {test_name}({test_code}): 异常={e}")
        diagnostics.append("抽样验证(3只权重股):\n" + "\n".join(sample_checks))
        # 输出诊断报告
        diag_report = "\n".join(diagnostics)
        if not silent:
            print(f"\n{'='*50}", flush=True)
            print(f"⚠️ BUG诊断: 全市场无DL_P>0.8信号 ({len(all_stocks)}只扫描, 0确认)", flush=True)
            print(f"{'='*50}", flush=True)
            print(diag_report, flush=True)
            print(f"{'='*50}", flush=True)
        scan_diagnostics = {
            "triggered": True,
            "report": diag_report,
            "checks": diagnostics,
        }
    else:
        scan_diagnostics = {"triggered": False, "report": "", "checks": []}

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
        "core": core,
        "watch": watch,
        "edge": edge,
        "scan_diagnostics": scan_diagnostics,
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefetch", action="store_true", help="启用并发预取加速")
    args = parser.parse_args()

    result = full_scan(silent=False, prefetch=args.prefetch)

    print(f"\n{'='*70}", flush=True)
    print("扫描报告", flush=True)
    print(f"{'='*70}", flush=True)
    print(f"覆盖: 沪A+深市全量 {result['total_scanned']}只 | 耗时{result['elapsed']}秒", flush=True)

    if result["confirmed"]:
        print(f"\n★ 确认买入信号 ({len(result['confirmed'])}只):", flush=True)
        for r in sorted(result["confirmed"], key=lambda x: -x["score"]):
            f = calc_funding(r["price"], result["total_asset"], result["cash"])
            label = r.get("label", "ABC买卖区间")
            sig_type = r.get("sig_type", "盘整背驰")
            print(f"  {r['name']} {r['code']} | ¥{r['price']:.2f} | {label} | {sig_type} | DL_P={r['dlp']:.2f} | tier={r.get('tier','?')}", flush=True)
            if f["need_transfer"]:
                print(f"    → 需转入{f['transfer']:,.0f}元 (1手={f['cost']:,.0f}元)", flush=True)
            else:
                print(f"    → 可买1手={f['cost']:,.0f}元", flush=True)
    else:
        print("\n★ 确认信号: 0只", flush=True)

    if result["near"]:
        print(f"\n◆ 接近确认 (前{len(result['near'])}只 / 共{result['total_near']}只):", flush=True)
        for r in result["near"][:15]:
            f = calc_funding(r["price"], result["total_asset"], result["cash"])
            label = r.get("label", "ABC买卖区间")
            sig_type = r.get("sig_type", "盘整背驰")
            missing = []
            if r["ratio"] >= 60: missing.append(f"ratio={r['ratio']:.0f}%")
            if r["dlp"] <= 0.8: missing.append(f"DL_P={r['dlp']:.2f}")
            xfer = f" (需转入{f['transfer']:,.0f}元)" if f["need_transfer"] else ""
            print(f"  {r['name']} {r['code']} | ¥{r['price']:.2f} | {label} | {sig_type} | DL_P={r['dlp']:.2f} | 缺:{'+'.join(missing)}{xfer}", flush=True)

    print(f"\n{'='*70}", flush=True)
    print(f"高价股已纳入扫描, 资金不足时提示转入金额", flush=True)
