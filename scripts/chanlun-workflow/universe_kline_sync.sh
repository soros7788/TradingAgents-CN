#!/usr/bin/env bash
# universe_kline_sync.sh — 全宇宙每交易日增量刷新(写本地) + push 到 GDrive hub
#
# 设计(守治理 + 规避 VM-A FUSE 写死锁):
#   1. universe_kline_refresh.py 写 LOCAL 副本(KLINE_CACHE_DIR 指向本地 ext4, 不碰 FUSE)
#      —— VM-A 的 GDrive FUSE 挂载写入会触发 D 状态死锁, 故绝不走 FUSE 写
#   2. rclone copy 本地 → gdrive: hub (走 rclone API, 非 FUSE 挂载)
#   3. VM-B 现有 15:40 rclone pull 从 hub 拉到本地(已就绪)
#
# 用法: universe_kline_sync.sh            # 全宇宙, 交易日收盘后由 timer 调用
#       universe_kline_sync.sh --limit 5  # 冒烟/测试
set -u
# 2026-09-28: 限流驱动角色管理（用户批准）
# role_manager.sh 接管调度，本脚本保留为手动 fallback
if [ "${ROLE_MANAGER:-1}" = "1" ] && [ -x "$HOME/chan_logs/role_manager.sh" ]; then
    exec "$HOME/chan_logs/role_manager.sh" a
fi
export PATH="$HOME/bin:$PATH"

LOCAL_DIR=/home/gorgesoros39/kline_cache_local
HUB=gdrive:TradingAgents-CN/kline_cache
REFRESH=/home/gorgesoros39/TradingAgents-CN/scripts/chanlun-workflow/universe_kline_refresh.py
PYTHON=/home/gorgesoros39/TradingAgents-CN/.venv/bin/python
RCLONE=/home/gorgesoros39/bin/rclone
DATE=$(date +%Y%m%d)
LOG=/home/gorgesoros39/chan_logs/universe_sync_${DATE}.log

LIMIT_ARG=""
if [ "${1:-}" = "--limit" ]; then LIMIT_ARG="--limit $2"; fi

mkdir -p "$(dirname "$LOG")"
echo "$(date '+%F %T') [sync] start limit='${LIMIT_ARG}'" >> "$LOG"

# D) 陈旧 lock 自愈 (12.5 待办 D: 零风险防御)
#    universe_kline_refresh.py 内部用 fcntl.flock 防重叠(锁随进程退出/被杀自动释放, 无残留)。
#    此处仅防御极端残留: 若当前【无活 refresh 进程】且 lock 文件仍在, 清除之,
#    保证本轮 refresh 不被陈旧文件干扰。若检测到活 refresh, 不动 lock (不抢锁/不并发刷)。
if ! pgrep -f "universe_kline_refresh.py" >/dev/null 2>&1; then
  if [ -e "$HOME/kline_refresh.lock" ]; then
    rm -f "$HOME/kline_refresh.lock" 2>/dev/null
    echo "$(date '+%F %T') [sync] D-selfheal: 无活 refresh, 已清除 stale lock" >> "$LOG"
  fi
else
  echo "$(date '+%F %T') [sync] D-selfheal: 检测到活 refresh 进程, 保留 lock(本轮仅 push)" >> "$LOG"
fi

# 1) 刷新写本地(append-only + 1m 滚窗封顶 5000)
KLINE_CACHE_DIR="$LOCAL_DIR" "$PYTHON" "$REFRESH" $LIMIT_ARG --coord-id a >> "$LOG" 2>&1
RC1=$?
echo "$(date '+%F %T') [sync] refresh rc=$RC1" >> "$LOG"

# 2) push 本地 → hub (size-only: 仅上传 size 变化的文件, 日常增量极小)
"$RCLONE" copy "$LOCAL_DIR/" "$HUB/" --size-only --log-file="$LOG" --log-level INFO >> "$LOG" 2>&1
RC2=$?
echo "$(date '+%F %T') [sync] rclone-push rc=$RC2" >> "$LOG"

echo "$(date '+%F %T') [sync] done (refresh=$RC1 push=$RC2)" >> "$LOG"
exit $((RC1 != 0 ? RC1 : RC2))
