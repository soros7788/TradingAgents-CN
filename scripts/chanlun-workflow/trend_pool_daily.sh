#!/bin/bash
# 顺势池(双pass) · 每日自动版
# 挂载点: 每交易日 08:20 (在 08:00 meet_merge_watch 之后 / 09:00 背驰池之前, 开盘前两池就绪)
#
# 与背驰转折池(beichi_turn_daily.sh)并列, 两池性质不同、不可混用:
#   顺势池   = 递归 R0+R1 双 pass (present_r_levels>=2) -> 趋势延续, 回踩不破可跟
#   背驰池   = final_verdict==conflict 且 dlp>0.618      -> 转折候选, 逆势抄底
#
# 选源策略(关键): 自动环境下 ~/chan_logs 下会混入大量「分批小扫描」json(每份仅数只),
#   若取单文件 dual 条数最多, 会踩中 20 只小批次。
#   → union 最近 24h 内所有 dualscan json 的 dual 记录(按 code dedup 保最新),
#     写出 dualscan_union_*.json 供下游消费, 同时天然排除陈旧历史 json。
#
# 治理: 仅读 dualscan json + 写 ~/chan_logs 产物; 不碰 sealed 模块。
set -u
HD=$HOME
WF=$HD/TradingAgents-CN/scripts/chanlun-workflow
LOGDIR=$HD/chan_logs
D8=$(date +%Y%m%d)
mkdir -p "$LOGDIR"

# 交易日门 (2026-10-06): 非交易日跳过, 不产出 ghost watchlist
if ! bash "$WF/is_trading_day.sh" >/dev/null 2>&1; then
  echo "$(date '+%F %T') [trend-pool] 非交易日，跳过"
  exit 0
fi

# 防重: pidfile + 存活检查(Step2 批量约 3 分钟, 避免 cron 重入叠加)
# P2-7 修复 (2026-10-01): pidfile check-then-write 有 TOCTOU 竞态, 改用 flock(锁随 fd 释放)
LOCK="$LOGDIR/.trend_pool.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') [trend-pool] 已有实例在运行 -> skip"
  exit 0
fi

cd "$WF" || exit 1

# ---- 0) Regime Gate (2026-10-03 v3) ----
# 池子永远跑，regime 只调阈值/仓位/模式。个股可有独立于指数的周线趋势。
eval $(python3 -c "
import sys; sys.path.insert(0, '.')
from regime_gate import get_regime, pool_config, gen_run_id
import json
r, _ = get_regime()
c = pool_config('trend', r)
print('REGIME=%s' % r)
print('THR_MULT=%.1f' % c['threshold_mult'])
print('SIZE_MULT=%.1f' % c['size_mult'])
print('POOL_MODE=%s' % c['mode'])
print('IS_PRIMARY=%s' % ('1' if c['primary'] else '0'))
print('RUN_ID=%s' % gen_run_id('trend'))
" 2>/dev/null)
echo "$(date '+%F %T') [trend-pool] run_id=$RUN_ID regime=$REGIME mode=$POOL_MODE thr_mult=$THR_MULT size_mult=$SIZE_MULT primary=$IS_PRIMARY"

# ---- 1) 选源 ----
SRC=$(python3 - "$LOGDIR" <<'PY'
import os, sys, json, glob, time
logdir = sys.argv[1]
now = time.time()
merged = {}   # code -> [mtime, record]
for f in glob.glob(os.path.join(logdir, "dualscan_*.json")):
    if "union" in os.path.basename(f):
        continue   # 跳过历史合并产物, 避免自我嵌套
    try:
        mt = os.path.getmtime(f)
    except OSError:
        continue
    # 2026-10-03: 数据窗口改为 7 天 (假期感知)，避免长假后 skip
    _window = int(os.environ.get("POOL_DATA_WINDOW_SEC", "604800"))
    if now - mt > _window:
        continue
    try:
        recs = json.load(open(f)).get("dual") or []
    except Exception:
        continue
    for r in recs:
        code = r.get("code") or r.get("stock") or r.get("symbol") or r.get("ts_code")
        if code and (code not in merged or mt >= merged[code][0]):
            merged[code] = [mt, r]
if not merged:
    print("")
else:
    out = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "stage1": None,
           "dual": [v[1] for v in merged.values()], "summary": {}}
    op = os.path.join(logdir, "dualscan_union_%s.json" % time.strftime("%Y%m%d_%H%M%S"))
    json.dump(out, open(op, "w"), ensure_ascii=False)
    print(op)
PY
)

if [ -z "$SRC" ]; then
  echo "$(date '+%F %T') [trend-pool] 最近 24h 内无可用 dualscan json -> skip"
  exit 0
fi
echo "$(date '+%F %T') [trend-pool] 数据源: $(basename "$SRC")"

# ---- 2) Step2: 双 pass 联立 + 批量周线嵌套 ----
export WN_SCAN="$SRC"
export LIANLI_MODE=pass2            # 强制标准: 递归 R0+R1 双 pass = present_r_levels>=2
export WN_OUT="$LOGDIR/trend_lianli2_${D8}.json"
python3 weekly_nesting_batch.py || exit 1

# ---- 3) Step3: 覆盖层(周线锚 + 新鲜度 + 越界方向化 + 持仓股) ----
export WN_IN="$WN_OUT"
export WNL_OUT="$LOGDIR/trend_pool_watchlist_${D8}.json"
export WNL_TXT="$LOGDIR/trend_pool_watchlist_${D8}.txt"
python3 weekly_watchlist.py || exit 1

# ---- 3b) 注入 regime/engine 标注 (2026-10-03 v3: v1/v5 闭环标注) ----
# 2026-10-05: v5 已接入
export _RUN_ID="$RUN_ID" _REGIME="$REGIME" _POOL_MODE="$POOL_MODE" _THR_MULT="$THR_MULT" _SIZE_MULT="$SIZE_MULT" _IS_PRIMARY="$IS_PRIMARY"
python3 - "$WNL_OUT" <<'PY'
import sys, json, os
p = sys.argv[1]
d = json.load(open(p))
d["engine"] = "v1+v5"
d["v5_status"] = "integrated"
d["run_id"] = os.environ.get("_RUN_ID", "")
d["market_regime"] = os.environ.get("_REGIME", "")
d["pool_mode"] = os.environ.get("_POOL_MODE", "")
d["threshold_mult"] = float(os.environ.get("_THR_MULT", "1.0"))
d["size_mult"] = float(os.environ.get("_SIZE_MULT", "1.0"))
d["is_primary"] = os.environ.get("_IS_PRIMARY", "0") == "1"
# v5 标注 (2026-10-05): 每只加 v5_state
try:
    sys.path.insert(0, "/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow")
    from v5_zhongshu import v5_state
    for r in d.get("records", []):
        try:
            s, direction = v5_state(r["code"])
            r["v5_state"] = s
            r["v5_direction"] = direction
        except Exception as e:
            print("[trend-pool] v5_state 失败 code=%s err=%s" % (r.get("code"), e), flush=True)
            r["v5_state"] = "无"
            r["v5_direction"] = ""
except ImportError as e:
    print("[trend-pool] v5_zhongshu 导入失败: %s" % e, flush=True)
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
print("[trend-pool] 标注已注入: engine=v1+v5 regime=%s mode=%s" % (d["market_regime"], d["pool_mode"]))
PY

# ---- 4) 给报告加口径标注(自动产出必须自解释, 避免与背驰转折池混淆) ----
python3 - "$WNL_TXT" "$SRC" <<'PY'
import sys
p, src = sys.argv[1], sys.argv[2]
s = open(p).read()
h = ("【口径】顺势池 —— 递归 R0+R1 双 pass 口径 (alignment_overall.present_r_levels >= 2)。\n"
     "        选池: 至少 2 个递归层算出方向; 再叠加周线宏观方向锚 + 时间嵌套合规。\n"
     "        越界(B-a2): 按方向重分类 —— 朝周线位置方向的突破=顺势确认(顺n), 反向=真违规(逆m);\n"
     "                    强信号门槛 = 4级齐全 且 逆势越界=0。\n"
     "        数据源: %s\n"
     "        风险属性: 顺势/趋势延续候选(回踩不破可跟), 与背驰转折池性质不同, 不可混用。\n"
     "        持仓股结论以【持仓/观察股重点】段为准。\n"
     % src.split("/")[-1]
     + "=" * 74 + "\n")
open(p, "w").write(h + s)
print("口径标注已写入: %s" % p)
PY

# P2-9 (2026-10-01): 清理 7 天前的 union 中间文件, 防 ~/chan_logs 堆积
find "$LOGDIR" -maxdepth 1 -name 'dualscan_union_*.json' -mtime +7 -delete 2>/dev/null || true
echo "$(date '+%F %T') [trend-pool] DONE -> $WNL_TXT"

# ---- 5) 顺势池双系统分级清单 ----
# 逻辑见 grade_pool_plan.py 头部 L1-L5:方向分流->信号join->A/B/C/JEV分级->区间套买卖点。
# 非致命:失败只记日志,不影响池子主产物。
python3 "$WF/grade_pool_plan.py" --pool-dir "$LOGDIR" --date "$D8" --out-dir "$LOGDIR" \
    >> "$LOGDIR/grade_pool_${D8}.log" 2>&1 || echo "$(date '+%F %T') [trend-pool] 分级失败 rc=$?"

# ---- 6) 交易计划卡片(每日输出格式) ----
# 逻辑见 render_plan_card.py 头部 R1-R5:选头名->动态价格梯子->买入区/止损派生->观察名单。
# 非致命:失败只记日志,不影响池子与分级产物。
python3 "$WF/render_plan_card.py" --pool-dir "$LOGDIR" --date "$D8" --out-dir "$LOGDIR" \
    >> "$LOGDIR/plan_card_${D8}.log" 2>&1 || echo "$(date '+%F %T') [trend-pool] 计划卡片失败 rc=$?"
