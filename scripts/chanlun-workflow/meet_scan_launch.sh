#!/bin/bash
# meet_scan_launch.sh — pgrep 防重 + 账本旋转 + 启动 meet_in_the_middle
# 用法: meet_scan_launch.sh asc  |  meet_scan_launch.sh desc

set -e

SIDE="${1:-asc}"
LOGDIR=~/chan_logs
mkdir -p $LOGDIR

SCRIPTDIR=~/TradingAgents-CN/scripts/chanlun-workflow
CODES=~/TradingAgents-CN/kline_cache/_codes_mainboard.txt
LEDGER=~/chan_logs/mtim_ledger/asc.jsonl

# VM-B 信息 (VM-A 写 VM-B 账本, VM-B 本地写自己的账本)
# VM-B 地址动态解析（2026-09-30）：B 外网 IP 为 ephemeral，禁止硬编码
# - VM-A：经 ~/chan_logs/peer_ip.sh 读 gdrive b_ip 信号
# - VM-B：经 GCP metadata 取本机外网 IP（desc 分支 ssh 自环用）
if [ -f "$HOME/chan_logs/peer_ip.sh" ]; then
    . "$HOME/chan_logs/peer_ip.sh" 2>/dev/null || true
    VMB_HOST="${PEER_B_HOST:-katelolita7788@35.212.190.147}"
else
    _BIP=$(curl -sf -m 5 -H "Metadata-Flavor: Google" http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/external-ip 2>/dev/null)
    VMB_HOST="katelolita7788@${_BIP:-35.212.190.147}"
fi
unset _BIP

# ---- 缓存盘健康自检 (2026-09-21 新增: 空盘自愈防护) ----
# 真实计数必须在目标 VM 本机执行(避免 $(...) 被本地 shell 抢展开导致误报 0)
# 行为: 0 csv -> CRITICAL 并中止启动(防盲跑触发 akshare 现拉+限流); <100 -> WARN 仍启动(扫描会按需回填); >=100 -> OK
_cache_csv_count() {
  local d="$1"
  [ -d "$d" ] || { echo 0; return; }
  ls "$d"/*.csv 2>/dev/null | wc -l
}
_cache_health() {
  local d="$1"
  local n
  n=$(_cache_csv_count "$d")
  if [ "$n" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [cache] CRITICAL: KLINE_CACHE_DIR=$d 为空(0 csv) -> 中止启动, 避免盲跑触发 akshare 现拉+限流"
    return 1
  fi
  if [ "$n" -lt 100 ]; then
    echo "$(date +%H:%M:%S) [cache] WARN: KLINE_CACHE_DIR=$d 仅 $n csv(偏薄), 扫描将按需现拉, 速度偏慢"
  else
    echo "$(date +%H:%M:%S) [cache] OK: KLINE_CACHE_DIR=$d 含 $n csv"
  fi
  return 0
}

# ---- pgrep 防重 ----
EXIST=$(pgrep -f "meet_in_the_middle_scan.*--side $SIDE" || true)
if [ -n "$EXIST" ]; then
  echo "$(date +%H:%M:%S) [launch] side=$SIDE worker already running (PID=$EXIST) -> SKIP"
  exit 0
fi

# W1-3: 新一轮启动 -> 清除上一轮完成标记
rm -f "$LOGDIR/.mtim_scan_${SIDE}.done" 2>/dev/null || true

# ---- 账本旋转 (MITM_ROTATE=1 才旋转归档; 默认 0 = 保留账本断点续跑, 防 union 倒退) ----
MITM_ROTATE="${MITM_ROTATE:-0}"
if [ "$MITM_ROTATE" = "1" ] && [ -f "$LEDGER" ]; then
  TS=$(date +%Y%m%d_%H%M%S)
  mv "$LEDGER" "$LOGDIR/scan_ledger.${SIDE}.${TS}.jsonl" 2>/dev/null || true
fi

# ---- 启动 ----
cd $SCRIPTDIR

COMMON_ARGS="--split --side $SIDE --codes $CODES --ledger $LEDGER --dual2-ledger ~/chan_logs/scan_ledger.jsonl --dual2-dir ."

if [ "$SIDE" = "asc" ]; then
  # VM-A 升序: 本地扫, 经 SSH 写 VM-B 共享账本
  # 覆盖 KLINE_CACHE_DIR 到本地缓存 (非 FUSE GDrive)
  KLINE_LOCAL=~/kline_cache_local
  if [ -d "$KLINE_LOCAL" ]; then
    export KLINE_CACHE_DIR="$KLINE_LOCAL"
  fi
  # 缓存盘健康自检: 空则中止, 偏薄则告警(自愈防护, 防盲跑)
  _cache_health "$KLINE_CACHE_DIR" || exit 1
  nohup env MITM_LEDGER="$LEDGER" MITM_SLEEP="0.3" \
    python3 meet_in_the_middle_scan.py $COMMON_ARGS \
    --ssh-host "$VMB_HOST" --ssh-key ~/.ssh/hermes_key \
    --share-remote chanlun-gdrive:mtim_ledger --vm A \
    > $LOGDIR/meet_asc.log 2>&1 &
  echo "$(date +%H:%M:%S) [launch] asc PID=$! KLINE=$KLINE_CACHE_DIR"
else
  # VM-B 降序: 本地扫 + 本地 O_APPEND 账本
  # 需要在 VM-B 上执行, 这里只是 VM-A 转发
  echo "$(date +%H:%M:%S) [launch] desc — forwarding to VM-B..."
  ssh "$VMB_HOST" "bash -s" << REMOTE_EOF
set -e
cd ~/TradingAgents-CN/scripts/chanlun-workflow
CODES=~/TradingAgents-CN/kline_cache/_codes_mainboard.txt
LEDGER=~/chan_logs/mtim_ledger/desc.jsonl
mkdir -p ~/chan_logs

EXIST=\$(pgrep -f "meet_in_the_middle_scan.*--side desc" || true)
if [ -n "\$EXIST" ]; then
  echo "[VM-B launch] already running PID=\$EXIST -> SKIP"
  exit 0
fi

# 删僵尸日志(09-18), 统一活跃日志为 mtim_postmarket_desc.log
rm -f ~/chan_logs/meet_desc.log 2>/dev/null
rm -f ~/chan_logs/.mtim_scan_desc.done 2>/dev/null || true # W1-3

# 账本旋转 (MITM_ROTATE=1 才旋转; 默认 0 = 保留账本断点续跑, 防 union 倒退)
MITM_ROTATE="\${MITM_ROTATE:-0}"
if [ "\$MITM_ROTATE" = "1" ] && [ -f "\$LEDGER" ]; then
  mv "\$LEDGER" ~/chan_logs/scan_ledger.desc.\$(date +%Y%m%d_%H%M%S).jsonl 2>/dev/null || true
fi

# 本地缓存盘 (与 VM-A 一致; 不存在则 fallback 默认本地盘, 绝不走 FUSE)
KLINE_LOCAL=~/kline_cache_local
if [ -d "\$KLINE_LOCAL" ]; then
  export KLINE_CACHE_DIR="\$KLINE_LOCAL"
fi
# 缓存盘健康自检: 空则中止, 偏薄则告警(自愈防护, 防盲跑)
_cache_health "\$KLINE_CACHE_DIR" || exit 1
nohup env MITM_LEDGER="\$LEDGER" MITM_SLEEP="0.3" \
  python3 meet_in_the_middle_scan.py \
  --side desc --codes "\$CODES" --ledger "\$LEDGER" --dual2-ledger ~/chan_logs/scan_ledger.jsonl --dual2-dir . \
  --share-remote chanlun-gdrive:mtim_ledger --vm B \
  > ~/chan_logs/mtim_postmarket_desc.log 2>&1 &
echo "[VM-B launch] desc PID=\$!"
renice -n 0 -p \$! 2>/dev/null || true
REMOTE_EOF
fi

echo "$(date +%H:%M:%S) [launch] done"
