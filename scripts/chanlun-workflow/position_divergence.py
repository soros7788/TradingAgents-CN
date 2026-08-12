"""
持仓背驰确认 V2 (2026-08-10)

功能:
  1. 读取当前持仓 (daily_workflow.get_today_holdings)
  2. 对每只持仓运行多级别背驰检测
  3. 核心: 判断持仓是否出现【顶背驰】确认信号

核心逻辑:
  - 持仓股票买入时依据底背驰(一买)信号, 持仓期间需要持续监控卖点
  - 【顶背驰确认】= 30min双中枢/单中枢顶背驰 → 强卖出信号, 清仓
  - 【一买低点破位】= 价格跌破一买最低点 → 买入逻辑失效, 清仓
  - 【30min看空信号】= 30min一卖信号增多 → 风险上升, 减仓
  - 【无顶背驰+无看空】= 持仓逻辑依然有效 → 继续持有

V2 更新 (2026-08-10):
  - 修复: 原版只检查底背驰是否有效, 未检查顶背驰是否出现
  - 持仓期间的核心问题是"是否出现顶背驰卖点", 而非"底背驰买点是否还在"
"""

import sys, os, time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from beichi_analyzer import detect_multilevel_buy_signals

# 导入持仓读取
try:
    from daily_workflow import get_today_holdings, get_account_summary
    _HAS_DAILY = True
except ImportError as e:
    _HAS_DAILY = False
    _IMPORT_ERR = str(e)


def _confirm_holding_divergence(holding, ml):
    """
    对单只持仓执行顶背驰确认 (V2 重写)

    对于已持仓的股票, 核心问题是"是否出现顶背驰卖点",
    而非"底背驰买点是否还在".

    判定优先级 (从高到低):
      1. 一买低点破位 → 强制清仓 (买入逻辑彻底失效)
      2. 30min双中枢顶背驰确认 → 清仓 (强顶背驰卖点)
      3. 30min单中枢顶背驰确认 → 减仓 (弱顶背驰卖点)
      4. 三买确认豁免 → 持有 (三买结构中B点回调正常)
      5. 30min看空信号密集 → 减仓 (风险上升)
      6. 无顶背驰+无看空 → 持有 (持仓逻辑有效)

    参数:
        holding: dict from get_today_holdings()
        ml: dict from detect_multilevel_buy_signals()

    返回: dict
        status: "顶背驰确认" / "底背驰失效" / "风险上升" / "背驰有效"
        divergence_type: str 背驰类型
        signal_label: str 买卖区间标注
        confidence: float 置信度
        details: list[str]
        action: "清仓" / "减仓" / "持有"
        risk: "高" / "中" / "低"
        top_divergence_confirmed: bool 是否顶背驰确认
    """
    name = holding.get("name", "?")
    code = holding.get("code", "?")
    shares = holding.get("shares", 0) or 0
    entry = holding.get("entry", 0) or 0
    close = holding.get("close", 0) or 0
    profit = holding.get("profit", 0) or 0
    pos = holding.get("pos", 0) or 0
    stop = holding.get("stop", 0) or 0
    t1_lock = holding.get("t1_lock", "")

    details = []
    risk = "低"

    # 提取顶背驰数据
    dc_top = ml.get("min30_double_center_top", {})
    sc_top = ml.get("min30_single_center_top", {})

    # 提取底背驰/支撑数据
    one_buy_low = ml.get("one_buy_low")
    zhongyin_active = ml.get("zhongyin_active", False)
    zhongyin_state = ml.get("zhongyin_state", "DOWN")
    daily_dir = ml.get("daily_dir", "flat")
    min30_dir = ml.get("min30_dir", "flat")
    daily_dl_p = ml.get("daily_dl_p", 0)
    daily_ep_p = ml.get("daily_ep_p", 0)

    # 提取看空信号
    min30_sell_count = ml.get("min30_sell_count", 0)
    min30_sell_dl_p = ml.get("min30_sell_dl_p", 0)
    min5_sell_count = ml.get("min5_sell_count", 0)

    # 提取三买确认 (2026-08-10 Fix)
    # 三买确认时, B点回踩天然产生卖单信号, 应降权处理
    sanmai = ml.get("sanmai")
    sanmai_confirmed = False
    sanmai_invalid = None
    if sanmai and isinstance(sanmai, dict):
        sanmai_confirmed = sanmai.get("valid", False) and sanmai.get("geometric", {}).get("is_confirmed", False)
        sanmai_invalid = sanmai.get("geometric", {}).get("invalid_boundary")

    # ================================================================
    # 优先级1: 一买低点破位 → 强制清仓
    # 买入逻辑: 一买低点是底背驰的最低点, 跌破意味着买入逻辑彻底失效
    # ================================================================
    if one_buy_low and one_buy_low > 0 and close < one_buy_low:
        pct_below = (close - one_buy_low) / one_buy_low * 100
        details.append(f"一买低点破位(一买低={one_buy_low:.2f}, 现价{close:.2f}, 跌{pct_below:+.1f}%)")
        return {
            "status": "底背驰失效",
            "divergence_type": "一买破位",
            "signal_label": "清仓",
            "confidence": 0.0,
            "details": details,
            "action": "清仓",
            "risk": "高",
            "top_divergence_confirmed": False,
            "one_buy_broken": True,
        }

    # ================================================================
    # 优先级2: 30min双中枢顶背驰确认 → 清仓
    # 双中枢上涨趋势后出现顶背驰 = 最强卖出信号
    # 【2026-08-12 同步】用户规则: 7个中枢不一定是有效双中枢, 需考虑中枢是否重叠.
    #   有效双中枢 = 两个中枢价格区间不重叠且依次分离.
    #   大量重叠/扩张的中枢本质是大级别盘整, 不算有效双中枢 → 归单中枢(ABC)处理.
    #   此处显式校验 valid_double_center, 与 beichi_analyzer.is_valid_double_center 保持一致.
    # ================================================================
    if dc_top.get("is_top_divergence", False) and dc_top.get("valid_double_center", False):
        dc_ratio = dc_top.get("divergence_ratio", 999)
        dc_conf = dc_top.get("confidence", 0)
        details.append(f"30min有效双中枢顶背驰确认(面积比={dc_ratio:.1f}%, conf={dc_conf:.2f})")
        return {
            "status": "顶背驰确认",
            "divergence_type": "趋势顶背驰",
            "signal_label": "一卖确认",
            "confidence": dc_conf,
            "details": details,
            "action": "清仓",
            "risk": "高",
            "top_divergence_confirmed": True,
            "one_buy_broken": False,
        }
    if dc_top.get("is_top_divergence", False) and not dc_top.get("valid_double_center", False):
        # 【2026-08-12】有顶背驰但非有效双中枢(中枢重叠) → 降级为单中枢盘整顶背驰处理(优先级3)
        details.append(f"非有效双中枢(中枢重叠为盘整), 顶背驰降级为单中枢处理: {dc_top.get('reason', '')}")

    # ================================================================
    # 优先级3: 30min单中枢顶背驰确认 → 减仓/清仓
    # 单中枢盘整顶背驰 = 较弱卖出信号, 但结合浮盈决定力度
    # ================================================================
    if sc_top.get("is_top_divergence", False):
        sc_ratio = sc_top.get("divergence_ratio", 999)
        sc_conf = sc_top.get("confidence", 0)
        details.append(f"30min单中枢顶背驰确认(面积比={sc_ratio:.1f}%, conf={sc_conf:.2f})")
        # 浮盈时清仓, 浮亏时减仓(防止地板割肉)
        if profit > 0:
            return {
                "status": "顶背驰确认",
                "divergence_type": "盘整顶背驰",
                "signal_label": "减仓/清仓",
                "confidence": sc_conf,
                "details": details,
                "action": "清仓",
                "risk": "高",
                "top_divergence_confirmed": True,
                "one_buy_broken": False,
            }
        else:
            return {
                "status": "顶背驰确认",
                "divergence_type": "盘整顶背驰",
                "signal_label": "减仓观察",
                "confidence": sc_conf * 0.7,
                "details": details,
                "action": "减仓",
                "risk": "中",
                "top_divergence_confirmed": True,
                "one_buy_broken": False,
            }

    # ================================================================
    # 优先级4: 三买豁免检查 (2026-08-10 Fix)
    # 三买确认后, B点回踩天然产生卖单信号, 不应触发"风险上升"
    # 只有价格跌破三买失效边界, 才真正触发风险
    # ================================================================
    if sanmai_confirmed and sanmai_invalid is not None:
        if close < sanmai_invalid:
            details.append(f"三买确认但价格跌破失效边界(边界={sanmai_invalid:.2f}, 现价{close:.2f})")
            return {
                "status": "风险上升",
                "divergence_type": "三买失效",
                "signal_label": "减仓",
                "confidence": 0.3,
                "details": details,
                "action": "减仓",
                "risk": "中",
                "top_divergence_confirmed": False,
                "one_buy_broken": False,
            }
        else:
            # 三买有效, B点回调的卖单信号是正常结构, 跳过卖单计数检查
            details.append(f"三买确认有效(失效边界={sanmai_invalid:.2f}, 现价{close:.2f}), B点回调卖单信号不构成风险")
            return {
                "status": "背驰有效",
                "divergence_type": "三买持有",
                "signal_label": "持有",
                "confidence": max(daily_ep_p, daily_dl_p, 0.5),
                "details": details,
                "action": "持有",
                "risk": "低",
                "top_divergence_confirmed": False,
                "one_buy_broken": False,
            }

    # ================================================================
    # 优先级5: 30min看空信号密集 → 风险上升, 减仓
    # 无顶背驰但看空信号较多 → 需要降低风险暴露
    # 注意: 三买确认的持仓已在上方被豁免, 不会进入此分支
    # ================================================================
    sell_reasons = []
    sell_risk = 0

    if min30_sell_count >= 3:
        sell_reasons.append(f"30min看空信号密集({min30_sell_count}个)")
        sell_risk += 2
    elif min30_sell_count >= 1:
        sell_reasons.append(f"30min看空信号({min30_sell_count}个)")
        sell_risk += 1
    if min5_sell_count >= 5:
        sell_reasons.append(f"5min看空信号密集({min5_sell_count}个)")
        sell_risk += 1
    if daily_dir == "down":
        sell_reasons.append("日线趋势向下")
        sell_risk += 1
    if profit < -0.05:
        sell_reasons.append(f"浮亏{profit*100:.1f}%")
        sell_risk += 1
    if zhongyin_active and zhongyin_state != "SECOND_BUY_CONFIRMED":
        sell_reasons.append(f"中阴状态[{zhongyin_state}]")
        sell_risk += 1

    if sell_risk >= 3:
        details.extend(sell_reasons)
        return {
            "status": "风险上升",
            "divergence_type": "看空信号累积",
            "signal_label": "减仓",
            "confidence": max(0, 1.0 - sell_risk * 0.2),
            "details": details,
            "action": "减仓",
            "risk": "中",
            "top_divergence_confirmed": False,
            "one_buy_broken": False,
        }

    if sell_risk >= 1:
        details.extend(sell_reasons)
        return {
            "status": "风险上升",
            "divergence_type": "看空信号",
            "signal_label": "观察",
            "confidence": max(0.3, 1.0 - sell_risk * 0.15),
            "details": details,
            "action": "持有",
            "risk": "低",
            "top_divergence_confirmed": False,
            "one_buy_broken": False,
        }

    # ================================================================
    # 优先级5: 无顶背驰+无看空 → 背驰有效, 继续持有
    # 持仓逻辑依然有效, 没有卖出信号
    # ================================================================
    details.append("无顶背驰确认, 无看空信号, 持仓逻辑有效")
    return {
        "status": "背驰有效",
        "divergence_type": "底背驰持有",
        "signal_label": "持有",
        "confidence": max(daily_dl_p, daily_ep_p, 0.3),
        "details": details,
        "action": "持有",
        "risk": "低",
        "top_divergence_confirmed": False,
        "one_buy_broken": False,
    }


def position_divergence_report(silent=False):
    """持仓背驰确认主入口 (V2)

    读取当前持仓, 对每只股票执行顶背驰确认, 输出结构化报告.

    参数:
        silent: bool 静默模式

    返回: dict
        summary: {
            "total_holdings": int 持仓总数,
            "top_divergence_confirmed": int 顶背驰确认数,
            "buy_thesis_failed": int 底背驰失效数,
            "risk_rising": int 风险上升数,
            "valid": int 背驰有效数,
            "total_asset": float, "cash": float, "position_ratio": float,
        }
        holdings: list[dict] 每只持仓的背驰确认结果
        sell_alerts: list[dict] 需要清仓的清单
        reduce_alerts: list[dict] 需要减仓的清单
    """
    if not _HAS_DAILY:
        err = f"无法导入daily_workflow: {_IMPORT_ERR}"
        if not silent:
            print(f"[持仓背驰] ❌ {err}")
        return {"error": err}

    # 1. 读取账户信息
    try:
        account = get_account_summary()
    except Exception as e:
        account = {"total_asset": 0, "cash": 0, "position_ratio": 0}

    # 2. 读取持仓
    try:
        holdings = get_today_holdings()
    except Exception as e:
        if not silent:
            print(f"[持仓背驰] ❌ 读取持仓失败: {e}")
        return {"error": f"读取持仓失败: {e}"}

    if not holdings:
        if not silent:
            print("[持仓背驰] 无持仓, 跳过")
        return {
            "summary": {
                "total_holdings": 0,
                "top_divergence_confirmed": 0,
                "buy_thesis_failed": 0,
                "risk_rising": 0,
                "valid": 0,
                "total_asset": account.get("total_asset", 0),
                "cash": account.get("cash", 0),
                "position_ratio": account.get("position_ratio", 0),
            },
            "holdings": [],
            "sell_alerts": [],
            "reduce_alerts": [],
        }

    if not silent:
        print(f"\n{'='*60}")
        print(f"  持仓背驰确认报告 V2 — {time.strftime('%Y-%m-%d %H:%M')}")
        print(f"{'='*60}")
        print(f"  总资产: {account.get('total_asset', 0):.2f}")
        print(f"  可用现金: {account.get('cash', 0):.2f}")
        print(f"  仓位比例: {account.get('position_ratio', 0)*100:.1f}%")
        print(f"  持仓个数: {len(holdings)}")
        print(f"{'='*60}")

    results = []
    sell_alerts = []
    reduce_alerts = []

    for i, h in enumerate(holdings):
        code = h.get("code", "")
        name = h.get("name", "?")
        shares = h.get("shares", 0) or 0
        entry = h.get("entry", 0) or 0
        close = h.get("close", 0) or 0
        profit = h.get("profit", 0) or 0
        pos = h.get("pos", 0) or 0
        stop = h.get("stop", 0) or 0
        t1_lock = h.get("t1_lock", "")

        if not silent:
            print(f"\n[{i+1}/{len(holdings)}] {name}({code})")
            print(f"   持仓: {shares}股 | 成本: {entry:.2f} | 现价: {close:.2f} | "
                  f"盈亏: {profit*100:.2f}% | 仓位: {pos*100:.1f}%")

        # 运行多级别背驰检测
        try:
            ml = detect_multilevel_buy_signals(code, price=close)
        except Exception as e:
            if not silent:
                print(f"   ⚠ 背驰检测失败: {e}")
            results.append({
                "code": code, "name": name,
                "shares": shares, "entry": entry, "close": close,
                "profit": profit, "pos": pos,
                "status": "检测失败", "action": "保持",
                "confidence": 0, "risk": "未知",
                "details": [f"分析异常: {e}"],
                "top_divergence_confirmed": False,
            })
            continue

        # 执行顶背驰确认
        confirm = _confirm_holding_divergence(h, ml)

        # 补充持仓信息
        confirm["code"] = code
        confirm["name"] = name
        confirm["shares"] = shares
        confirm["entry"] = entry
        confirm["close"] = close
        confirm["profit"] = profit
        confirm["pos"] = pos
        confirm["stop"] = stop
        confirm["t1_lock"] = t1_lock

        # 补充关键指标
        confirm["daily_dl_p"] = ml.get("daily_dl_p", 0)
        confirm["daily_ep_p"] = ml.get("daily_ep_p", 0)
        confirm["daily_dir"] = ml.get("daily_dir", "flat")
        confirm["min30_dir"] = ml.get("min30_dir", "flat")
        confirm["min30_ep_p"] = ml.get("min30_ep_p", 0)
        confirm["min30_sell_count"] = ml.get("min30_sell_count", 0)
        confirm["min5_sell_count"] = ml.get("min5_sell_count", 0)
        confirm["zhongyin_active"] = ml.get("zhongyin_active", False)
        confirm["one_buy_low"] = ml.get("one_buy_low")

        # 顶背驰细节
        dc_top = ml.get("min30_double_center_top", {})
        sc_top = ml.get("min30_single_center_top", {})
        confirm["min30_dc_top"] = {
            "has": dc_top.get("has_double_center", False),
            "is_divergence": dc_top.get("is_top_divergence", False),
            "ratio": dc_top.get("divergence_ratio", 999),
            "confidence": dc_top.get("confidence", 0),
        }
        confirm["min30_sc_top"] = {
            "has": sc_top.get("has_single_center", False),
            "is_divergence": sc_top.get("is_top_divergence", False),
            "ratio": sc_top.get("divergence_ratio", 999),
            "confidence": sc_top.get("confidence", 0),
        }

        # 底背驰细节 (保留用于对比)
        dc = ml.get("min30_double_center", {})
        sc = ml.get("min30_single_center", {})
        confirm["min30_dc_bottom"] = {
            "has": dc.get("has_double_center", False),
            "is_divergence": dc.get("is_divergence", False),
            "ratio": dc.get("divergence_ratio", 999),
        }
        confirm["min30_sc_bottom"] = {
            "has": sc.get("has_single_center", False),
            "is_divergence": sc.get("is_divergence", False),
            "ratio": sc.get("divergence_ratio", 999),
        }

        results.append(confirm)

        if not silent:
            # 状态图标
            status_icon = {
                "顶背驰确认": "🔴", "底背驰失效": "🔴",
                "风险上升": "🟡", "背驰有效": "✅",
                "检测失败": "⚪",
            }.get(confirm["status"], "⚪")

            action_icon = {
                "清仓": "🔴", "减仓": "⬇️",
                "持有": "✅", "保持": "⚪",
            }.get(confirm["action"], "⚪")

            print(f"   顶背驰: {status_icon} [{confirm['status']}] "
                  f"{confirm['divergence_type']}")
            print(f"   操作: {action_icon} {confirm['action']} | "
                  f"置信度: {confirm['confidence']:.2f} | 风险: {confirm['risk']}")
            for d in confirm["details"]:
                print(f"   → {d}")

        # 收集清仓预警
        if confirm["action"] in ("清仓",):
            sell_alerts.append(confirm)

        # 收集减仓预警
        if confirm["action"] in ("减仓",):
            reduce_alerts.append(confirm)

    # 汇总统计
    top_count = sum(1 for r in results if r.get("status") == "顶背驰确认")
    failed_count = sum(1 for r in results if r.get("status") == "底背驰失效")
    risk_count = sum(1 for r in results if r.get("status") == "风险上升")
    valid_count = sum(1 for r in results if r.get("status") == "背驰有效")
    error_count = sum(1 for r in results if r.get("status") == "检测失败")

    summary = {
        "total_holdings": len(holdings),
        "top_divergence_confirmed": top_count,
        "buy_thesis_failed": failed_count,
        "risk_rising": risk_count,
        "valid": valid_count,
        "errors": error_count,
        "total_asset": account.get("total_asset", 0),
        "cash": account.get("cash", 0),
        "position_ratio": account.get("position_ratio", 0),
    }

    if not silent:
        print(f"\n{'='*60}")
        print(f"  汇总: 顶背驰确认{top_count} | 底背驰失效{failed_count} | "
              f"风险上升{risk_count} | 有效{valid_count}")
        if sell_alerts:
            print(f"  🔴 清仓预警: {len(sell_alerts)}只")
            for s in sorted(sell_alerts, key=lambda x: x.get("risk", "低")):
                _r = s.get("risk", "?")
                _n = s.get("name", "?")
                _c = s.get("code", "?")
                _p = s.get("profit", 0)
                _t = s.get("divergence_type", "?")
                print(f"    {_r} {_n}({_c}) {_t} 盈亏{_p:+.2f}%")
        if reduce_alerts:
            print(f"  🟡 减仓预警: {len(reduce_alerts)}只")
            for r in reduce_alerts:
                _n = r.get("name", "?")
                _c = r.get("code", "?")
                _t = r.get("divergence_type", "?")
                print(f"    {_n}({_c}) {_t}")
        print(f"{'='*60}\n")

    return {
        "summary": summary,
        "holdings": results,
        "sell_alerts": sell_alerts,
        "reduce_alerts": reduce_alerts,
    }


def main():
    """CLI入口"""
    position_divergence_report(silent=False)


if __name__ == "__main__":
    main()