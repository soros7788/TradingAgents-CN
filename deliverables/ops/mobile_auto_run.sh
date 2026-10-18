#!/data/data/com.termux/files/usr/bin/bash
# ==============================================================================
# 缠论双系统 · 手机端独立 K线极速拉取与全自动推流调度器
# 彻底脱离 AI 会话，纯原生本地 Linux 运行，断网断电防休眠
# ==============================================================================

WORK_DIR="/data/data/com.termux/files/home/chanlun_mobile_worker"
KLINE_LOCAL_DIR="$WORK_DIR/kline_cache"
VM_HOST="136.66.64.228"
VM_USER="gorgesoros39"
VM_PASS="Gorgesoros9-Trading2026!"
VM_TARGET_DIR="/home/gorgesoros39/kline_cache_local"
LOG_FILE="$WORK_DIR/run.log"

cd "$WORK_DIR" || exit 1
mkdir -p "$KLINE_LOCAL_DIR"

echo "==========================================================" >> "$LOG_FILE"
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 手机端独立刷K任务启动..." >> "$LOG_FILE"

# 1. 阻止系统锁屏深度休眠
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔒 已开启 Termux Wake-Lock 防锁屏休眠" >> "$LOG_FILE"
fi

# 1.1 弹出开始执行通知与 IP 探测
IP_INFO=$(python3 -c "
import urllib.request
try:
    req = urllib.request.Request('http://cip.cc', headers={'User-Agent': 'curl/7.88.1'})
    with urllib.request.urlopen(req, timeout=3) as r:
        lines = r.read().decode('utf-8', errors='ignore').split('\n')
        ip = next((l.split(':')[1].strip() for l in lines if l.startswith('IP')), '未知')
        addr = next((l.split(':')[1].strip() for l in lines if l.startswith('地址')), '未知')
        isp = next((l.split(':')[1].strip() for l in lines if l.startswith('运营商')), '')
        print(f'{ip} ({addr} {isp})')
except Exception:
    print('网络检测超时')
" 2>/dev/null)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🌐 手机刷K任务执行中，网络出口: $IP_INFO" >> "$LOG_FILE"
if command -v termux-notification >/dev/null 2>&1; then
    timeout 3 termux-notification \
        --title "🚀【缠论系统】手机刷K已启动" \
        --content "当前IP: $IP_INFO。正在极速追加刷新..." \
        --priority max \
        --id 9999 >/dev/null 2>&1 || true
fi

# 2. 权威数据前置质检门禁 (防 15:35 交易所清算延迟/未定型)
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔍 正在探测大盘数据定型状态 (上证日线 + 15:00:00 5m线)..." >> "$LOG_FILE"
python3 "$WORK_DIR/check_market_ready.py" "$@" >> "$LOG_FILE" 2>&1
READY_RC=$?

if [ $READY_RC -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 交易所数据未定型或今日非交易日 (rc=$READY_RC)，安全退出，杜绝脏数据。" >> "$LOG_FILE"
    if command -v termux-wake-unlock >/dev/null 2>&1; then termux-wake-unlock; fi
    exit 0
fi

# 2.1 远程主锁声明: 告知云端“手机已接管刷新，云端自动退让等待，禁止抢写”
echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🔒 正在向云端注记主刷新锁 (mobile_refreshing.lock)..." >> "$LOG_FILE"
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VM_USER@$VM_HOST" \
    "echo $(date +%s) > /home/gorgesoros39/chan_logs/signals/mobile_refreshing.lock" 2>/dev/null || true

# 3. 调用多并发原生脚本拉取 K线 (国内民用 IP 极速通道)
export KLINE_CACHE_DIR="$KLINE_LOCAL_DIR"
export UNIVERSE_FILE="$WORK_DIR/_codes_mainboard.txt"
export KLINE_MAX_WORKERS=4
export KLINE_MIN_SUBMIT_INTERVAL=0.1
export ONE_MIN_CAP=5000

python3 "$WORK_DIR/universe_kline_refresh.py" "$@" >> "$LOG_FILE" 2>&1
REFRESH_RC=$?

if [ $REFRESH_RC -ne 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 刷新任务异常退出 (rc=$REFRESH_RC)，详见日志" >> "$LOG_FILE"
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 本地 1m/Day 拉取完成，开始本地极速合成 5m 与 30m 周期..." >> "$LOG_FILE"
    KLINE_CACHE_DIR="$KLINE_LOCAL_DIR" python3 "$WORK_DIR/resample_universe_5m_30m.py" >> "$LOG_FILE" 2>&1 || true
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ✅ 全周期 K线对齐完成！准备推送到云端 VM..." >> "$LOG_FILE"
fi


# 4. 极速增量推送到 GCP VM (安全门禁保护 + 增量推流)
CSV_COUNT=$(find "$KLINE_LOCAL_DIR" -name "*.csv" | wc -l)
MIN_SAFE_CSV_COUNT=12000

if [ "$CSV_COUNT" -lt "$MIN_SAFE_CSV_COUNT" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 熔断拦截: 本地 K线数量 ($CSV_COUNT) 小于安全水位 ($MIN_SAFE_CSV_COUNT)！" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛑 拒绝推送到云端，坚决杜绝截断覆盖云端 5 年历史资产！" >> "$LOG_FILE"
    if command -v termux-wake-unlock >/dev/null 2>&1; then termux-wake-unlock; fi
    exit 2
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🛡️ 安全门禁通过 (本地底座 $CSV_COUNT 个)，正在以 --update 增量同步到 VM ($VM_HOST)..." >> "$LOG_FILE"
rsync -avz --update --inplace -e "ssh -o StrictHostKeyChecking=no" "$KLINE_LOCAL_DIR/" "$VM_USER@$VM_HOST:$VM_TARGET_DIR/" >> "$LOG_FILE" 2>&1
SYNC_RC=$?

if [ $SYNC_RC -eq 0 ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🎉 同步完成！云端 VM 已无损更新到最新新鲜度数据！" >> "$LOG_FILE"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚡ 发出事件驱动触发指令 -> VM-A 启动全自动流水线..." >> "$LOG_FILE"
    ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 "$VM_USER@$VM_HOST" \
        "nohup bash /home/gorgesoros39/chan_logs/on_kline_pushed_pipeline.sh >> /home/gorgesoros39/chan_logs/pipeline_trigger.log 2>&1 &"
    TRIGGER_RC=$?
    if [ $TRIGGER_RC -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🚀 云端后处理流水线已成功触发运行！" >> "$LOG_FILE"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️ 云端流水线触发警告 (rc=$TRIGGER_RC)" >> "$LOG_FILE"
    fi
else
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ 同步到 VM 失败 (rc=$SYNC_RC)" >> "$LOG_FILE"
fi

# 4. 释放远程主锁与本地唤醒锁
ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 "$VM_USER@$VM_HOST" \
    "rm -f /home/gorgesoros39/chan_logs/signals/mobile_refreshing.lock" 2>/dev/null || true

if command -v termux-wake-unlock >/dev/null 2>&1; then
    termux-wake-unlock
fi

echo "[$(date '+%Y-%m-%d %H:%M:%S')] 🏁 本轮调度全部结束" >> "$LOG_FILE"
