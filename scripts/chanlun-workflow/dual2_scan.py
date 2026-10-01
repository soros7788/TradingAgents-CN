#!/usr/bin/env python3
"""dual_scan — 全市场扫描强制过双引擎 (递归A定方向 + 区间套B定时机)

调用层 wiring：零改动 sealed modules (tradingagents/chan/*, recursive_core, interval_engine)。

两层架构:
  Stage 1: full_scan (workflow 层背驰裁定) → 候选池 = confirmed 全部 + near 前 N (默认30)
  Stage 2: 双引擎门禁 — 每只候选取 CanonicalMarketSnapshot → EvidenceBundle:
      - 递归引擎 A: TREND 优先、SEGMENT 兜底 → direction 定方向
          BULLISH → PASS   BEARISH → BLOCKED   NEUTRAL → NEUTRAL
      - 区间套引擎 B: confirmed bullish interval 证据数 → 定时机强度
  最终报告: 只有过门禁的候选才算可做多候选。

用法:
  python dual_scan.py                                # 全市场两段式
  python dual_scan.py --codes 002141,600006,603256   # 指定标的(跳过 Stage1)
  python dual_scan.py --near 20                      # Stage2 验证 near 前20
"""
import argparse
import json
import os
import signal
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", ".."))
for p in (_SCRIPT_DIR, _PROJECT_ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)

# === 消费层 shim 注入 (dual_scan 启动期一次) ===
# 将 ak.stock_zh_a_minute 重定向到共享盘 K 线缓存; 零改 sealed, 未覆盖回退 live。
from tradingagents.utils.kline_cache_shim import install_kline_cache_shim
install_kline_cache_shim()

from full_scan import full_scan, calc_funding, scan_one  # noqa: E402

from tradingagents.chan.canonical_snapshot_acquisition import (  # noqa: E402
    acquire_canonical_market_snapshot,
)
from tradingagents.chan.canonical_evidence_orchestration import (  # noqa: E402
    build_evidence_bundle_from_snapshot,
)
from tradingagents.chan.data_alignment import DataAlignmentResult  # noqa: E402
from tradingagents.chan.evidence_schema import (  # noqa: E402
    EvidenceDirection,
    EvidenceStatus,
    EvidenceType,
)
from tradingagents.chan.level_alignment import level_alignment_check  # noqa: E402

LOGDIR = Path.home() / "chan_logs"
SH_TZ = ZoneInfo("Asia/Shanghai")
GATE = {EvidenceDirection.BULLISH: "PASS", EvidenceDirection.BEARISH: "BLOCKED",
        EvidenceDirection.NEUTRAL: "NEUTRAL"}


RECURSIVE_BASE_TIMEFRAME = "5m"  # 递归 R0 基棒，对齐 debate_context.py:207 默认值


def _base_tf_shortfall(snapshot) -> bool:
    """只按递归基棒判定 history shortfall。

    snapshot.history_shortfall 是 Dict[str, HistoryShortfall]，键为 timeframe，
    只在实际根数 < 契约根数的档位挂一条。AKShare 1m 硬上限 ~1970 < 契约 2000，
    每只标的常驻一条 1m shortfall；但 1m 非递归基棒（基棒=5m，契约 1000，
    可取 5001，达标），该噪音不应污染 gate。
    """
    sf = getattr(snapshot, "history_shortfall", None) or {}
    return RECURSIVE_BASE_TIMEFRAME in sf


def unverified_alignment() -> DataAlignmentResult:
    """A4-R10B sealed convention: single-source unverified alignment."""
    return DataAlignmentResult(
        status="unverified", symbol_match=False, date_match=False,
        source_match=False, adjustment_match=False, timezone_match=False,
        details={"comparison_performed": False, "reason": "single_source"},
    )


def recursive_direction(bundle) -> EvidenceDirection:
    """递归引擎 A 定方向 — 缠原文三级联立正解。

    按递归层级分层取 TREND (= Movement 走势类型) direction:
      - R2 (大级别) 走势方向
      - R1 (本级别) 走势方向
      - R0 (次级别) 走势方向
    三级同方向 → 有效信号。两层同方向 → 较弱信号。否则取最顶层可用的。
    """
    rec = bundle.recursive_evidence or []

    def _confirmed_trends_at(level_str):
        return [e for e in rec
                if e.type == EvidenceType.TREND
                and getattr(e, 'level', None) == level_str
                and e.status == EvidenceStatus.CONFIRMED]

    r2 = _confirmed_trends_at("R2")   # 大级别走势
    r1 = _confirmed_trends_at("R1")   # 本级别走势
    r0 = _confirmed_trends_at("R0")   # 次级别走势

    # 三级联立: 三层都有 confirmed TREND 且方向一致
    if r2 and r1 and r0:
        d2, d1, d0 = r2[-1].direction, r1[-1].direction, r0[-1].direction
        if d2 == d1 == d0 and d2 != EvidenceDirection.NEUTRAL:
            return d2

    # 两级联立: R1+R0 方向一致
    if r1 and r0:
        d1, d0 = r1[-1].direction, r0[-1].direction
        if d1 == d0 and d1 != EvidenceDirection.NEUTRAL:
            return d1

    if r2 and r1:
        d2, d1 = r2[-1].direction, r1[-1].direction
        if d2 == d1 and d2 != EvidenceDirection.NEUTRAL:
            return d2

    # 兜底: 取最高可用层级的 direction
    for trends in (r2, r1, r0):
        if trends:
            return trends[-1].direction

    return EvidenceDirection.NEUTRAL

def recursive_evidence_summary(bundle) -> dict:
    """递归引擎 A 证据展开 — 按递归层级 (R0/R1/R2) 分层统计。

    缠原文正解: 每一级递归都有 bis/segments/zhongshus/movements 四类结构,
    映射为 EvidenceType.BI / SEGMENT / ZHONGSHU / TREND (DERIVED from Movement)。
    三级联立 = R2/R1/R0 每级各有自己的 TREND direction。
    """
    from tradingagents.chan.evidence_schema import EvidenceType, EvidenceDirection, EvidenceStatus
    rec = bundle.recursive_evidence or []

    LEVELS = ["R0", "R1", "R2"]

    # 按 level 分层
    by_level = {lv: [e for e in rec if getattr(e, 'level', None) == lv] for lv in LEVELS}
    by_level["other"] = [e for e in rec if getattr(e, 'level', None) not in LEVELS]

    def _cnt(lv, etype, edir, status=None):
        pool = by_level.get(lv, []) if lv != "ALL" else rec
        return sum(1 for e in pool
                   if e.type == etype and e.direction == edir
                   and (status is None or e.status == status))

    # ── 全局汇总 (ALL levels 混合, 保留旧接口兼容) ──
    tb_c = _cnt("ALL", EvidenceType.TREND, EvidenceDirection.BULLISH, EvidenceStatus.CONFIRMED)
    te_c = _cnt("ALL", EvidenceType.TREND, EvidenceDirection.BEARISH, EvidenceStatus.CONFIRMED)
    sb_c = _cnt("ALL", EvidenceType.SEGMENT, EvidenceDirection.BULLISH, EvidenceStatus.CONFIRMED)
    se_c = _cnt("ALL", EvidenceType.SEGMENT, EvidenceDirection.BEARISH, EvidenceStatus.CONFIRMED)
    bb_c = _cnt("ALL", EvidenceType.BI, EvidenceDirection.BULLISH, EvidenceStatus.CONFIRMED)
    be_c = _cnt("ALL", EvidenceType.BI, EvidenceDirection.BEARISH, EvidenceStatus.CONFIRMED)

    # ── 每级分层统计 ──
    tier_stats = {}
    for lv in LEVELS:
        pool = by_level[lv]
        trend_confirmed = [e for e in pool
                           if e.type == EvidenceType.TREND
                           and e.status == EvidenceStatus.CONFIRMED]
        seg_confirmed = [e for e in pool
                         if e.type == EvidenceType.SEGMENT
                         and e.status == EvidenceStatus.CONFIRMED]
        bi_confirmed = [e for e in pool
                        if e.type == EvidenceType.BI
                        and e.status == EvidenceStatus.CONFIRMED]
        zs_confirmed = [e for e in pool
                        if e.type == EvidenceType.ZHONGSHU
                        and e.status == EvidenceStatus.CONFIRMED]

        trend_dir = trend_confirmed[-1].direction.value if trend_confirmed else "none"
        tier_stats[lv] = {
            "trend_dir": trend_dir,
            "trend_confirmed": len(trend_confirmed),
            "segment_confirmed": len(seg_confirmed),
            "bi_confirmed": len(bi_confirmed),
            "zhongshu_confirmed": len(zs_confirmed),
            "trend_bullish": _cnt(lv, EvidenceType.TREND, EvidenceDirection.BULLISH, EvidenceStatus.CONFIRMED),
            "trend_bearish": _cnt(lv, EvidenceType.TREND, EvidenceDirection.BEARISH, EvidenceStatus.CONFIRMED),
        }

    # ── 三级联立状态 ──
    r2_d = tier_stats["R2"]["trend_dir"]
    r1_d = tier_stats["R1"]["trend_dir"]
    r0_d = tier_stats["R0"]["trend_dir"]
    three_way = (r2_d == r1_d == r0_d) and r2_d in ("bullish", "bearish")
    two_way_r10 = (r1_d == r0_d) and r1_d in ("bullish", "bearish")
    two_way_r21 = (r2_d == r1_d) and r2_d in ("bullish", "bearish")

    # ── strength score ──
    def _strength_sum(lv, etype, edir):
        pool = by_level.get(lv, []) if lv != "ALL" else rec
        return sum(e.strength or 0.0 for e in pool
                   if e.type == etype and e.direction == edir
                   and e.status == EvidenceStatus.CONFIRMED)

    t_score_all = round(
        _strength_sum("ALL", EvidenceType.TREND, EvidenceDirection.BULLISH)
        - _strength_sum("ALL", EvidenceType.TREND, EvidenceDirection.BEARISH), 3)
    s_score_all = round(
        _strength_sum("ALL", EvidenceType.SEGMENT, EvidenceDirection.BULLISH)
        - _strength_sum("ALL", EvidenceType.SEGMENT, EvidenceDirection.BEARISH), 3)

    tb_f = _cnt("ALL", EvidenceType.TREND, EvidenceDirection.BULLISH, EvidenceStatus.FORMING)
    te_f = _cnt("ALL", EvidenceType.TREND, EvidenceDirection.BEARISH, EvidenceStatus.FORMING)

    confirmed_total = sum(1 for e in rec if e.status == EvidenceStatus.CONFIRMED)
    forming_total = sum(1 for e in rec if e.status == EvidenceStatus.FORMING)

    return {
        # ── 兼容旧字段 (ALL levels 混合) ──
        "trend_bullish": tb_c, "trend_bearish": te_c,
        "segment_bullish": sb_c, "segment_bearish": se_c,
        "bi_bullish": bb_c, "bi_bearish": be_c,
        "trend_bullish_form": tb_f, "trend_bearish_form": te_f,
        "trend_score": t_score_all, "segment_score": s_score_all,
        "evidence_total": len(rec),
        "confirmed": confirmed_total, "forming": forming_total,
        # ── 新增: 三级联立核心数据 ──
        "tier_R0": tier_stats["R0"],
        "tier_R1": tier_stats["R1"],
        "tier_R2": tier_stats["R2"],
        "r2_dir": r2_d, "r1_dir": r1_d, "r0_dir": r0_d,
        "three_way_unison": three_way,
        "two_way_r10": two_way_r10,
        "two_way_r21": two_way_r21,
    }

def interval_timing(bundle) -> dict:
    """区间套引擎 B 定时机: 各方向 confirmed 证据统计。"""
    iv = bundle.interval_evidence or []
    return {
        "bullish_confirmed": sum(
            1 for e in iv if e.direction == EvidenceDirection.BULLISH
            and e.status == EvidenceStatus.CONFIRMED),
        "bearish_confirmed": sum(
            1 for e in iv if e.direction == EvidenceDirection.BEARISH
            and e.status == EvidenceStatus.CONFIRMED),
        "total": len(iv),
    }


def _snapshot_last_close(snapshot):
    """从 canonical snapshot 取递归基棒(5m)最后一根收盘价作为 market price。

    Stage1 被跳过(--codes/--codes-file)时 stage1_map 为空, price/ratio/dlp 全为 None。
    这里从 snapshot.bars 直接取价, 零额外网络开销(快照已含 K 线)。
    """
    bars = getattr(snapshot, "bars", None) or {}
    for tf in (RECURSIVE_BASE_TIMEFRAME, "30m", "1m"):
        arr = bars.get(tf)
        if arr:
            last = arr[-1]
            close = getattr(last, "close", None)
            if close:
                return float(close)
    return None


def run_dual_engine(code: str, as_of: datetime, price: float = None) -> dict:
    snapshot = acquire_canonical_market_snapshot(symbol=code, as_of=as_of)
    bundle = build_evidence_bundle_from_snapshot(
        snapshot=snapshot,
        alignment_result=unverified_alignment(),
        as_of=as_of,
        generated_at=datetime.now(SH_TZ),
    )
    direction = recursive_direction(bundle)
    timing = interval_timing(bundle)
    rec_sum = recursive_evidence_summary(bundle)

    # level alignment check (2026-09-12)
    alignment = level_alignment_check(bundle, tolerance=0)
    gate_modifier = alignment["overall"]["gate_modifier"]

    # Gate verdict: direction x alignment modifier
    if gate_modifier == "BLOCKED":
        final_gate = "BLOCKED"
    elif direction == EvidenceDirection.BEARISH:
        final_gate = "BLOCKED"
    elif direction == EvidenceDirection.BULLISH:
        final_gate = "PASS" if gate_modifier in ("PASS", "WEAK_PASS") else gate_modifier
    else:
        final_gate = gate_modifier if gate_modifier == "PASS" else "NEUTRAL"

    return {
        "code": code,
        "gate": final_gate,
        "price": price if price is not None else _snapshot_last_close(snapshot),
        "recursive_direction": direction.value,
        "recursive_summary": rec_sum,
        "interval_timing": timing,
        "level_alignment": alignment["per_level"],
        "alignment_overall": alignment["overall"],
        "alignment": bundle.data_alignment,
        "history_shortfall": _base_tf_shortfall(snapshot),
    }



LEDGER_PATH = os.environ.get("MITM_LEDGER", "~/chan_logs/scan_ledger.jsonl")


SCAN_ONE_TIMEOUT = int(os.environ.get("SCAN_ONE_TIMEOUT", "30"))


class _ScanTimeout(Exception):
    pass


def _scan_one_with_timeout(code, name, price, timeout=None):
    """给 scan_one 加超时保护 (2026-09-13)。

    背景: scan_one -> analyze_beichi 是同步网络调用, 个别标的会卡死数分钟,
    导致整轮扫描停滞、被 watchdog 误判重启 (002475 卡 316s 即此因)。
    用 SIGALRM 中断; 超时返回 None, 让主流程跳过 DL_P 补算继续。
    注意: 仅主线程可设 SIGALRM; 批量模式是单线程主循环, 安全。
    """
    t = SCAN_ONE_TIMEOUT if timeout is None else timeout
    if t <= 0:
        return scan_one(code, name, price)

    def _handler(signum, frame):
        raise _ScanTimeout(f"scan_one exceeded {t}s")

    old = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(t)
    try:
        return scan_one(code, name, price)
    except _ScanTimeout:
        print(f"  [dlp] TIMEOUT {code} >{t}s, 跳过补算", flush=True)
        return None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def _build_jev_prompt(row: dict) -> str:
    """把 PASS 标的的双引擎判据汇总成 JEV 可读的英文描述。

    JEV 是纯文本输入模型, 不接受 K 线。这里把递归A(方向/趋势/证据计数)
    + 区间套B(多空确认) + 分层方向 转成一段紧凑的评估文本。
    """
    rs = row.get("recursive_summary", {}) or {}
    timing = row.get("interval_timing", {}) or {}
    align = row.get("alignment_overall", {}) or {}

    t = [
        f"symbol {row.get('code')} ({row.get('name') or 'unknown'})",
        f"A-engine direction {row.get('recursive_direction')}",
        f"confirmed bullish trends {rs.get('trend_bullish')}, bearish {rs.get('trend_bearish')}",
        f"confirmed bullish segments {rs.get('segment_bullish')}, bearish {rs.get('segment_bearish')}",
        f"evidence total {rs.get('evidence_total')} (confirmed {rs.get('confirmed')}, forming {rs.get('forming')})",
        f"unison R2/R1/R0 {rs.get('r2_dir')}/{rs.get('r1_dir')}/{rs.get('r0_dir')}, three_way {rs.get('three_way_unison')}",
        f"B-engine bullish confirmed {timing.get('bullish_confirmed')}, bearish {timing.get('bearish_confirmed')}, interval total {timing.get('total')}",
        f"alignment gate_modifier {align.get('gate_modifier')}, alignment ok {align.get('aligned')}",
    ]
    return ". ".join(t)


def jev_secondary_gate(row: dict, key: str = None, threshold: float = 0.55) -> dict:
    """JEV 二次门禁 (PASS 后追加): 用 JEV 对双引擎已判 PASS 的标的再做一次评估。

    2026-09-22: JEV 系统一模型, 纯文本。将行内递归/区间套判据喂给 JEV 的
    Noul(Yes/No) 接口判断"该标的是否构成值得纳入的看多机会"。

    默认关闭 (需环境变量 JEV_GATE=1 或调用方显式启用)。
    返回 dict: {prob, verdict, error}。verdict: 'PASS'/'FAIL'/'ERROR'。
    threshold 未命中返回 FAIL; 调用异常返回 ERROR (不阻断主流程)。
    """
    try:
        import os as _os
        # key 来源: 显式参数 > 环境变量 > ~/.typesafe_key 文件
        # (.bashrc 只在交互 shell 生效, 非交互启动需显式读文件, 与项目约定一致)
        _key = key or _os.environ.get("TYPESAFE_API_KEY")
        if not _key:
            try:
                _kf = _os.path.expanduser("~/.typesafe_key")
                import builtins
                with builtins.open(_kf, "r", encoding="utf-8") as _fh:
                    _key = _fh.read().strip()
            except Exception:
                _key = None
        if not _key:
            return {"prob": None, "verdict": "ERROR", "error": "TYPESAFE_API_KEY not set"}
        from typesafe_sdk import Noul
        from typesafe_sdk import TypeSafeClient

        base = _os.environ.get("JEV_BASE_URL", "https://api.typesafe.ai")
        transport = None
        _px = _os.environ.get("JEV_PROXY") or _os.environ.get("HTTPS_PROXY")
        if _px:
            try:
                import httpx2 as _hh
                transport = _hh.HTTPTransport(proxy=_px, verify=True)
            except Exception:
                transport = None

        client = TypeSafeClient(
            api_key=_key,
            base_url=base, transport=transport, timeout=_os.environ.get("JEV_TIMEOUT", 30.0) or 30.0,
        )
        prompt = _build_jev_prompt(row)
        import time as _time
        _t0 = _time.time()
        resp = client.system_one(
            state=prompt,
            questions={"is_worthlong": Noul(
                instructions="Given the A-direction, B-timing and evidence counts, is this stock a worthwhile long candidate to include?")},
        )
        prob = float(resp.answers["is_worthlong"].noul)
        verdict = "PASS" if prob >= threshold else "FAIL"
        # 2026-10-01: 返回判分上下文供审计。NoulAnswer schema 实锤只有 noul:float,
        # 无文字评语字段,故评语=输入 prompt 全文 + 阈值 + 耗时, verdict 可复现。
        return {"prob": prob, "verdict": verdict, "error": None,
                "prompt": prompt, "threshold": threshold,
                "latency_ms": round((_time.time() - _t0) * 1000, 1)}
    except Exception as e:  # noqa: BLE001 — JEV 失败不阻断主流程
        return {"prob": None, "verdict": "ERROR", "error": f"{type(e).__name__}: {e}"}


def _append_ledger_row(ledger_path: str, row: dict) -> None:
    """把 Stage2 行写入账本 (jsonl, append-only)。行内带完整信号字段。

    2026-09-13: 打通 DL_P 链路。原先 --codes-file 批量模式不写账本,
    导致 chan_merge -> plan 生成时 price/ratio/dlp 全空。
    """
    if not ledger_path:
        return
    def _native(v):
        # numpy/pandas 标量 -> python 原生, 保 json 可序列化
        if v is None:
            return None
        if isinstance(v, (bool, int, float, str)):
            try:
                if isinstance(v, float):
                    import math as _m
                    if _m.isnan(v) or _m.isinf(v):
                        return None
            except Exception:
                pass
            return v
        try:
            import numpy as _np
            if isinstance(v, _np.generic):
                return v.item()
        except Exception:
            pass
        return str(v)

    entry = {
        "code": row.get("code"),
        "gate": row.get("gate"),
        "vm": row.get("vm", ""),
        "ts": datetime.now(SH_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "price": _native(row.get("price")),
        "ratio": _native(row.get("ratio")),
        "dlp": _native(row.get("dlp")),
        "valid": _native(row.get("valid")),
        "confirmed": _native(row.get("confirmed")),
        "near": _native(row.get("near")),
        "name": row.get("name", ""),
        # F1 (2026-09-23 WORKBUDDY): JEV 二次门禁判分落盘, 使 JEV 判了什么可审计.
        # row["jev"] = {prob, verdict, error} (见 jev_secondary_gate). 无 JEV 时为 None.
        "jev_prob": _native((row.get("jev") or {}).get("prob")),
        "jev_verdict": (row.get("jev") or {}).get("verdict"),
        # 2026-10-01: JEV 判分上下文落盘, verdict 可审计可复现 (阈值校准的数据基础)
        "jev_prompt": (row.get("jev") or {}).get("prompt"),
        "jev_threshold": _native((row.get("jev") or {}).get("threshold")),
        "jev_latency_ms": _native((row.get("jev") or {}).get("latency_ms")),
        # H2a (2026-09-24): 透传 error 键 — ERROR 标带真实原因, 消除 JEV v9 读到的 error=None 空洞假象
        # (此前 _append_ledger_row entry 未写 error 字段, 导致数据质量/异常原因丢失)
        "error": row.get("error"),
    }
    try:
        p = os.path.expanduser(ledger_path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001 — 账本写失败不阻断扫描
        print(f"  [ledger] WARN write failed for {row.get('code')}: {e}", flush=True)


def _process_one(code, base, is_confirmed, as_of, no_beichi, vm_tag):
    """F2 (2026-09-24): Stage2 单只处理单元, 供 ProcessPoolExecutor 并发调用。

    包含原串行循环体内的全部逻辑: run_dual_engine -> PASS补算 -> JEV二次门禁,
    但**不写账本/不 sleep/不 print 进度**(这些由主进程串行负责, 保持账本原子性)。
    注意: 子进程是独立进程, 各自有主线程, _scan_one_with_timeout 内的 SIGALRM 可用。
    依赖 fork 启动 (Linux 默认), 子进程继承已导入的模块与 shim 状态。
    最外层 try 保证单只崩溃(含非预期异常)降级为 ERROR 而非炸掉整个进程池。
    """
    try:
        row = {"code": code, "name": base.get("name", ""), "price": base.get("price"),
               "ratio": base.get("ratio"), "dlp": base.get("dlp"),
               "stage1_confirmed": is_confirmed}
        try:
            verdict = run_dual_engine(code, as_of, price=row.get("price"))
            row.update(verdict)
        except Exception as e:  # noqa: BLE001 — 单标的失败不阻断
            row.update({"gate": "ERROR", "error": f"{type(e).__name__}: {e}"})

        # ── 补算 DL_P/ratio（P1-5 2026-10-01 扩展）──
        # meet 调用恒用 --codes（跳过 Stage1），非 PASS 行的 dlp/ratio 原恒为 None，
        # 转折池（BLOCKED+dlp）被迫依赖外部 vm_rescan_dlp.py 回填。此处扩展到全部
        # 非 ERROR 行，dual2 自给自足；no_beichi 时仍跳过。
        if (not no_beichi) and row.get("gate") not in (None, "ERROR") \
                and row.get("ratio") is None and row.get("price"):
            try:
                sig = _scan_one_with_timeout(code, row.get("name", ""), row["price"])
                if sig:
                    row["ratio"] = sig.get("ratio")
                    row["dlp"] = sig.get("dlp")
                    row["valid"] = sig.get("valid")
                    row["confirmed"] = sig.get("confirmed")
                    row["near"] = sig.get("near")
                    row["score"] = sig.get("score")
                    row["slp"] = sig.get("slp")
                    row["slp_score"] = sig.get("slp_score")
                    row["slp_valid"] = sig.get("slp_valid")
                    row["slp_source"] = "scan_one_backfill"
                    row["dlp_source"] = "scan_one_backfill"
            except Exception as e:  # noqa: BLE001 — 补算失败不阻断主流程
                row["dlp_error"] = f"{type(e).__name__}: {e}"

        # ── JEV 二次门禁 ──
        _jev_env = os.environ.get("JEV_GATE", "").strip()
        _jev_on = _jev_env in ("1", "on", "true", "TRUE")
        if not _jev_env:
            try:
                _jg = Path.home() / ".jev_gate"
                if _jg.exists():
                    _jev_on = _jg.read_text(encoding="utf-8").strip().lower() in ("1", "on", "true")
            except Exception:
                _jev_on = False
        if _jev_on and row.get("gate") == "PASS":
            _j = jev_secondary_gate(row)
            row["jev"] = _j
            if _j.get("verdict") == "FAIL":
                row["gate"] = "PASS_JEVN"
                row["jev_gate"] = "FAIL"
            elif _j.get("verdict") == "ERROR":
                row["jev_gate"] = "ERROR"
            else:
                row["jev_gate"] = "PASS"

        row["vm"] = vm_tag
        return row
    except Exception as e:  # noqa: BLE001 — worker 兜底, 单只崩溃不炸池
        return {"code": code, "gate": "ERROR",
                "error": f"worker:{type(e).__name__}: {e}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--codes-file", default="", help="codes 文件, 每行一个")
    ap.add_argument("--codes", default="", help="逗号分隔指定标的(跳过 Stage1)")
    ap.add_argument("--near", type=int, default=30, help="Stage2 验证 near 前 N (默认30)")
    ap.add_argument("--sleep", type=float, default=1.0, help="标的间隔秒数")
    ap.add_argument("--no-beichi", action="store_true",
                    help="跳过 PASS 标的的 DL_P/ratio 补算(回退旧行为)")
    ap.add_argument("--ledger", default=None,
                    help="账本 jsonl 路径 (默认 $MITM_LEDGER 或 ~/chan_logs/scan_ledger.jsonl)")
    ap.add_argument("--vm", default=None, help="VM 标记 (A/B), 写入账本用")
    args = ap.parse_args()
    if args.codes_file:
        with open(args.codes_file) as _f:
            lines = [l.strip() for l in _f if l.strip()]
        args.codes = ",".join(lines)
        print(f"[codes-file] loaded {len(lines)} codes")


    as_of = datetime.now(SH_TZ)
    ts = as_of.strftime("%Y%m%d_%H%M%S")
    report = {"ts": as_of.strftime("%Y-%m-%d %H:%M:%S %Z"), "stage1": None,
              "dual": [], "summary": {}}
    # ── Stage 1: 候选池 ──
    confirmed_codes: set = set()
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        stage1_map = {}
        print(f"[Stage1] 跳过 (指定标的 {len(codes)} 只)")
    else:
        print("[Stage1] workflow 层全市场扫描 (候选池生成)...")
        result = full_scan(silent=False)
        all_sig = result.get("all_signals", result["confirmed"] + result["near"])
        # P1-6 (2026-10-01): 原 all_sig[:500] 静默截断，加警告并允许环境变量覆盖
        _s1max = int(os.environ.get("MITM_STAGE1_MAX", "500"))
        if len(all_sig) > _s1max:
            print(f"[warn] Stage1 无 --codes 模式截断: {len(all_sig)} → {_s1max} "
                  f"(MITM_STAGE1_MAX 可调)", flush=True)
        stage1_map = {r["code"]: r for r in all_sig[:_s1max]}
        confirmed_codes = {r["code"] for r in result["confirmed"]}
        confirmed_n = len(result["confirmed"])
        codes = [r["code"] for r in all_sig[:_s1max]]
        codes = list(dict.fromkeys(codes))  # 去重保序
        report["stage1"] = {
            "total_scanned": result["total_scanned"],
            "confirmed": confirmed_n,
            "all_signals": len(all_sig),
            "candidates": len(codes),
        }
        print(f"[Stage1] 覆盖{result['total_scanned']}只 | confirmed={confirmed_n} "
              f"| all_signals={len(all_sig)} | 双引擎全量待验 {len(codes)} 只")

    # ── Stage 2: 双引擎门禁 ──
    print(f"[Stage2] 双引擎门禁 (递归A定方向 + 区间套B定时机), {len(codes)}只...")
    # ── Stage 2 并发执行 (F2, 2026-09-24) ──
    # 原串行循环改为: 抽取 _process_one 单元, ProcessPoolExecutor 并发跑,
    # 主进程按原始顺序串行写账本(保持 jsonl 原子性) + 打印进度。
    # 并发度默认 4 (DUAL2_WORKERS 可调); 子进程各自独立, SIGALRM/耗时可并行。
    _workers = int(os.environ.get("DUAL2_WORKERS", "4"))
    _serial = (args.codes and len(codes) <= 1) or _workers <= 1 or not codes
    print(f"[Stage2] 双引擎门禁 (并发 workers={_workers}{' | 串行回退' if _serial else ''}), {len(codes)}只...")

    _jobs = [
        (c, stage1_map.get(c, {}), c in confirmed_codes, as_of, args.no_beichi,
         (args.vm or ("A" if args.codes_file else "?")))
        for c in codes
    ]
    rows = []
    if _serial:
        for _j in _jobs:
            rows.append(_process_one(*_j))
    else:
        with ProcessPoolExecutor(max_workers=_workers) as _ex:
            rows = list(_ex.map(_process_one, *zip(*_jobs)))

    # 主进程串行: 写账本(原子) + 累计 + 打印 (顺序与 codes 一致)
    for i, row in enumerate(rows, 1):
        _append_ledger_row(args.ledger or LEDGER_PATH, row)
        report["dual"].append(row)
        rs = row.get('recursive_summary', {})
        print(f"  [{i}/{len(codes)}] {row.get('code')} {row.get('name','')} "
              f"A={row.get('recursive_direction','-')} "
              f"(T↑{rs.get('trend_bullish',0)}/T↓{rs.get('trend_bearish',0)} "
              f"S↑{rs.get('segment_bullish',0)}/S↓{rs.get('segment_bearish',0)}"
              f"  form:{rs.get('forming',0)}/{rs.get('evidence_total',0)}"
              f"  score={rs.get('trend_score',0):.2f}/{rs.get('segment_score',0):.2f})"
              f" → {row.get('gate','ERROR')} "
              f"(B 多/空: {row.get('interval_timing',{}).get('bullish_confirmed','-')}/"
              f"{row.get('interval_timing',{}).get('bearish_confirmed','-')})")

    # ── 汇总 ──
    gates = {}
    for r in report["dual"]:
        gates[r["gate"]] = gates.get(r["gate"], 0) + 1
    report["summary"] = gates
        # 双引擎 PASS 里按 DL_P 降序排 (DL 是补充选择项)
    # 2026-09-09: dlp > 0.618 黄金门槛过滤 (用户规则)
    DL_P_MIN = 0.618
    # 2026-09-22: PASS_JEVN 是 JEV 二次门禁拒掉的标的, 不计入通过名单
    # P1-4 (2026-10-01): 原两步过滤净效果即纯 PASS（PASS_JEVN 排除），合并为一步
    all_pass = [r for r in report["dual"] if r["gate"] == "PASS"]
    # 判断 dlp 是否可用（Stage1 可能被跳过导致全 null）
    has_dlp = any(r.get("dlp") is not None and r.get("dlp") > 0 for r in all_pass)
    if has_dlp:
        # 正常模式：按 DL_P 过滤 + 排序
        passing = sorted(
            [r for r in all_pass if (r.get("dlp") or 0) > DL_P_MIN],
            key=lambda r: r.get("dlp") or 0, reverse=True,
        )
    else:
        # dlp 不可用 → 回退：按 B-engine bullish_confirmed 强度 + A-engine trend_score 排序
        print(f"[汇总] dlp 缺失 ({sum(1 for r in all_pass if r.get('dlp') is None)}/{len(all_pass)}) → 回退按 B-engine 强度排序")
        passing = sorted(
            all_pass,
            key=lambda r: (
                r.get("interval_timing", {}).get("bullish_confirmed", 0),
                r.get("interval_timing", {}).get("total", 0) - r.get("interval_timing", {}).get("bearish_confirmed", 0),
            ), reverse=True,
        )

    print(f"\n{'='*70}")
    print("双引擎裁定汇总 (递归A定方向)")
    print(f"{'='*70}")
    print(f"门禁分布: {gates}")
    if passing:
        print(f"\n★ PASS — 递归A=BULLISH 且通过双引擎 ({len(passing)}只, dlp>{DL_P_MIN}):")
        print(f"  {'标的':<14} {'code':<7} {'价格':<8} {'DL_P':<7} "
              f"{'A(T↑/T↓/S↑/S↓)':<16} {'B(多/空)':<10} {'来源'}")
        print(f"  {'-'*85}")
        for r in passing:
            # P1-4 (2026-10-01): 资金参数原为硬编码 20326.12/7847.12，改走环境变量
            _ft = float(os.environ.get("DUAL2_FUNDING_TOTAL", "20326.12"))
            _fb = float(os.environ.get("DUAL2_FUNDING_BASE", "7847.12"))
            f = calc_funding(r.get("price") or 0.0, _ft, _fb)
            src = "confirmed" if r.get("stage1_confirmed") else "near"
            rs = r.get("recursive_summary", {})
            t = f"T↑{rs.get('trend_bullish',0)}/T↓{rs.get('trend_bearish',0)} S↑{rs.get('segment_bullish',0)}/S↓{rs.get('segment_bearish',0)}"
            print(f"  {r.get('name',''):<12} {r['code']:<7} "
                  f"¥{(r.get('price') or 0):<7.2f} {(r.get('dlp') or 0):.4f}  "
                  f"{t:<16} "
                  f"{r['interval_timing']['bullish_confirmed']}/"
                  f"{r['interval_timing']['bearish_confirmed']:<5} {src}")
    else:
        print("\n★ PASS: 0只 (递归A方向未给出 BULLISH)")

    blocked = [r for r in report["dual"] if r["gate"] == "BLOCKED"]
    if blocked:
        print(f"\n⛔ BLOCKED — 递归A=BEARISH ({len(blocked)}只): "
              + ", ".join(f"{r['code']}{r.get('name','')}" for r in blocked))

    # 落盘
    LOGDIR.mkdir(exist_ok=True)
    jpath = LOGDIR / f"dualscan_{ts}.json"
    jpath.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str),
                     encoding="utf-8")
    print(f"\n📄 JSON: {jpath}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
