# -*- coding: utf-8 -*-
"""
交易计划生成模块 (2026-08-09) V1
基于持仓分析 + 全市场扫描结果, 生成结构化交易计划

输出格式:
  1. 持仓状态评估 (多级别信号 + 动态仓位 + 止盈/止损)
  2. 买入候选排序 (核心池/观察池/边缘池 + 资金匹配)
  3. 卖出建议 (减仓/清仓/去弱留强)
  4. 资金分配 (总资产/现金/可用/需转入)
  5. 优先级排序 (今日必做/今日关注/持续跟踪)

用法:
  from trading_plan import build_trading_plan, format_plan, format_plan_short

  # 从 daily_workflow 获取数据
  plan = build_trading_plan(holdings=holdings, scan_result=scan_data, account=account)
  print(format_plan(plan))
"""

import sys, os, time

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from beichi_analyzer import detect_multilevel_buy_signals, detect_sell_signals
from full_scan import calc_funding


def calc_add_position(code, current_shares, current_cost, current_price, cash,
                      buy_range_name="三买", buy_range_low=None, buy_range_high=None,
                      invalid_boundary=None):
    """
    加仓计算器 V1 (2026-08-11)

    计算需加仓多少股，使新均价落在目标买点区间内（合规要求）。

    数学公式:
        x = S * (target - E) / (P - target)
        S = 当前股数, E = 成本, P = 现价, target = 目标均价

    约束:
        1. target ∈ (E, min(P, buy_range_high))   — 目标>成本(拉高均价)且<现价
        2. target ∈ [buy_range_low, buy_range_high] — 合规落在买点区间内
        3. 加仓股数不可超过现金上限

    四舍五入豁免 (2026-08-11):
        x小数部分 >= 0.5 → 向上取整到整百股
        x小数部分 < 0.5 → 向下取整到整百股

    参数:
        code: 股票代码
        current_shares: 当前持仓股数
        current_cost: 当前持仓成本
        current_price: 当前价格
        cash: 可用现金
        buy_range_name: 目标买点区间名称 ("二买" / "三买")
        buy_range_low: 买点区间下限
        buy_range_high: 买点区间上限
        invalid_boundary: 失效边界(跌破则清仓)

    返回: dict
        feasible: bool 是否可行
        reason: str 可行/不可行原因
        target: float 推荐目标均价
        additional_shares: int 需加仓股数(整百)
        cost: float 需资金
        new_avg: float 新均价
        new_total_shares: int 新总股数
        new_position_value: float 新持仓市值
        cash_remaining: float 剩余现金
        cash_used_pct: float 现金使用比例
        margin_to_failure: float 安全垫百分比(距失效边界)
        buy_range_name: str
        buy_range_low: float
        buy_range_high: float
        rounding_exempted: bool 是否使用了四舍五入豁免
        raw_shares: float 计算原始股数(未取整)
        test_scenarios: list[dict] 多target测试结果
        recommend: dict 推荐场景
    """
    result = {
        "feasible": False,
        "reason": "",
        "target": None,
        "additional_shares": 0,
        "cost": 0,
        "new_avg": current_cost,
        "new_total_shares": current_shares,
        "new_position_value": current_shares * current_price,
        "cash_remaining": cash,
        "cash_used_pct": 0,
        "margin_to_failure": 0,
        "buy_range_name": buy_range_name,
        "buy_range_low": buy_range_low,
        "buy_range_high": buy_range_high,
        "rounding_exempted": False,
        "raw_shares": 0,
        "test_scenarios": [],
        "recommend": None,
    }

    S = current_shares
    E = current_cost
    P = current_price

    # 前置检查
    if S <= 0:
        result["reason"] = "当前持仓0股，无需加仓"
        return result
    if E <= 0 or P <= 0:
        result["reason"] = "成本或现价无效"
        return result
    if buy_range_low is None or buy_range_high is None:
        result["reason"] = f"{buy_range_name}区间未定义，无法作为加仓目标"
        return result
    if buy_range_high <= E:
        result["reason"] = (f"{buy_range_name}区间上限({buy_range_high:.2f}) <= 当前成本({E:.2f})，"
                           f"加仓无法降低均价到区间内(买在高位只会拉高均价)")
        return result

    # 生成测试场景: 在(E, min(P, buy_range_high)]区间内均匀取点
    max_target = min(P, buy_range_high)
    effective_low = max(E, buy_range_low)
    if effective_low >= max_target:
        result["reason"] = (f"有效区间为空: 目标下限({effective_low:.2f}) >= 上限({max_target:.2f})，"
                           f"当前成本({E:.2f})已接近或超过{ buy_range_name}区间")
        return result

    step = (max_target - effective_low) / 5
    test_targets = []
    for i in range(1, 6):
        t = effective_low + step * i
        test_targets.append(round(t, 2))

    scenarios = []
    for target in test_targets:
        # x = S * (target - E) / (P - target)
        if P <= target:
            continue
        raw_x = S * (target - E) / (P - target)
        if raw_x <= 0:
            continue

        # 四舍五入豁免
        frac = raw_x - int(raw_x)
        exempted = False
        if frac >= 0.5:
            rounded_x = ((int(raw_x) // 100) + 1) * 100
            exempted = True
        else:
            rounded_x = (int(raw_x) // 100) * 100

        if rounded_x <= 0:
            continue

        cost = rounded_x * P
        if cost > cash:
            continue

        new_shares = S + rounded_x
        new_avg = (S * E + rounded_x * P) / new_shares

        # 【Fix 2026-08-11】后置校验: 新均价必须落在买点区间内
        # 防止因取整导致加仓量不足/过多, 新均价偏离区间
        if new_avg < buy_range_low or new_avg > buy_range_high:
            continue

        new_mv = new_shares * P
        cash_rem = cash - cost
        cash_pct = cost / cash * 100 if cash > 0 else 0

        margin = 0
        if invalid_boundary and invalid_boundary > 0:
            margin = (new_avg - invalid_boundary) / invalid_boundary * 100

        scenarios.append({
            "target": target,
            "raw_x": round(raw_x, 2),
            "rounded_x": rounded_x,
            "rounding_exempted": exempted,
            "cost": round(cost, 2),
            "new_avg": round(new_avg, 2),
            "new_shares": new_shares,
            "new_mv": round(new_mv, 2),
            "cash_used_pct": round(cash_pct, 1),
            "cash_remaining": round(cash_rem, 2),
            "margin_to_failure": round(margin, 1),
        })

    if not scenarios:
        result["reason"] = "所有target均不可达(现金不足或target超出范围)"
        return result

    # 选推荐: 距离失效边界最远 + 资金使用率适中的场景
    # 按安全垫降序, 取现金使用率<=50%的最优解
    candidates = [s for s in scenarios if s["cash_used_pct"] <= 50]
    if not candidates:
        # 没有低资金使用的, 取安全垫最高的
        candidates = scenarios
    candidates.sort(key=lambda s: -s["margin_to_failure"])
    best = candidates[0]

    result["feasible"] = True
    result["reason"] = (f"{buy_range_name}区间[{buy_range_low:.2f},{buy_range_high:.2f}]内加仓可行"
                       f" → target={best['target']:.2f}")
    result["target"] = best["target"]
    result["additional_shares"] = best["rounded_x"]
    result["cost"] = best["cost"]
    result["new_avg"] = best["new_avg"]
    result["new_total_shares"] = best["new_shares"]
    result["new_position_value"] = best["new_mv"]
    result["cash_remaining"] = best["cash_remaining"]
    result["cash_used_pct"] = best["cash_used_pct"]
    result["margin_to_failure"] = best["margin_to_failure"]
    result["rounding_exempted"] = best["rounding_exempted"]
    result["raw_shares"] = best["raw_x"]
    result["test_scenarios"] = scenarios
    result["recommend"] = best

    return result


def analyze_holding(code, name, cost, price, shares, pos_pct):
    """
    分析单只持仓的多级别状态

    返回: dict
      - code, name, cost, price, shares, pos_pct
      - profit_pct: 浮盈百分比
      - profit_amount: 浮盈金额
      - profit_status: 大幅盈利/盈利/持平/亏损/深套
      - tier: 多级别分层 (核心池/观察池/边缘池/无信号)
      - 30min_dl_p, 30min_ep_p, 5min_dl_p, 5min_ep_p: 各级别信号
      - has_buy_signal: 是否有二买/三买信号
      - sell_signal: 是否有卖出信号
      - sell_reason: 卖出原因
      - dynamic_cap: 动态仓位上限
      - action: 建议动作 (持有/加仓/卖出/止盈/止损考虑/观察)
      - action_reason: 动作原因
      - urgency: 紧急程度 (0-10)
    """
    # 多级别买点信号
    try:
        ml = detect_multilevel_buy_signals(code, price=price)
    except Exception:
        ml = {}

    # 卖出信号检测
    sell_info = {"sell_now": False, "sell_reason": "", "urgency": 0}
    try:
        sell_r = detect_sell_signals(code, cost, price)
        if sell_r.get("should_clear"):
            sell_info = {
                "sell_now": True,
                "sell_reason": sell_r.get("reason", "清仓信号"),
                "urgency": 10,
            }
        elif sell_r.get("should_reduce"):
            sell_info = {
                "sell_now": True,
                "sell_reason": sell_r.get("reason", "减仓信号"),
                "urgency": 6,
            }
    except Exception:
        pass

    # 浮盈计算
    profit_pct = (price - cost) / cost * 100 if cost > 0 else 0
    profit_amount = (price - cost) * shares if shares and cost else 0

    # 盈亏状态
    if profit_pct > 10:
        profit_status = "大幅盈利"
    elif profit_pct > 3:
        profit_status = "盈利"
    elif profit_pct > -3:
        profit_status = "持平"
    elif profit_pct > -10:
        profit_status = "亏损"
    else:
        profit_status = "深套"

    # 动态仓位上限
    try:
        from daily_workflow import get_dynamic_position_cap
        cap = get_dynamic_position_cap(code, cost, price)
    except Exception:
        cap = 0.35

    # 信号提取
    tier = ml.get("tier", "无信号")
    has_buy = bool(ml.get("min30_first_buy_valid") or ml.get("ermai") or ml.get("sanmai"))
    dl_30 = ml.get("30min_dl_p", 0)
    ep_30 = ml.get("30min_ep_p", 0)
    dl_5 = ml.get("5min_dl_p", 0)
    ep_5 = ml.get("5min_ep_p", 0)
    one_buy_low = ml.get("one_buy_low")

    # 提取买点区间 (2026-08-11: 加仓计算器用)
    ermai = ml.get("ermai")
    sanmai = ml.get("sanmai")
    ermai_range = None
    sanmai_range = None
    if ermai and isinstance(ermai, dict) and ermai.get("valid"):
        geo = ermai.get("geometric", {})
        ermai_range = {
            "low": geo.get("gold_min") or geo.get("optimal_buy_min"),
            "high": geo.get("gold_max") or geo.get("optimal_buy_max"),
            "invalid": geo.get("invalid_boundary"),
        }
    if sanmai and isinstance(sanmai, dict) and sanmai.get("valid"):
        geo = sanmai.get("geometric", {})
        sanmai_range = {
            "low": geo.get("optimal_buy_min"),
            "high": geo.get("optimal_buy_max"),
            "invalid": geo.get("invalid_boundary"),
        }

    # 动作决策树
    if sell_info["sell_now"]:
        if sell_info["urgency"] >= 10:
            action = "清仓"
        else:
            action = "减仓"
        action_reason = sell_info["sell_reason"]
    elif profit_status == "大幅盈利" and ep_30 < 0.3:
        action = "止盈"
        action_reason = f"盈利{profit_pct:.1f}%+30min买点信号减弱(EP_L={ep_30:.2f})"
    elif profit_status == "深套" and tier == "无信号":
        action = "止损考虑"
        action_reason = f"亏损{profit_pct:.1f}%+无多级别买点信号"
    elif has_buy and profit_pct >= 5:
        action = "加仓"
        action_reason = f"二买/三买确认+浮盈{profit_pct:.1f}%>=5%"
    elif tier in ("核心池", "观察池"):
        action = "持有"
        action_reason = f"多级别信号在{tier}, 30min_EP_L={ep_30:.2f}"
    else:
        action = "观察"
        action_reason = f"tier={tier}, DL_P={dl_30:.2f}, EP_L={ep_30:.2f}"

    return {
        "code": code,
        "name": name,
        "cost": round(cost, 2),
        "price": round(price, 2),
        "shares": shares or 0,
        "pos_pct": pos_pct or 0,
        "profit_pct": round(profit_pct, 1),
        "profit_amount": round(profit_amount, 2),
        "profit_status": profit_status,
        "tier": tier,
        "30min_dl_p": round(dl_30, 3),
        "30min_ep_p": round(ep_30, 3),
        "5min_dl_p": round(dl_5, 3),
        "5min_ep_p": round(ep_5, 3),
        "has_buy_signal": has_buy,
        "sell_signal": sell_info["sell_now"],
        "sell_reason": sell_info["sell_reason"],
        "one_buy_low": one_buy_low,
        "dynamic_cap": cap,
        "action": action,
        "action_reason": action_reason,
        "urgency": sell_info["urgency"],
        # 买点区间 (2026-08-11: 加仓计算器用)
        "ermai_range": ermai_range,
        "sanmai_range": sanmai_range,
    }


def build_trading_plan(holdings=None, scan_result=None, account=None, buy_candidates_limit=10):
    """
    生成完整交易计划

    参数:
        holdings: list[dict] 持仓列表 (来自 get_today_holdings)
        scan_result: dict 全市场扫描结果 (来自 full_scan)
        account: dict 账户信息 (来自 get_account_summary)
        buy_candidates_limit: int 买入候选最大展示数, 默认10

    返回: dict
        - generated_at: 生成时间
        - account: 账户信息
        - holdings_analysis: list[dict] 持仓分析
        - buy_candidates: list[dict] 买入候选
        - sell_candidates: list[dict] 卖出候选
        - add_positions: list[dict] 加仓候选
        - funding: dict 资金信息
        - priorities: dict 优先级排序
        - summary: dict 摘要
    """
    plan = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M"),
        "account": account or {},
        "holdings_analysis": [],
        "buy_candidates": [],
        "sell_candidates": [],
        "add_positions": [],
        "funding": {},
        "priorities": {"today_must": [], "today_watch": [], "continuous": []},
        "summary": {},
    }

    # ============================================================
    # 1. 持仓分析
    # ============================================================
    if holdings:
        for h in holdings:
            ha = analyze_holding(
                h["code"], h["name"],
                h.get("entry", 0) or h.get("cost", 0),
                h.get("close", 0) or h.get("price", 0),
                h.get("shares", 0),
                h.get("pos", 0) or h.get("position_ratio", 0),
            )
            plan["holdings_analysis"].append(ha)

        # 排序: 卖出优先 > 紧急程度 > 浮亏优先
        plan["holdings_analysis"].sort(
            key=lambda x: (-x["urgency"], x["profit_pct"])
        )

    # ============================================================
    # 2. 买入候选 (从全市场扫描结果)
    # ============================================================
    if scan_result:
        total_asset = account.get("total_asset", 20326) if account else 20326
        cash = account.get("cash", 7847) if account else 7847

        # 核心池
        for s in scan_result.get("core", []):
            f = calc_funding(s["price"], total_asset, cash)
            plan["buy_candidates"].append({
                "code": s["code"],
                "name": s["name"],
                "price": s["price"],
                "tier": "核心",
                "dl_p": s["dlp"],
                "label": s.get("label", ""),
                "sig_type": s.get("sig_type", "盘整背驰"),   # 趋势背驰/盘整背驰
                "sig_label": s.get("sig_label", s.get("label", "ABC买卖区间")),  # 123/ABC
                "cost_1shou": f["cost"],
                "need_transfer": f["need_transfer"],
                "transfer_amount": f["transfer"],
                "position_cap": s.get("single_stock_cap", 0.35),
            })

        # 观察池
        for s in scan_result.get("watch", []):
            f = calc_funding(s["price"], total_asset, cash)
            plan["buy_candidates"].append({
                "code": s["code"],
                "name": s["name"],
                "price": s["price"],
                "tier": "观察",
                "dl_p": s["dlp"],
                "label": s.get("label", ""),
                "sig_type": s.get("sig_type", "盘整背驰"),
                "sig_label": s.get("sig_label", s.get("label", "ABC买卖区间")),
                "cost_1shou": f["cost"],
                "need_transfer": f["need_transfer"],
                "transfer_amount": f["transfer"],
                "position_cap": s.get("single_stock_cap", 0.35),
            })

        # 边缘池 (前5只)
        for s in scan_result.get("edge", [])[:5]:
            f = calc_funding(s["price"], total_asset, cash)
            plan["buy_candidates"].append({
                "code": s["code"],
                "name": s["name"],
                "price": s["price"],
                "tier": "边缘",
                "dl_p": s["dlp"],
                "label": s.get("label", ""),
                "sig_type": s.get("sig_type", "盘整背驰"),
                "sig_label": s.get("sig_label", s.get("label", "ABC买卖区间")),
                "cost_1shou": f["cost"],
                "need_transfer": f["need_transfer"],
                "transfer_amount": f["transfer"],
                "position_cap": s.get("single_stock_cap", 0.35),
            })

        # 排序: tier + dl_p降序
        tier_order = {"核心": 0, "观察": 1, "边缘": 2}
        plan["buy_candidates"].sort(
            key=lambda x: (tier_order.get(x["tier"], 9), -x["dl_p"])
        )

        # 截断
        if buy_candidates_limit > 0:
            plan["buy_candidates"] = plan["buy_candidates"][:buy_candidates_limit]

    # ============================================================
    # 3. 卖出候选 & 加仓候选
    # ============================================================
    _cash_available = account.get("cash", 7847) if account else 7847
    for h in plan["holdings_analysis"]:
        if h["action"] in ("清仓", "减仓", "止盈", "止损考虑"):
            plan["sell_candidates"].append(h)
        if h["action"] == "加仓":
            # 尝试计算加仓方案 (2026-08-11)
            add_calc = None
            # 优先用三买 => 二买
            for range_name, range_key in [("三买", "sanmai_range"), ("二买", "ermai_range")]:
                br = h.get(range_key)
                if br and br.get("low") and br.get("high"):
                    add_calc = calc_add_position(
                        code=h["code"],
                        current_shares=h["shares"],
                        current_cost=h["cost"],
                        current_price=h["price"],
                        cash=_cash_available,
                        buy_range_name=range_name,
                        buy_range_low=br["low"],
                        buy_range_high=br["high"],
                        invalid_boundary=br.get("invalid"),
                    )
                    if add_calc["feasible"]:
                        break
                    else:
                        # 三买不可行且二买有区间, 重置calc继续尝试二买
                        if range_name == "二买":
                            add_calc = None  # 二买也不可行, 不附加
            h["add_calc"] = add_calc
            plan["add_positions"].append(h)

    # ============================================================
    # 4. 资金计算
    # ============================================================
    if account:
        plan["funding"] = {
            "total_asset": account.get("total_asset", 0),
            "cash": account.get("cash", 0),
            "position_ratio": account.get("position_ratio", 0),
            "max_single_stock": round(account.get("total_asset", 0) * 0.35, 2),
            "allow_new": account.get("allow_new", True),
            "stage": account.get("stage", ""),
            "monthly_target": account.get("monthly_target", 0),
            "deviation": account.get("deviation", 0),
        }

    # ============================================================
    # 5. 优先级排序
    # ============================================================
    # 今日必做: 卖出信号 + 清仓
    for h in plan["holdings_analysis"]:
        if h["action"] in ("清仓",):
            plan["priorities"]["today_must"].append({
                "type": "清仓",
                "code": h["code"],
                "name": h["name"],
                "reason": h["sell_reason"],
                "price": h["price"],
                "shares": h["shares"],
                "amount": round(h["price"] * h["shares"], 2),
            })

    # 今日必做: 减仓/止盈
    for h in plan["holdings_analysis"]:
        if h["action"] in ("减仓", "止盈") and len(plan["priorities"]["today_must"]) < 5:
            plan["priorities"]["today_must"].append({
                "type": "减仓/止盈",
                "code": h["code"],
                "name": h["name"],
                "reason": h["action_reason"],
                "profit_pct": h["profit_pct"],
            })

    # 今日必做: 止损考虑 (深套+无信号)
    for h in plan["holdings_analysis"]:
        if h["action"] == "止损考虑" and len(plan["priorities"]["today_must"]) < 8:
            plan["priorities"]["today_must"].append({
                "type": "止损评估",
                "code": h["code"],
                "name": h["name"],
                "reason": h["action_reason"],
                "profit_pct": h["profit_pct"],
            })

    # 今日关注: 买入候选
    for c in plan["buy_candidates"][:5]:
        plan["priorities"]["today_watch"].append({
            "type": "买入关注",
            "code": c["code"],
            "name": c["name"],
            "tier": c["tier"],
            "dl_p": c["dl_p"],
            "price": c["price"],
            "cost_1shou": c["cost_1shou"],
        })

    # 今日关注: 加仓候选
    for h in plan["add_positions"]:
        plan["priorities"]["today_watch"].append({
            "type": "加仓",
            "code": h["code"],
            "name": h["name"],
            "reason": h["action_reason"],
            "profit_pct": h["profit_pct"],
            "cap": h["dynamic_cap"],
        })

    # ============================================================
    # 6. 摘要
    # ============================================================
    total_profit = sum(h.get("profit_amount", 0) for h in plan["holdings_analysis"])
    buy_count = len(plan["buy_candidates"])
    sell_count = len(plan["sell_candidates"])
    add_count = len(plan["add_positions"])

    plan["summary"] = {
        "holdings_count": len(plan["holdings_analysis"]),
        "total_profit": round(total_profit, 2),
        "sell_count": sell_count,
        "buy_count": buy_count,
        "add_count": add_count,
        "must_do_count": len(plan["priorities"]["today_must"]),
        "watch_count": len(plan["priorities"]["today_watch"]),
        "has_clear_signal": any(h["action"] == "清仓" for h in plan["holdings_analysis"]),
        "has_add_signal": add_count > 0,
    }

    return plan


def format_plan(plan, show_all_holdings=True):
    """
    格式化交易计划为可读文本

    参数:
        plan: dict build_trading_plan 的输出
        show_all_holdings: bool 是否展示所有持仓, False仅展示有动作的

    返回: str 格式化文本
    """
    lines = []
    lines.append("=" * 72)
    lines.append(f"  交易计划 — {plan['generated_at']}")
    lines.append("=" * 72)

    s = plan["summary"]

    # 摘要行
    parts = [
        f"持仓{s['holdings_count']}只",
        f"盈亏¥{s['total_profit']:+.2f}",
        f"卖出{s['sell_count']}只",
        f"买入{s['buy_count']}只",
        f"加仓{s['add_count']}只",
    ]
    lines.append(f"  {' | '.join(parts)}")

    # 账户
    if plan["account"] and plan["funding"]:
        a = plan["account"]
        f = plan["funding"]
        lines.append(
            f"  账户: ¥{a.get('total_asset',0):.2f} | "
            f"现金¥{a.get('cash',0):.2f} | "
            f"仓位{f.get('position_ratio',0):.1f}% | "
            f"阶段: {f.get('stage','?')} | "
            f"新建仓: {'允许' if f.get('allow_new') else '禁止'}"
        )

    # 今日必做
    lines.append(f"\n{'─'*72}")
    must = plan["priorities"]["today_must"]
    if must:
        lines.append(f"  🔴 今日必做 ({len(must)}项)")
        for p in must:
            if p["type"] == "清仓":
                amt = p.get("amount", 0)
                lines.append(
                    f"    [{p['type']}] {p['name']}({p['code']}): "
                    f"{p['reason']} | {p['shares']}股 x ¥{p['price']:.2f} = ¥{amt:.0f}"
                )
            else:
                lines.append(
                    f"    [{p['type']}] {p['name']}({p['code']}): {p['reason']}"
                )
    else:
        lines.append("  ✅ 无紧急操作")

    # 今日关注
    watch = plan["priorities"]["today_watch"]
    if watch:
        lines.append(f"\n  🟡 今日关注 ({len(watch)}项)")
        for p in watch:
            if p["type"] == "买入关注":
                label = p.get("sig_label", p.get("label", "ABC买卖区间")).replace("买卖区间", "")
                sig_type = p.get("sig_type", "盘整背驰")
                lines.append(
                    f"    [{p['type']}] {p['name']}({p['code']}): "
                    f"tier={p['tier']} DL_P={p['dl_p']:.2f} "
                    f"{label}/{sig_type} "
                    f"¥{p['price']:.2f} 1手¥{p['cost_1shou']:.0f}"
                )
            else:
                lines.append(
                    f"    [{p['type']}] {p['name']}({p['code']}): {p['reason']}"
                )

    # 持仓明细
    lines.append(f"\n{'─'*72}")
    lines.append("  📋 持仓分析")
    header = f"  {'名称':<10} {'盈亏':>8} {'状态':<8} {'动作':<10} {'tier':<8} {'30minEP':>8} {'上限':>6}"
    lines.append(header)
    lines.append(f"  {'─'*60}")

    for h in plan["holdings_analysis"]:
        if not show_all_holdings and h["action"] in ("持有", "观察"):
            continue
        act_mark = {
            "清仓": "🔴", "减仓": "🟠", "止盈": "🟢",
            "止损考虑": "🔴", "加仓": "🟢", "持有": "⚪", "观察": "⚪",
        }.get(h["action"], "⚪")
        lines.append(
            f"  {act_mark} {h['name']:<8} {h['profit_pct']:>+7.1f}% "
            f"{h['profit_status']:<8} {h['action']:<8} "
            f"{h['tier']:<8} {h['30min_ep_p']:>7.2f} {h['dynamic_cap']:>5.0%}"
        )

    # 买入候选
    if plan["buy_candidates"]:
        lines.append(f"\n{'─'*72}")
        lines.append(f"  🟢 买入候选 ({len(plan['buy_candidates'])}只)")
        lines.append(f"  {'名称':<10} {'tier':<6} {'DL_P':>5} {'买卖区间':<10} {'背驰':<8} {'价格':>8} {'1手':>8} {'需转入':>8}  {'上限':>6}")
        lines.append(f"  {'─'*85}")
        for c in plan["buy_candidates"]:
            xfer = f"¥{c['transfer_amount']:,.0f}" if c.get("need_transfer") else "无需"
            sig_label = c.get("sig_label", c.get("label", ""))
            # 买卖区间缩写: 123买卖区间→123, ABC买卖区间→ABC
            label_short = sig_label.replace("买卖区间", "")
            sig_type = c.get("sig_type", "盘整背驰")
            lines.append(
                f"  {c['name']:<10} {c['tier']:<6} {c['dl_p']:>.2f} "
                f"{label_short:<10} {sig_type:<8} "
                f"¥{c['price']:>6.2f} ¥{c['cost_1shou']:>6,.0f} {xfer:>8} "
                f"{c['position_cap']:>5.0%}"
            )

    # 卖出候选
    if plan["sell_candidates"]:
        lines.append(f"\n{'─'*72}")
        lines.append(f"  🔴 卖出候选 ({len(plan['sell_candidates'])}只)")
        for h in plan["sell_candidates"]:
            lines.append(f"    {h['name']}({h['code']}): {h['action_reason']}")

    # 加仓候选
    if plan["add_positions"]:
        lines.append(f"\n  🟢 加仓候选 ({len(plan['add_positions'])}只)")
        for h in plan["add_positions"]:
            lines.append(
                f"    {h['name']}({h['code']}): {h['action_reason']} | "
                f"当前仓位{h['pos_pct']:.1f}% 上限{h['dynamic_cap']:.0%}"
            )
            # 加仓计算明细 (2026-08-11)
            ac = h.get("add_calc")
            if ac and ac.get("feasible"):
                lines.append(
                    f"      └ 加仓{ac['additional_shares']}股 x ¥{h['price']:.2f} = ¥{ac['cost']:.0f} "
                    f"| 均价{ac['buy_range_name']}{ac['new_avg']:.2f} "
                    f"({ac['buy_range_low']:.2f}~{ac['buy_range_high']:.2f}) "
                    f"| 安全垫{ac['margin_to_failure']:.1f}% "
                    f"| 现金{ac['cash_used_pct']:.0f}%"
                )

    lines.append(f"\n{'='*72}")
    return "\n".join(lines)


def format_plan_short(plan):
    """
    极简版交易计划 (适合Telegram消息)
    """
    lines = []
    lines.append(f"📋 交易计划 {plan['generated_at']}")
    s = plan["summary"]
    lines.append(f"持仓{s['holdings_count']}只 | 盈亏¥{s['total_profit']:+.0f} | "
                 f"卖出{s['sell_count']} | 买入{s['buy_count']} | 加仓{s['add_count']}")

    # 必做
    must = plan["priorities"]["today_must"]
    if must:
        lines.append(f"▸ 必做({len(must)}):")
        for p in must[:3]:
            lines.append(f"  [{p['type']}] {p['name']} {p.get('reason','')[:40]}")

    # 关注
    watch = plan["priorities"]["today_watch"]
    if watch:
        lines.append(f"▸ 关注({len(watch)}):")
        for p in watch[:3]:
            label = p.get("sig_label", p.get("label", "")).replace("买卖区间", "")
            sig_type = p.get("sig_type", "")
            tag = f" {label}/{sig_type}" if label else ""
            lines.append(f"  [{p['type']}] {p['name']}{tag}")

    # 持仓概览
    lines.append("▸ 持仓动作:")
    for h in plan["holdings_analysis"]:
        if h["action"] in ("持有", "观察"):
            continue
        lines.append(f"  {h['action']} {h['name']} {h['profit_pct']:+.1f}%")
        # 加仓计算摘要 (2026-08-11)
        if h["action"] == "加仓":
            ac = h.get("add_calc")
            if ac and ac.get("feasible"):
                lines.append(f"    → 加{ac['additional_shares']}股 ¥{ac['cost']:.0f} "
                             f"均价{ac['buy_range_name']}{ac['new_avg']:.2f} "
                             f"安全垫{ac['margin_to_failure']:.1f}%")

    return "\n".join(lines)


def generate_plan_from_workflow(total_asset=20326.12, cash=7847.12):
    """
    从 daily_workflow 获取数据并生成交易计划 (一站式入口)
    会调用 get_today_holdings + run_full_scan + get_account_summary

    返回: dict build_trading_plan 的输出
    """
    from daily_workflow import get_today_holdings, get_account_summary
    from full_scan import full_scan

    print("[计划] 获取持仓数据...")
    try:
        holdings = get_today_holdings()
        print(f"  -> {len(holdings)}只持仓")
    except Exception as e:
        print(f"  ⚠ 持仓获取失败: {e}")
        holdings = []

    print("[计划] 获取账户信息...")
    try:
        account = get_account_summary()
        print(f"  -> 总资产¥{account.get('total_asset',0):.2f}")
    except Exception as e:
        print(f"  ⚠ 账户获取失败: {e}")
        account = {"total_asset": total_asset, "cash": cash}

    print("[计划] 全市场扫描...")
    try:
        scan_result = full_scan(
            total_asset=account.get("total_asset", total_asset),
            cash=account.get("cash", cash),
            silent=False,
        )
        print(f"  -> {len(scan_result.get('confirmed',[]))}只确认信号")
    except Exception as e:
        print(f"  ⚠ 扫描失败: {e}")
        scan_result = None

    # 先预取持仓股数据
    if holdings:
        print("[计划] 预取持仓股数据...")
        try:
            from concurrent_prefetch import prefetch_holdings
            prefetch_holdings(holdings, levels=["日线", "30min", "5min"], max_workers=15)
        except Exception as e:
            print(f"  ⚠ 预取失败: {e}")

    print("[计划] 生成交易计划...")
    plan = build_trading_plan(
        holdings=holdings,
        scan_result=scan_result,
        account=account,
    )

    return plan


if __name__ == "__main__":
    plan = generate_plan_from_workflow()
    print("\n")
    print(format_plan(plan))