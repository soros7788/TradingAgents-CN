#!/bin/bash
# ==============================================================================
# on_kline_pushed_pipeline.sh — 盘后事件驱动流水线主入口 (2026-10-18 极速沙箱+无缝兜底版)
# 触发源：手机 Termux 增量 rsync K线完成后的瞬时 SSH 事件调用
# 职责链：
#   1. 防重与环境检查 (flock + 交易日门 + 新鲜度门)
#   2. GCP 内网极速同步 K线至 VM-B (毫秒级 SSH/rsync, 弃用 GDrive)
#   3. 【极速沙箱先行】调起 InstaBox 4核8G 并发扫穿 + 极速出卡 (半小时闭环)
#   4. 【零风险兜底】若沙箱超时或异常，自动无缝切换到 GCP VM-B 升配双机对撞保底
# ==============================================================================

set -u
LOGDIR="$HOME/chan_logs"
mkdir -p "$LOGDIR"
LOGFILE="$LOGDIR/pipeline_$(date +%Y%m%d).log"
LOCKFILE="/tmp/on_kline_pushed.lock"

log() {
    echo "[$(date +%Y-%m-%d %H:%M:%S)] [pipeline] $*" | tee -a "$LOGFILE"
}

# 1. flock 原子防重入
exec 8>"$LOCKFILE"
if ! flock -n 8; then
    log "⚠️ 流水线正在运行中，跳过重复触发。"
    exit 0
fi

log "🚀 收到手机端 K线推送事件，开始执行全自动盘后流水线..."

# 2. 交易日与工作日校验
DOW=$(date +%u)
TODAY=$(date +%F)
if [ "$DOW" -ge 6 ]; then
    log "🛑 今日非交易日 ($TODAY)，流水线安全退出。"
    exit 0
fi
if [ -f "$LOGDIR/trading_holidays.txt" ] && grep -qx "$TODAY" "$LOGDIR/trading_holidays.txt" 2>/dev/null; then
    log "🛑 今日为法定节假日 ($TODAY)，流水线安全退出。"
    exit 0
fi

# 3. K线本地盘新鲜度快速抽验
KDIR="$HOME/kline_cache_local"
if [ ! -d "$KDIR" ]; then
    log "❌ 本地 K线目录不存在: $KDIR"
    exit 1
fi
CSV_COUNT=$(ls "$KDIR"/*.csv 2>/dev/null | wc -l)
if [ "$CSV_COUNT" -lt 100 ]; then
    log "❌ 本地 CSV 仅 $CSV_COUNT 个 (低于100)，中止启动防盲跑。"
    exit 1
fi
log "✅ 本地 K线就绪，共计 $CSV_COUNT 个 CSV。"

# 4. 动态解析 VM-B IP
if [ -f "$HOME/chan_logs/peer_ip.sh" ]; then
    source "$HOME/chan_logs/peer_ip.sh"
fi
VMB_USER_HOST="${PEER_B_HOST:-katelolita7788@35.212.228.16}"
VMB_IP=$(echo "$VMB_USER_HOST" | awk -F@ '{print $2}')
log "📍 VM-B 目标解析: $VMB_USER_HOST (IP: $VMB_IP)"

# 5. GCP 内网极速同步 K线至 VM-B
log "📦 正在向 VM-B 同步最新 K线 (内网 rsync)..."
ssh -i ~/.ssh/hermes_key -o StrictHostKeyChecking=no -o ConnectTimeout=10 katelolita7788@"$VMB_IP" "mkdir -p ~/TradingAgents-CN/kline_cache" 2>/dev/null || true
rsync -avz --inplace -e "ssh -i $HOME/.ssh/hermes_key -o StrictHostKeyChecking=no -o ConnectTimeout=10" \
    "$KDIR/" katelolita7788@"$VMB_IP":~/TradingAgents-CN/kline_cache/ >> "$LOGFILE" 2>&1
if [ $? -eq 0 ]; then
    log "✅ VM-B K线内网同步完成！"
else
    log "⚠️ VM-B 内网同步报错，将继续执行极速通道或保底对撞..."
fi

# 5.5 【极速通道】优先调起 InstaBox 4核8G 极速流水线 (若成功出卡则提前结束，免升配 VM-B)
ENABLE_INSTABOX="${ENABLE_INSTABOX:-1}"
if [ "$ENABLE_INSTABOX" = "1" ]; then
    log "⚡ [极速通道] 优先调起 InstaBox 4核8G 极速流水线..."
    if ssh -i ~/.ssh/hermes_key -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
        katelolita7788@"$VMB_IP" "bash ~/chan_logs/instabox_pipeline.sh" >> "$LOGFILE" 2>&1; then
        TODAY_D8=$(date +%Y%m%d)
        TODAY_CARD="$LOGDIR/trade_plan_card_${TODAY_D8}.html"
        if [ -s "$TODAY_CARD" ]; then
            log "🎉 [成功闭环] InstaBox 极速出卡成功！计划卡已落盘: $TODAY_CARD"
            log "✨ 盘后流水线圆满收官 (免升配 VM-B，零闲置扣费)！"
            exit 0
        else
            log "⚠️ InstaBox 流水线返回正常但未见今日计划卡落盘，无缝进入 VM-B 升配保底通道..."
        fi
    else
        log "⚠️ InstaBox 极速通道执行异常或超时，无缝回退至原有 VM-B 升配对撞流水线保底！"
    fi
fi

# 6. 【兜底通道】自动升级 VM-B 至 e2-standard-2
log "⚡ [兜底通道] 正在调用 GCP API 升级 VM-B 至 e2-standard-2..."
$HOME/TradingAgents-CN/.venv/bin/python $HOME/chan_logs/b_vm_autoscale.py upgrade >> "$LOGFILE" 2>&1 || log "⚠️ 升级调用异常，将继续后续启动"

# 轮询确认 VM-B 状态与 SSH 就绪
log "⏳ 等待 VM-B 升配开机与 SSH 就绪..."
for i in $(seq 1 12); do
    sleep 10
    if [ -f "$HOME/chan_logs/peer_ip.sh" ]; then
        source "$HOME/chan_logs/peer_ip.sh"
    fi
    VMB_USER_HOST="${PEER_B_HOST:-katelolita7788@35.212.228.16}"
    VMB_IP=$(echo "$VMB_USER_HOST" | awk -F@ '{print $2}')
    if ssh -i ~/.ssh/hermes_key -o StrictHostKeyChecking=no -o ConnectTimeout=5 -o BatchMode=yes katelolita7788@"$VMB_IP" "uptime" >/dev/null 2>&1; then
        log "✅ VM-B 已就绪！当前 IP: $VMB_IP"
        break
    fi
    log "   等待 VM-B 握手中 ($i/12)..."
done

# 7. 全速拉起双机对撞扫描 (A=asc, B=desc)
log "🏁 正在拉起双机对撞扫描..."
# 7.1 启动 VM-B (desc)
$HOME/TradingAgents-CN/scripts/chanlun-workflow/meet_scan_launch.sh desc --force >> "$LOGFILE" 2>&1 || log "⚠️ B 端启动返回异常"
# 7.2 启动 VM-A (asc)
$HOME/TradingAgents-CN/scripts/chanlun-workflow/meet_scan_launch.sh asc --force >> "$LOGFILE" 2>&1 || log "⚠️ A 端启动返回异常"

# 8. 挂载双端去重聚合降级 Monitor
log "🛡️ 挂载双端聚合降级 Monitor (3195 全量完成即自动降配)..."
pkill -f "b_autodowngrade_monitor.sh" 2>/dev/null || true
nohup $HOME/chan_logs/b_autodowngrade_monitor.sh 3195 >> "$LOGDIR/b_autodowngrade.log" 2>&1 &
log "🎉 全自动化流水线已成功点火并进入双机对撞监控状态！(Monitor PID: $!)"
