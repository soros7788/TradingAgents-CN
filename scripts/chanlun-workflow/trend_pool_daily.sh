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

# 防重: pidfile + 存活检查(Step2 批量约 3 分钟, 避免 cron 重入叠加)
# P2-7 修复 (2026-10-01): pidfile check-then-write 有 TOCTOU 竞态, 改用 flock(锁随 fd 释放)
LOCK="$LOGDIR/.trend_pool.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') [trend-pool] 已有实例在运行 -> skip"
  exit 0
fi

cd "$WF" || exit 1

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
    if now - mt > 86400:          # 仅最近 24h, 排除陈旧历史
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
