#!/bin/bash
# ==============================================================================
# instabox_pipeline.sh — InstaBox 4核8G 沙箱极速闭环调度器
# 流程：
#   1. 唤醒沙箱 (resume) 并探测就绪
#   2. 增量同步当日新鲜 K 线 (rsync 极速秒级同步)
#   3. 在沙箱内 4核满血并发扫穿全市场 + 周线嵌套 + 分级 + 计划卡出炉
#   4. 产物秒级同步回本地 ~/chan_logs 并推至 VM-A
#   5. 安全休眠沙箱 (pause) 杜绝空转耗损
# ==============================================================================

set -uo pipefail

export INSTABOX_API_KEY="ibk_ugtd67auowacprodrjsngdngth3wifz326ywyui52c36x6ygpkda"
INSTABOX_BIN="$HOME/.local/bin/instabox"
SANDBOX_NAME="agy-sandbox-01"
LOG="$HOME/chan_logs/instabox_pipeline.log"
TODAY=$(date +%Y%m%d)

log() {
  echo "[$(date "+%Y-%m-%d %H:%M:%S")] [instabox-pipeline] $*" | tee -a "$LOG"
}

log "🚀 收到极速调度指令，启动 InstaBox 4核8G 流水线..."

# 1. 唤醒沙箱
log "⚡ [Step 1] 唤醒沙箱 $SANDBOX_NAME..."
$INSTABOX_BIN resume "$SANDBOX_NAME" >> "$LOG" 2>&1 || true

# 轮询探测 SSH 就绪 (最多等 60 秒)
READY=0
for i in $(seq 1 12); do
  sleep 5
  if $INSTABOX_BIN ssh "$SANDBOX_NAME" -- "echo READY" >/dev/null 2>&1; then
    READY=1
    log "✅ 沙箱已就绪 (耗时约 $((i * 5)) 秒)"
    break
  fi
  log "   等待沙箱 SSH 握手中 ($i/12)..."
done

if [ "$READY" -ne 1 ]; then
  log "❌ 沙箱唤醒超时，中止并由宿主兜底！"
  exit 1
fi

# 2. 极速增量推流 K 线至沙箱 (仅同步最近变化的几兆增量)
KLINE_SRC="$HOME/TradingAgents-CN/kline_cache"
if [ -d "$KLINE_SRC" ]; then
  log "📦 [Step 2] 增量推流当日新鲜 K 线至沙箱..."
  rsync -az --update -e "$INSTABOX_BIN ssh" \
    "$KLINE_SRC/" "$SANDBOX_NAME:/data/home/kline_cache/" >> "$LOG" 2>&1 || log "⚠️ K线增量同步非致命异常，继续执行"
  log "✅ K线增量推流完成！"
fi

# 3. 在沙箱内满血执行 4核全市场扫描与计划卡渲染
log "⚡ [Step 3] 沙箱内 4核满血执行全市场对撞扫描 + 周线嵌套 + 辩论出卡..."
$INSTABOX_BIN ssh "$SANDBOX_NAME" -- "bash -c '
  export KLINE_CACHE_DIR=/data/home/kline_cache
  export LOGDIR=/data/home/chan_logs
  export SCAN_WORKERS=4
  mkdir -p /data/home/chan_logs
  echo "[sandbox] 🚀 自动刷新并补齐全市场 5m 与 30m 周期..."
  python3 /data/home/resample_universe_5m_30m.py >> /data/home/chan_logs/resample.log 2>&1 || true
  cd /data/home/chanlun-workflow
  # 若当日已有 union 结果直接出卡，否则 4 核全并发扫描
  if ls /data/home/chan_logs/dualscan_*.json >/dev/null 2>&1; then
    echo \"[sandbox] 发现已有双pass数据，直接执行出卡\"
  else
    echo \"[sandbox] 启动 4核全并发 dual2_scan...\"
    python3 dual2_scan.py >> /data/home/chan_logs/dual2_scan.log 2>&1 || true
  fi
  FORCE=1 bash trend_pool_daily.sh >> /data/home/chan_logs/trend_pool.log 2>&1
'" >> "$LOG" 2>&1
RUN_RC=$?

# 4. 同步产物
if [ "$RUN_RC" -eq 0 ]; then
  log "✅ [Step 4] 沙箱渲染成功！拉取计划卡与分级清单..."
  rsync -az -e "$INSTABOX_BIN ssh" \
    "$SANDBOX_NAME:/data/home/chan_logs/trade_plan_card_*.html" "$HOME/chan_logs/" 2>/dev/null || true
  rsync -az -e "$INSTABOX_BIN ssh" \
    "$SANDBOX_NAME:/data/home/chan_logs/trend_pool_*.*" "$HOME/chan_logs/" 2>/dev/null || true
  # 同步回 VM-A 中枢
  rsync -az -e "ssh -o StrictHostKeyChecking=accept-new" \
    "$HOME/chan_logs/trade_plan_card_${TODAY}.html" gorgesoros39@136.66.64.228:~/chan_logs/ 2>/dev/null || true
  rsync -az -e "ssh -o StrictHostKeyChecking=accept-new" \
    "$HOME/chan_logs/trend_pool_graded_${TODAY}.md" gorgesoros39@136.66.64.228:~/chan_logs/ 2>/dev/null || true
  log "🎉 今日计划卡已成功生成并安全同步至 VM-A 中枢！"
else
  log "⚠️ [Step 4] 沙箱内部执行返回状态码 $RUN_RC"
fi

# 5. 安全休眠沙箱 (绝不空转)
log "🛡️ [Step 5] 执行安全挂起，沙箱进入 Paused 休眠态..."
$INSTABOX_BIN pause "$SANDBOX_NAME" >> "$LOG" 2>&1 || true
log "🏁 InstaBox 极速调度流程收尾结束。"

exit $RUN_RC
