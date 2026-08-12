"""
缠论递归系统 — 笔、段、中枢的完整递归实现 (v1.0)

包含:
  1. K线包含处理 (merge_klines)
  2. 顶底分型识别 (find_fractals)
  3. 笔的构建 (build_bi)
  4. 线段确认 (build_duan) — 特征序列法
  5. 中枢检测 (find_zhongshu_duan) — 三段线段重叠
  6. 级别递归 (recurse_level) — 5min→30min→日线

输入格式: [{"open": f, "high": f, "low": f, "close": f, "volume": f}, ...]
输出格式: 与现有 find_zhongshu 兼容 {"s": i, "e": j, "zg": f, "zd": f, "w": w}

作者: 5.5 (2026-08-09)
"""


def merge_klines(klines):
    """
    K线包含处理 (缠论原文标准算法)

    输入: 原始K线列表
    返回: (合并后的K线列表, idx_map)
          idx_map[j] = 合并后位置j对应的原始K线起始索引

    算法:
      1. 第一根K线作为基准
      2. 从第二根开始检查是否包含 (一根完全包裹另一根)
      3. 非包含时确定方向:
         - 向上: 高价抬高且低价也抬高
         - 向下: 高价降低且低价也降低
      4. 包含时按方向合并:
         - 向上趋势: 取高高 (max高, max低)
         - 向下趋势: 取低低 (min高, min低)
    """
    if not klines:
        return [], []

    result = [dict(klines[0])]
    idx_map = [0]  # 合并后位置0 → 原始索引0
    direction = 0  # 0=未知, 1=向上, -1=向下

    for i in range(1, len(klines)):
        k = klines[i]
        prev = result[-1]

        h1, l1 = prev["high"], prev["low"]
        h2, l2 = k["high"], k["low"]

        # 判断是否包含: 一根的高>=另一根的高 且 低<=另一根的低
        is_contained = (h2 >= h1 and l2 <= l1) or (h1 >= h2 and l1 <= l2)

        if not is_contained:
            # 非包含: 确定方向 (仅在方向未确定时)
            if direction == 0:
                if h2 > h1 and l2 > l1:
                    direction = 1
                elif h2 < h1 and l2 < l1:
                    direction = -1
                # else: 方向仍为0, 继续等待
            result.append(dict(k))
            idx_map.append(i)  # 新位置 → 原始索引i
        else:
            # 包含: 按方向合并
            if direction >= 0:  # 向上或未知, 取高高
                prev["high"] = max(h1, h2)
                prev["low"] = max(l1, l2)
            else:  # 向下, 取低低
                prev["high"] = min(h1, h2)
                prev["low"] = min(l1, l2)
            # 更新收盘价和开盘价 (用最新的)
            prev["close"] = k["close"]
            if direction == 0:
                prev["open"] = k["open"]
            # volume累加
            prev["volume"] = prev.get("volume", 0) + k.get("volume", 0)
            # idx_map不变: 当前位置仍映射到原始索引idx_map[-1]

    return result, idx_map


def find_fractals(klines):
    """
    顶底分型识别

    输入: 合并后的K线列表
    返回: [{type, index, high, low, strength}, ...]

    顶分型: 中间K线最高价 > 左右最高价, 且中间最低价 > 左右最低价
    底分型: 中间K线最低价 < 左右最低价, 且中间最高价 < 左右最高价
    strength: 分型强度 (0-1), 越大约可信
    """
    n = len(klines)
    fractals = []

    for i in range(1, n - 1):
        h1, h2, h3 = klines[i-1]["high"], klines[i]["high"], klines[i+1]["high"]
        l1, l2, l3 = klines[i-1]["low"], klines[i]["low"], klines[i+1]["low"]

        # 顶分型: 中间最高 > 两边, 中间最低 > 两边
        if h2 > h1 and h2 > h3 and l2 > l1 and l2 > l3:
            # 强度: 中间比两边高出的比例
            strength = min(
                (h2 - h1) / max(h1, 0.01),
                (h2 - h3) / max(h3, 0.01),
            )
            strength = min(strength, 1.0)  # 归一化
            fractals.append({
                "type": "top",
                "index": i,
                "high": h2,
                "low": l2,
                "strength": max(strength, 0.01),
            })

        # 底分型: 中间最低 < 两边, 中间最高 < 两边
        elif l2 < l1 and l2 < l3 and h2 < h1 and h2 < h3:
            strength = min(
                (l1 - l2) / max(l2, 0.01),
                (l3 - l2) / max(l2, 0.01),
            )
            strength = min(strength, 1.0)
            fractals.append({
                "type": "bottom",
                "index": i,
                "high": h2,
                "low": l2,
                "strength": max(strength, 0.01),
            })

    return fractals


def build_bi(fractals, klines, min_bi_amp_pct=0.0):
    """
    笔的构建

    输入:
      fractals: find_fractals 的输出 (已按index排序)
      klines: 原始K线 (用于幅度计算, 传None则用merged)
      min_bi_amp_pct: 最小笔幅度 (%)

    返回: [{type, s, e, s_price, e_price, high, low, amp_pct}, ...]

    规则:
      1. 顶底分型交替连接 (不能连续两个顶或两个底)
      2. 顶底之间至少隔1根K线 (index差>=2)
      3. 强度过滤: 弱分型优先被舍弃
      4. 相同方向的分型: 取更极端的 (顶取更高, 底取更低)
    """
    if len(fractals) < 2:
        return []

    bis = []
    # 第一步: 过滤并构建候选笔
    candidates = []

    # 相同方向分型处理: 顶取最高, 底取最低
    i = 0
    while i < len(fractals):
        f = fractals[i]
        if f["type"] == "top":
            # 连续多个顶: 取最高
            best = f
            j = i + 1
            while j < len(fractals) and fractals[j]["type"] == "top":
                if fractals[j]["high"] > best["high"]:
                    best = fractals[j]
                j += 1
            candidates.append(best)
            i = j
        else:
            # 连续多个底: 取最低
            best = f
            j = i + 1
            while j < len(fractals) and fractals[j]["type"] == "bottom":
                if fractals[j]["low"] < best["low"]:
                    best = fractals[j]
                j += 1
            candidates.append(best)
            i = j

    # 第二步: 交替连接 (必须顶底交替)
    # 第一个分型决定起始方向
    # 从第一个分型开始, 取交替序列
    filtered = [candidates[0]]
    for c in candidates[1:]:
        if c["type"] != filtered[-1]["type"]:
            filtered.append(c)
        else:
            # 同方向: 取更极端的替换
            if c["type"] == "top" and c["high"] > filtered[-1]["high"]:
                filtered[-1] = c
            elif c["type"] == "bottom" and c["low"] < filtered[-1]["low"]:
                filtered[-1] = c

    # 第三步: 检查最小间隔 (顶底index差>=2)
    i = 0
    while i < len(filtered) - 1:
        curr = filtered[i]
        next_f = filtered[i + 1]
        gap = abs(next_f["index"] - curr["index"])
        if gap < 2:
            # 间隔不足: 删除较弱的一个
            if curr["strength"] <= next_f["strength"]:
                del filtered[i]
            else:
                del filtered[i + 1]
            # 重新检查
            if i > 0:
                i -= 1
            continue
        i += 1

    # 第四步: 构建笔
    for i in range(len(filtered) - 1):
        f1 = filtered[i]
        f2 = filtered[i + 1]

        if f1["type"] == "bottom" and f2["type"] == "top":
            bi_type = "up"
            s_price = f1["low"]
            e_price = f2["high"]
            bi_high = max(f1["high"], f2["high"])
            bi_low = min(f1["low"], f2["low"])
            for j in range(f1["index"], f2["index"] + 1):
                if j < len(klines):
                    bi_high = max(bi_high, klines[j]["high"])
                    bi_low = min(bi_low, klines[j]["low"])
        elif f1["type"] == "top" and f2["type"] == "bottom":
            bi_type = "down"
            s_price = f1["high"]
            e_price = f2["low"]
            bi_high = max(f1["high"], f2["high"])
            bi_low = min(f1["low"], f2["low"])
            for j in range(f1["index"], f2["index"] + 1):
                if j < len(klines):
                    bi_high = max(bi_high, klines[j]["high"])
                    bi_low = min(bi_low, klines[j]["low"])
        else:
            continue  # 不应该发生

        amp_pct = (bi_high - bi_low) / max(bi_low, 0.01) * 100

        if amp_pct >= min_bi_amp_pct:
            bis.append({
                "type": bi_type,
                "s": f1["index"],
                "e": f2["index"],
                "s_price": s_price,
                "e_price": e_price,
                "high": bi_high,
                "low": bi_low,
                "amp_pct": amp_pct,
            })

    return bis


def _get_bi_extreme(bi, extreme_type):
    """获取笔的极值 (顶=high, 底=low)"""
    return bi["high"] if extreme_type == "top" else bi["low"]


def build_duan(bis, klines):
    """
    线段构建 — 特征序列法

    输入:
      bis: build_bi 的输出 (已按时间排序)
      klines: 原始K线 (用于索引映射)

    返回: [{type, s, e, s_idx, e_idx, high, low}, ...]

    算法:
      1. 从第一笔开始, 段方向 = 第一笔方向
      2. 特征序列 = 段内反向笔
      3. 段结束条件:
         - 向上段: 特征序列(向下笔)出现"底在抬高" (后一个向下笔的low > 前一个向下笔的low)
         - 向下段: 特征序列(向上笔)出现"顶在降低" (后一个向上笔的high < 前一个向上笔的high)
      4. 段结束 = 确认破坏, 新段从破坏笔开始
    """
    if len(bis) < 2:
        if bis:
            # 只有一笔也构成一个段
            h = max(bis[0]["high"], klines[bis[0]["s"]]["high"])
            l = min(bis[0]["low"], klines[bis[0]["s"]]["low"])
            return [{
                "type": bis[0]["type"],
                "s": bis[0]["s"],
                "e": bis[0]["e"],
                "s_idx": 0,
                "e_idx": 0,
                "high": h,
                "low": l,
            }]
        return []

    duans = []
    seg_start = 0
    seg_dir = bis[0]["type"]  # 'up' or 'down'

    for i in range(1, len(bis)):
        bi = bis[i]
        # 当前段方向 = 段内第一笔的方向
        # 特征序列 = 段内反向笔

        # 检查是否破坏
        if seg_dir == "up":
            # 向上段: 特征序列是向下笔
            # 收集段内所有向下笔 (从seg_start到i)
            down_bis = [bis[j] for j in range(seg_start, i + 1)
                       if bis[j]["type"] == "down"]
            if len(down_bis) >= 2:
                # 检查底是否在抬高: 后一个向下笔的low > 前一个向下笔的low
                # 说明向下笔的力度在减弱 → 向上段被破坏
                if down_bis[-1]["low"] > down_bis[-2]["low"]:
                    # 确认破坏: 段结束至i-1, 新段从i开始(断裂笔)
                    seg_end = i - 1
                    _finish_duan(duans, bis, seg_start, seg_end, klines)
                    seg_start = i  # 新段从断裂笔开始 (修复: 从i不是i-1)
                    seg_dir = bis[seg_start]["type"]
        else:
            # 向下段: 特征序列是向上笔
            up_bis = [bis[j] for j in range(seg_start, i + 1)
                     if bis[j]["type"] == "up"]
            if len(up_bis) >= 2:
                # 检查顶是否在降低: 后一个向上笔的high < 前一个向上笔的high
                # 说明向上笔的力度在减弱 → 向下段被破坏
                if up_bis[-1]["high"] < up_bis[-2]["high"]:
                    seg_end = i - 1
                    _finish_duan(duans, bis, seg_start, seg_end, klines)
                    seg_start = i  # 修复: 从i不是i-1
                    seg_dir = bis[seg_start]["type"]

    # 最后一笔到结束
    if seg_start < len(bis):
        _finish_duan(duans, bis, seg_start, len(bis) - 1, klines)

    return duans


def _finish_duan(duans, bis, s, e, klines):
    """完成一个段: 计算段内极值, 写入duans列表"""
    seg_bis = bis[s:e + 1]
    # 段内最高价 = 所有笔的高 + 笔间K线高
    h = max(b["high"] for b in seg_bis)
    l = min(b["low"] for b in seg_bis)
    # 检查笔间的K线
    for j in range(bis[s]["s"], bis[e]["e"] + 1):
        if j < len(klines):
            h = max(h, klines[j]["high"])
            l = min(l, klines[j]["low"])

    # 段方向 = 段内第一笔方向
    duans.append({
        "type": bis[s]["type"],
        "s": bis[s]["s"],
        "e": bis[e]["e"],
        "s_idx": s,
        "e_idx": e,
        "high": h,
        "low": l,
    })


def find_zhongshu_duan(duans):
    """
    中枢检测 — 三段线段重叠

    输入: build_duan 的输出
    返回: [{s, e, zg, zd, w, type}, ...]
          - s, e: 中枢起始/结束K线索引 (原始K线空间)
          - zg, zd: 中枢上沿/下沿价格
          - w: 覆盖的K线宽度
          - type: "up"/"down" (中枢方向)

    算法: 连续3段的价格区间有重叠 → 构成中枢
      ZG = min(段1.高, 段2.高, 段3.高)  [三段的最高点中的最小值]
      ZD = max(段1.低, 段2.低, 段3.低)  [三段的最低点中的最大值]
      要求 ZG > ZD (中枢有宽度)
    """
    if len(duans) < 3:
        return []

    zhongshus = []

    for i in range(len(duans) - 2):
        d1, d2, d3 = duans[i], duans[i + 1], duans[i + 2]

        # 三段必须方向交替: 上→下→上 或 下→上→下
        if not (d1["type"] != d2["type"] and d2["type"] != d3["type"]):
            continue

        # ZG = 三段最高点中的最小值
        zg = min(d1["high"], d2["high"], d3["high"])
        # ZD = 三段最低点中的最大值
        zd = max(d1["low"], d2["low"], d3["low"])

        if zg > zd:
            # 中枢范围: 第一段的起点 ~ 第三段的终点
            zhongshus.append({
                "s": d1["s"],
                "e": d3["e"],
                "zg": zg,
                "zd": zd,
                "w": d3["e"] - d1["s"] + 1,
                "type": d1["type"],  # 中枢方向 = 第一段方向
                "segments": [i, i + 1, i + 2],
            })

    # 合并重叠的中枢 (宽度优先)
    if not zhongshus:
        return []

    zhongshus.sort(key=lambda x: (-x["w"], x["s"]))
    merged = []
    for z in zhongshus:
        if not any(m["s"] <= z["s"] and m["e"] >= z["e"] for m in merged):
            merged.append(z)
    merged.sort(key=lambda x: x["s"])

    return merged


def analyze_level(klines, min_bi_amp_pct=0.0):
    """
    完整级别分析入口

    输入: 原始K线列表
    返回: {
        "merged": [...],      # 合并后的K线
        "idx_map": [...],     # 合并位置→原始索引映射
        "fractals": [...],    # 顶底分型
        "bis": [...],         # 笔
        "duans": [...],       # 线段
        "zhongshus": [...],   # 中枢 (兼容find_zhongshu格式, 索引已映射回原始K线空间)
    }

    注意: 所有返回的s/e索引都是原始K线空间 (通过idx_map映射)
    """
    merged, idx_map = merge_klines(klines)
    fractals = find_fractals(merged)
    bis = build_bi(fractals, klines, min_bi_amp_pct)
    duans = build_duan(bis, klines)
    zhongshus = find_zhongshu_duan(duans)

    # 将中枢索引从合并空间映射回原始K线空间
    for z in zhongshus:
        if z["s"] < len(idx_map):
            z["s"] = idx_map[z["s"]]
        if z["e"] < len(idx_map):
            z["e"] = idx_map[z["e"]]

    return {
        "merged": merged,
        "idx_map": idx_map,
        "fractals": fractals,
        "bis": bis,
        "duans": duans,
        "zhongshus": zhongshus,
    }


def recurse_level(klines, from_level, to_level):
    """
    跨级别K线合并递归 — 5min→30min→日线

    输入:
      klines: 低级别K线列表 [{"open":f, "high":f, "low":f, "close":f, "volume":f}, ...]
      from_level: 输入级别 ("5min", "30min")
      to_level: 输出级别 ("30min", "日线", "日线直接")

    返回: 高级别K线列表 [{"open":f, "high":f, "low":f, "close":f, "volume":f}, ...]

    聚合规则:
      - 5min → 30min: 每6根合成1根 (6×5min=30min)
      - 30min → 日线: 每8根合成1根 (8×30min=4h, A股交易时间)
      - 5min → 日线: 每48根合成1根 (48×5min=4h)

    OHLC规则:
      Open = 组内第1根的开盘价
      High = 组内最高价
      Low  = 组内最低价
      Close = 组内最后1根的收盘价
      Volume = 组内成交量之和
    """
    if not klines:
        return []

    # 确定合并因子
    if from_level == "5min" and to_level == "30min":
        n = 6  # 6 × 5min = 30min
    elif from_level == "30min" and to_level == "日线":
        n = 8  # 8 × 30min = 4h (A股交易日)
    elif from_level == "5min" and to_level == "日线":
        n = 48  # 48 × 5min = 4h
    else:
        raise ValueError(f"不支持的级别合并: {from_level} → {to_level}")

    result = []
    i = 0
    while i < len(klines):
        group = klines[i:i + n]
        if len(group) < n:
            # 最后一组不足n根: 丢弃(不完整时段)
            break

        bar = {
            "open": group[0]["open"],
            "high": max(k["high"] for k in group),
            "low": min(k["low"] for k in group),
            "close": group[-1]["close"],
            "volume": sum(k.get("volume", 0) for k in group),
        }
        result.append(bar)
        i += n

    return result


def recurse_full_pipeline(klines_5min):
    """
    完整递归管线: 5min → 30min → 日线

    输入: 5分钟K线列表
    返回: {
        "5min": klines_5min,                    # 原始5min
        "30min": [...],                          # 递归合成的30min
        "日线": [...],                            # 递归合成的日线
        "stats": {"5min": N, "30min": N, "日线": N}
    }
    """
    klines_30min = recurse_level(klines_5min, "5min", "30min")
    klines_daily = recurse_level(klines_30min, "30min", "日线")

    return {
        "5min": klines_5min,
        "30min": klines_30min,
        "日线": klines_daily,
        "stats": {
            "5min": len(klines_5min),
            "30min": len(klines_30min),
            "日线": len(klines_daily),
        },
    }


def print_summary(result, name=""):
    """打印级别分析摘要 (调试用)"""
    merged = result["merged"]
    fractals = result["fractals"]
    bis = result["bis"]
    duans = result["duans"]
    zhongshus = result["zhongshus"]

    print(f"\n{'='*60}")
    print(f"  {name}" if name else "")
    print(f"  K线: 原始{len(merged)}根 (包含处理后)")
    print(f"  顶底分型: {len(fractals)}个")
    tops = sum(1 for f in fractals if f["type"] == "top")
    bottoms = sum(1 for f in fractals if f["type"] == "bottom")
    print(f"    顶: {tops}, 底: {bottoms}")
    print(f"  笔: {len(bis)}条")
    for i, b in enumerate(bis):
        print(f"    {i}: {b['type']} [{b['s']}→{b['e']}] ¥{b['low']:.2f}~{b['high']:.2f} ({b['amp_pct']:.2f}%)")
    print(f"  段: {len(duans)}条")
    for i, d in enumerate(duans):
        print(f"    {i}: {d['type']} [{d['s']}→{d['e']}] ¥{d['low']:.2f}~{d['high']:.2f}")
    print(f"  中枢: {len(zhongshus)}个")
    for i, z in enumerate(zhongshus):
        print(f"    {i}: [{z['s']}→{z['e']}] ZG={z['zg']:.2f} ZD={z['zd']:.2f} w={z['w']}")
    print(f"{'='*60}")