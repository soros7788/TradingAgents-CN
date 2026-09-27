#!/bin/bash
# 背驰转折池 · 每日自动版
# 挂载点: 每交易日 09:00 (在 08:00 meet_merge_watch 合并之后, 昨夜扫描已就绪)
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

cd "$WF" || exit 1
export KLINE_CACHE_DIR="$HD/kline_cache_local"

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
  echo "$(date '+%F %T') [beichi-turn] 最近 24h 内无可用 dualscan json -> skip"
  exit 0
fi
echo "$(date '+%F %T') [beichi-turn] 数据源: $(basename "$SRC")"

# ---- 2) 选池 ----
export WN_SCAN="$SRC"
export BT_CODES_OUT="$LOGDIR/beichi_turn_codes_${D8}.txt"
export BT_JSON_OUT="$LOGDIR/beichi_turn_pool_${D8}.json"
python3 beichi_turn_pool.py || exit 1

# ---- 3) 覆盖层(周线锚 + 新鲜度 + 持仓股) ----
export WNL_OUT="$LOGDIR/beichi_turn_watchlist_${D8}.json"
export WNL_TXT="$LOGDIR/beichi_turn_watchlist_${D8}.txt"
python3 weekly_watchlist.py "$BT_CODES_OUT" || exit 1

# ---- 4) 给报告加口径标注(自动产出也必须自解释, 避免与顺势 2/2 池混淆) ----
python3 - "$WNL_TXT" "$SRC" <<'PY'
import sys
p, src = sys.argv[1], sys.argv[2]
s = open(p).read()
h = ("【口径】背驰转折候选池 —— 缠论原文口径: 转折信号 = 背驰(趋势末端+力度衰竭), 非顺势共振。\n"
     "        选池: alignment_overall.final_verdict == conflict  且  dlp > 0.618 (用户黄金门槛)。\n"
     "        数据源: %s\n"
     "        风险属性: 逆势/转折候选, 与顺势 2/2 池性质不同, 不可混用。\n"
     % src.split("/")[-1]
     + "=" * 74 + "\n")
open(p, "w").write(h + s)
print("口径标注已写入: %s" % p)
PY

echo "$(date '+%F %T') [beichi-turn] DONE -> $WNL_TXT"
