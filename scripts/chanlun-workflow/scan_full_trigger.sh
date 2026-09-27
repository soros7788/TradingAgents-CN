#!/bin/bash
# scan_full_trigger.sh — 会合扫描 union 达 100% 后【立即】自动执行后续, 不等次日 cron 窗口
#
# 规则(用户 2026-09-21 定):
#   默认全链路在跑、健康; 但缓存确实陈旧 ——
#   会合扫描 union=3195/3195 (100%, 通常次日 02:00 前后跑完) 完成后【立即自动执行】
#   全宇宙缓存刷新 + 三池, 而不是等到 08:20/09:00/09:20 的 cron 时刻。
#
# 判定: 从 meet_asc.log 取最新一行 "union=N/TOTAL", N>=TOTAL 即 100%
# 去重: 以「日期|N/TOTAL」为 KEY 写 state, 同一轮只触发一次(防每 10min 重复触发)
# 动作:
#   1) universe_kline_sync.sh  —— 全宇宙缓存刷新 + rclone push(耗时数小时, 后台跑)
#   2) trend_pool_daily.sh / beichi_turn_daily.sh / combo_pool_daily.sh —— 三池立即出结果
#       (各脚本自带 pidfile 防重; 此刻 dualscan 已全量产出, 是跑三池的最佳时点)
set -u

WF=/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow
LOGD=/home/gorgesoros39/chan_logs
LOG=$LOGD/scan_full_trigger.log
STATE=$LOGD/.scan_full_trigger_state
ASC_LOG=$LOGD/meet_asc.log
TODAY=$(date +%F)

log(){ echo "$(date '+%F %T') [scan-full] $*" >> "$LOG"; }

[ -f "$ASC_LOG" ] || exit 0

# 最新一条 union 进度行, 形如: [meet] self=580 peer=620 union=1200/3195
LINE=$(grep -a "union=" "$ASC_LOG" 2>/dev/null | tail -1)
[ -n "$LINE" ] || exit 0

CUR=$(echo "$LINE" | sed -n 's/.*union=\([0-9]\{1,\}\)\/\([0-9]\{1,\}\).*/\1/p')
TOTAL=$(echo "$LINE" | sed -n 's/.*union=\([0-9]\{1,\}\)\/\([0-9]\{1,\}\).*/\2/p')
[ -n "$CUR" ] && [ -n "$TOTAL" ] || exit 0
[ "$TOTAL" -gt 0 ] || exit 0

if [ "$CUR" -lt "$TOTAL" ]; then
  exit 0                      # 未达 100%, 静默跳过
fi

KEY="${TODAY}|${CUR}/${TOTAL}"
LAST=$(cat "$STATE" 2>/dev/null || echo "")
if [ "$KEY" = "$LAST" ]; then
  exit 0                      # 本轮已触发过
fi
echo "$KEY" > "$STATE"

log "会合扫描 union=${CUR}/${TOTAL} (100%) -> 立即触发: 全宇宙刷新 + 三池"

# 1) 全宇宙缓存刷新 + push (后台: 单线程约 17.6h, 跨窗口续跑; 此刻扫描已结束, 不抢限流)
nohup bash "$WF/universe_kline_sync.sh" >> "$LOGD/scan_full_sync.log" 2>&1 &
SYNC_PID=$!
log "universe_kline_sync.sh 后台启动 pid=$SYNC_PID (日志 scan_full_sync.log)"

# 2) 三池立即跑 —— 不等次日 cron 时刻
for s in trend_pool_daily.sh beichi_turn_daily.sh combo_pool_daily.sh; do
  bash "$WF/$s" >> "$LOG" 2>&1
  log "$s rc=$?"
done

log "全部触发完成 (sync pid=$SYNC_PID)"
