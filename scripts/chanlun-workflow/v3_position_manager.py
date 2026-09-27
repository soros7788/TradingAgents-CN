#!/usr/bin/env python3
"""v3_position_manager.py — 动态仓位资金管理法则 (v3 执行版).

来源: 动态仓位资金管理法则_执行版_v3.xlsx (9 表)
账户阶段: 婴儿期 (总仓 40%, 单股 35%)

三大组件:
  1. cap_table — 单股仓位上限表 (买级 × 浮盈档)
  2. TotalPositionCap — 总仓位上限 (市场状态 × 信号等级) + 回撤/赚钱效应闸门
  3. RiskGates — 前置门控 (浮盈铺垫 / 一买低点距离 / 中阴 NotChasing)

用法:
  from v3_position_manager import V3PositionManager
  pm = V3PositionManager(stage="infant", total_assets=30000.0)
  # 输入候选清单 → 输出每只仓位建议
  result = pm.plan(candidates, market_state="neutral", signal_grade="B")
"""
from __future__ import annotations
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Optional

class MarketState(str, Enum):
    STRONG = "strong"
    NEUTRAL = "neutral"
    WEAK = "weak"

class SignalGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"

class BuyLevel(str, Enum):
    ONE = "一买"
    TWO = "二买"
    THREE = "三买"

@dataclass
class Candidate:
    """单只候选股的最小输入."""
    code: str
    name: str = ""
    gate: str = "PASS"
    buy_level: BuyLevel = BuyLevel.ONE
    current_price: float = 0.0
    entry_price: float = 0.0
    one_buy_low: float = 0.0
    pnl_pct: float = 0.0          # 当前浮盈%, 新仓 = 0
    is_zhongyin: bool = False      # 中阴 / NotChasing
    dl_p: float = 0.0              # DL_P 动量分数
    momentum_30d: float = 0.0

# ============================================================
# 1. cap_table — 单股仓位上限表
# ============================================================
# 行 = 买级, 列 = 浮盈档 (<5% / ≥5% / ≥10%)
# 值 = 单股仓位上限 (账户净值的 %)
CAP_TABLE: dict[BuyLevel, list[float]] = {
    BuyLevel.ONE:   [0.35, 0.35, 0.35],  # 一买始终 35%
    BuyLevel.TWO:   [0.35, 0.50, 0.50],  # 二买: 浮盈 <5% 锁 35%, ≥5% 升 50%
    BuyLevel.THREE: [0.35, 0.50, 0.60],  # 三买: 浮盈 <5% 锁 35%, ≥5% 50%, ≥10% 60%
}

def _pnl_bucket(pnl_pct: float) -> int:
    """浮盈档 → cap_table 列索引: 0=<5%, 1=≥5%, 2=≥10%"""
    if pnl_pct >= 0.10: return 2
    if pnl_pct >= 0.05: return 1
    return 0

def _compute_cap(c: Candidate, applied_level: Optional[BuyLevel] = None) -> tuple[float, str]:
    """计算单股仓位上限 + 门控说明.

    返回 (cap_pct, gates_desc)
    """
    gates_hit = []
    level = applied_level or c.buy_level
    bucket = _pnl_bucket(c.pnl_pct)
    cap = CAP_TABLE[level][bucket]

    # 门控 ① 浮盈铺垫: pnl < 5% → cap 锁 35% (一买本来就 35%)
    if c.pnl_pct < 0.05 and level != BuyLevel.ONE:
        cap = min(cap, 0.35)
        gates_hit.append("浮盈铺垫→锁35%")

    # 门控 ② 一买低点距离: (close - one_buy_low)/one_buy_low < 3% → 锁 35%
    if c.one_buy_low > 0 and c.current_price > 0:
        dist = (c.current_price - c.one_buy_low) / c.one_buy_low
        if dist < 0.03:
            cap = min(cap, 0.35)
            gates_hit.append(f"一买低点距离{dist:.1%}<3%→锁35%")

    # 门控 ③ 中阴 / NotChasing: cap × 0.5
    if c.is_zhongyin:
        cap *= 0.5
        gates_hit.append("中阴→cap×0.5")

    return cap, ";".join(gates_hit) if gates_hit else "OK"

# ============================================================
# 2. TotalPositionCap — 总仓位上限
# ============================================================
# 单市场状态制: 市场状态 × 信号等级 → 总仓上限
_TOTAL_CAP: dict[MarketState, dict[SignalGrade, float]] = {
    MarketState.STRONG:  {SignalGrade.A: 0.70, SignalGrade.B: 0.60, SignalGrade.C: 0.40},
    MarketState.NEUTRAL: {SignalGrade.A: 0.50, SignalGrade.B: 0.40, SignalGrade.C: 0.25},
    MarketState.WEAK:    {SignalGrade.A: 0.30, SignalGrade.B: 0.20, SignalGrade.C: 0.10},
}

# 回撤闸门
_DRAWDOWN_GATES = [
    (-0.15, 0.10, "回撤≤-15% → 禁新买, 总仓≤10%"),
    (-0.10, 0.30, "回撤≤-10% → 降总仓到30%"),
]

# 赚钱效应闸门 (优先于亏钱效应)
_PROFIT_EFFECT = [
    (4, 0.10, "赚钱效应≥4 → 10%"),
    (3, 0.30, "赚钱效应≥3 → 30%"),
]

# 账户阶段总仓硬顶
STAGE_HARD_CAP = {
    "infant": 0.40,    # 婴儿期 40%
    "toddler": 0.55,   # 幼儿期 55%
    "teen": 0.70,      # 少年期 70%
}

# 单股上限 (按总资产分档)
SINGLE_ASSET_CAP = [
    (30000, 0.35),    # <3万 → 35%
    (50000, 0.30),    # <5万 → 30%
    (100000, 0.25),   # <10万 → 25%
    (float("inf"), 0.20),  # ≥10万 → 20%
]

def compute_total_cap(
    market_state: MarketState,
    signal_grade: SignalGrade,
    drawdown: float = 0.0,      # 当前回撤, 负值
    profit_effect: int = 0,    # 赚钱效应 0-5
    loss_effect: int = 0,       # 亏钱效应 0-6 (优先级高)
    stage: str = "infant",
) -> tuple[float, str]:
    """计算总仓位上限 + 触发的闸门说明."""
    gates = []

    # 基础: 市场 × 信号
    cap = _TOTAL_CAP[market_state][signal_grade]

    # 亏钱效应 ≥3: 优先
    if loss_effect >= 4:
        cap = min(cap, 0.0)
        gates.append(f"亏钱效应≥4→禁新仓")
    elif loss_effect >= 3:
        cap = min(cap, 0.30)
        gates.append(f"亏钱效应≥3→30%")

    # 回撤闸门
    for dd_thr, dd_cap, dd_desc in _DRAWDOWN_GATES:
        if drawdown <= dd_thr:
            cap = min(cap, dd_cap)
            gates.append(dd_desc)

    # 赚钱效应
    if profit_effect >= 4:
        cap = max(cap, 0.10)  # 最低保 10%? 不对, 赚钱效应≥4 闸门写的是 10% 上限
    for pe_thr, pe_cap, pe_desc in _PROFIT_EFFECT:
        if profit_effect >= pe_thr:
            cap = min(cap, pe_cap)
            gates.append(pe_desc)
            break

    # 阶段硬顶
    hard = STAGE_HARD_CAP.get(stage, 0.40)
    cap = min(cap, hard)
    gates.append(f"阶段{stage}硬顶{hard:.0%}")

    # 全局硬顶 70%
    cap = min(cap, 0.70)

    return cap, ";".join(gates) if gates else "OK"

def compute_single_cap(total_assets: float) -> float:
    """单股上限 (账户资产分档)."""
    for threshold, cap in SINGLE_ASSET_CAP:
        if total_assets < threshold:
            return cap
    return 0.20

# ============================================================
# 3. V3PositionManager — 统一入口
# ============================================================
@dataclass
class PositionPlan:
    """单只候选的仓位建议."""
    code: str
    name: str = ""
    gate: str = ""
    buy_level: str = ""
    dl_p: float = 0.0
    pnl_pct: float = 0.0
    single_cap_pct: float = 0.0    # 单股仓位上限 (%)
    max_amount: float = 0.0        # 最大可买金额
    gates: str = ""
    risk_flags: list[str] = field(default_factory=list)

@dataclass
class PortfolioPlan:
    portfolio_candidates: list[PositionPlan]
    total_cap_pct: float
    total_cap_amount: float
    used_amount: float
    remaining_amount: float
    gates: str = ""
    summary: str = ""

class V3PositionManager:
    """v3 仓位管理器."""

    def __init__(self, stage: str = "infant", total_assets: float = 30000.0):
        self.stage = stage
        self.total_assets = total_assets
        self.single_cap_pct = compute_single_cap(total_assets)

    def plan(
        self,
        candidates: list[Candidate],
        market_state: MarketState = MarketState.NEUTRAL,
        signal_grade: SignalGrade = SignalGrade.B,
        drawdown: float = 0.0,
        profit_effect: int = 0,
        loss_effect: int = 0,
        max_positions: int = 5,
    ) -> PortfolioPlan:
        """给候选清单排仓位."""
        # 总仓
        total_cap_pct, total_gates = compute_total_cap(
            market_state, signal_grade, drawdown, profit_effect, loss_effect, self.stage)
        total_cap_amount = self.total_assets * total_cap_pct

        # 过滤 + 排序: PASS 优先, 按 dl_p 降序
        pass_only = [c for c in candidates if c.gate in ("PASS", "BLOCKED")]
        pass_only.sort(key=lambda c: c.dl_p, reverse=True)

        # 单股上限约束 + 分配
        used = 0.0
        plans = []
        for c in pass_only[:max_positions]:
            raw_cap, gates = _compute_cap(c)
            # 单股硬顶 (资产分档)
            cap = min(raw_cap, self.single_cap_pct)

            # 剩余资金约束
            remaining = total_cap_amount - used
            max_amount = min(self.total_assets * cap, remaining)

            risk_flags = []
            if c.dl_p < 0.6: risk_flags.append("DL_P<0.6")
            if c.is_zhongyin: risk_flags.append("中阴")
            if max_amount < 100: risk_flags.append("资金不足")

            plan = PositionPlan(
                code=c.code, name=c.name, gate=c.gate,
                buy_level=c.buy_level.value,
                dl_p=c.dl_p, pnl_pct=c.pnl_pct,
                single_cap_pct=cap, max_amount=round(max_amount, 0),
                gates=gates, risk_flags=risk_flags,
            )
            plans.append(plan)
            used += max_amount

        summary = (
            f"v3 执行: 市场={market_state.value} 信号={signal_grade.value} "
            f"阶段={self.stage} 总资产=¥{self.total_assets:,.0f}\n"
            f"总仓上限={total_cap_pct:.0%} (¥{total_cap_amount:,.0f}) "
            f"单股上限={self.single_cap_pct:.0%}\n"
            f"已用=¥{used:,.0f}  剩余=¥{total_cap_amount-used:,.0f}\n"
            f"候选 {len(candidates)} → 入选 {len(plans)}"
        )

        return PortfolioPlan(
            portfolio_candidates=plans,
            total_cap_pct=total_cap_pct,
            total_cap_amount=round(total_cap_amount, 0),
            used_amount=round(used, 0),
            remaining_amount=round(total_cap_amount - used, 0),
            gates=total_gates,
            summary=summary,
        )

    def plan_to_dict(self, plan: PortfolioPlan) -> dict:
        """转 JSON 可序列化."""
        return {
            "total": {
                "cap_pct": plan.total_cap_pct,
                "cap_amount": plan.total_cap_amount,
                "used_amount": plan.used_amount,
                "remaining_amount": plan.remaining_amount,
                "gates": plan.gates,
            },
            "positions": [asdict(p) for p in plan.portfolio_candidates],
            "summary": plan.summary,
        }

# ============ CLI ============
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", help="chan-merge 输出 JSON 或 scan_ledger.jsonl")
    ap.add_argument("--ssh-host", default=None)
    ap.add_argument("--ssh-key", default=None)
    ap.add_argument("--market", default="neutral", choices=["strong","neutral","weak"])
    ap.add_argument("--signal", default="B", choices=["A","B","C"])
    ap.add_argument("--assets", type=float, default=30000.0)
    ap.add_argument("--drawdown", type=float, default=0.0)
    ap.add_argument("--stage", default="infant")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # 读候选 (stdin 或 --ledger)
    from chan_merge import read_ledger, dedupe, classify
    records = read_ledger(args.ledger or "~/chan_logs/scan_ledger.jsonl", args.ssh_host, args.ssh_key)
    deduped = dedupe(records)
    passes = [r for r in deduped.values() if r.get("gate") == "PASS"]

    candidates = []
    for r in passes:
        candidates.append(Candidate(
            code=r["code"],
            gate=r.get("gate","PASS"),
            dl_p=float(r.get("dl_p", 0.0)),
        ))

    pm = V3PositionManager(stage=args.stage, total_assets=args.assets)
    plan = pm.plan(
        candidates,
        market_state=MarketState(args.market),
        signal_grade=SignalGrade(args.signal),
        drawdown=args.drawdown,
    )

    out = pm.plan_to_dict(plan)
    if args.out:
        with open(args.out, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"📄 v3 仓位 → {args.out}")
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n{plan.summary}")

if __name__ == "__main__":
    main()

