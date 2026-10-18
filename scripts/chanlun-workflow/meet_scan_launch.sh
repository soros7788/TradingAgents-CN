#!/bin/bash
# meet_scan_launch.sh — pgrep 防重 + 账本旋转 + 启动 meet_in_the_middle
# 用法: meet_scan_launch.sh asc  |  meet_scan_launch.sh desc

set -e

# ---- 参数解析 (2026-10-01: 增加 --force) ----
SIDE="asc"
FORCE=0
for _a in "$@"; do
  case "$_a" in
    asc|desc) SIDE="$_a" ;;
    --force)  FORCE=1 ;;
  esac
done
unset _a

# ---- VM-B 防护 (2026-10-04): B 端只准跑 desc, 跑 asc 直接报错 ----
# 原理: AB 互助要求 A=asc B=desc, B 端跑 asc 会跟 A 重叠
if [ "$SIDE" = "asc" ] && [ "$(hostname)" != "tradingagents-new" ]; then
  # 非 A 主机 (即 VM-B) 跑 asc → 报错, 除非 --force
  if [ "$FORCE" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [guard] ERROR: 本机非 VM-A, 禁止跑 asc (会跟 A 端重复)。B 端只准跑 desc。" >&2
    echo "如确需手动干跑, 加 --force。" >&2
    exit 1
  elif [ "$hour" -lt 15 ]; then
    # 2026-10-13: 15点前用上一个交易日
    if [ "$dow" -eq 1 ]; then
      date -d "$today -3 days" +%F
    else
      date -d "$today -1 day" +%F
    fi
  else
    echo "$(date +%H:%M:%S) [guard] WARN: --force 放行, 在非 A 主机跑 asc。" >&2
  fi
fi

# ---- 盘后时间门 (2026-10-16 补丁: 防盘中提前偷跑，必须等 15:50 之后数据定型) ----
if [ "${FORCE:-0}" -eq 0 ]; then
  _CUR_HHMM=$(date +%H%M)
  if [ "$_CUR_HHMM" -lt "1550" ]; then
    echo "$(date +%H:%M:%S) [gate] 当前时间早于 15:50 (当前: $(date +%H:%M))，交易所未完全定型/推流未收尾，禁止启动盘后扫描。如需手动干跑加 --force。"
    exit 0
  fi
  unset _CUR_HHMM
fi

LOGDIR=~/chan_logs
mkdir -p $LOGDIR

# ---- 交易日门 (2026-10-01 止血: 休市日 cron 照跑空烧算力+JEV API) ----
# 周末(周六/日)或 $LOGDIR/trading_holidays.txt 中的法定节假日 → 非交易日直接退出(0)
# 口径与 daily_morning_check.sh 一致；手动验证/干跑加 --force 放行
if [ "$FORCE" -eq 0 ]; then
  _DOW=$(date +%u); _TODAY=$(date +%F)
  _TRADING=1
  if [ "$_DOW" -ge 6 ]; then _TRADING=0; fi
  if [ -f "$LOGDIR/trading_holidays.txt" ] && grep -qx "$_TODAY" "$LOGDIR/trading_holidays.txt" 2>/dev/null; then
    _TRADING=0
  fi
  if [ "$_TRADING" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [gate] 今日非交易日($_TODAY)，跳过扫描启动。如需手动干跑，加 --force。"
    exit 0
  fi
  unset _DOW _TODAY _TRADING
fi

# ---- K刷新互斥 (2026-10-16 跨机加固): 本机刷新/手机刷新/推流未完成时不启动扫描 ----
if [ "${FORCE:-0}" -eq 0 ]; then
  # 自动解救卡死僵尸: 若检测到刷新进程已运行超过 2 小时 (7200s)，判定为异常卡死，自动强杀释放
  for _pid in $(pgrep -f universe_kline_refresh.py 2>/dev/null || true); do
    _etime=$(ps -o etimes= -p "$_pid" 2>/dev/null | tr -d " " || echo 0)
    if [ -n "$_etime" ] && [ "$_etime" -gt 7200 ]; then
      echo "$(date +%H:%M:%S) [gate] 发现运行超过 ${_etime}s 的僵尸刷新进程 (PID=$_pid)，自动执行自愈清理！" >&2
      kill -9 "$_pid" 2>/dev/null || true
    fi
  done
  unset _pid _etime

  # 检查并清理超时的手机端锁 (防网络断连残留)
  _MOB_LOCK="/data/data/com.termux/files/home/chan_logs/signals/mobile_refreshing.lock"
  if [ -f "$_MOB_LOCK" ]; then
    _lock_age=$(($(date +%s) - $(stat -c %Y "$_MOB_LOCK" 2>/dev/null || echo $(date +%s))))
    if [ "$_lock_age" -gt 7200 ]; then
      echo "$(date +%H:%M:%S) [gate] 发现超过 2 小时的手机僵尸主锁，自动清除。" >&2
      rm -f "$_MOB_LOCK" 2>/dev/null || true
    fi
    unset _lock_age
  fi

  _WAIT=0
  while pgrep -f universe_kline_refresh.py >/dev/null 2>&1 || [ -f "$_MOB_LOCK" ] || pgrep -f "rsync.*kline_cache" >/dev/null 2>&1; do
    if [ "$_WAIT" -ge 1800 ]; then
      echo "$(date +%H:%M:%S) [gate] K线刷新/推流超过 30 分钟未完成，跳过本次扫描启动（防并发冲突）。"
      exit 0
    fi
    if [ "$_WAIT" -eq 0 ]; then
      echo "$(date +%H:%M:%S) [gate] 检测到 K线刷新或推流进行中，等待完成后再启动扫描..."
    fi
    sleep 30
    _WAIT=$(($_WAIT + 30))
  done
  if [ "$_WAIT" -gt 0 ]; then
    echo "$(date +%H:%M:%S) [gate] K线刷新与推流已完成，放行启动。"
  fi
  unset _WAIT _MOB_LOCK
fi

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
  local hour dow today
  hour=$(date +%H)
  dow=$(date +%u)
  today=$(date +%F)
  n=$(_cache_csv_count "$d")
  if [ "$n" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [cache] CRITICAL: KLINE_CACHE_DIR=$d 为空(0 csv) -> 中止启动, 避免盲跑触发 akshare 现拉+限流"
    return 1
  fi
  if [ "$n" -lt 100 ]; then
    echo "$(date +%H:%M:%S) [cache] WARN: KLINE_CACHE_DIR=$d 仅 $n csv(偏薄), 扫描将按需现拉, 速度偏慢"
  elif [ "$hour" -lt 15 ]; then
    # 2026-10-13: 15点前用上一个交易日
    if [ "$dow" -eq 1 ]; then
      date -d "$today -3 days" +%F
    else
      date -d "$today -1 day" +%F
    fi
  else
    echo "$(date +%H:%M:%S) [cache] OK: KLINE_CACHE_DIR=$d 含 $n csv"
  fi
  return 0
}

# ---- K线新鲜度门 (2026-10-16 补丁: 跨号段全域质检，杜绝字典序抽查偏差) ----
_cache_freshness() {
  local d="$1"
  local expected="$2"  # 期望日期 YYYY-MM-DD
  local fresh=0
  local total=0
  local latest=""

  # 涵盖深市主板头部、中小板、沪市大盘金融、权重白酒、沪市小盘、沪市主板尾部
  local test_codes=("000001" "000002" "002313" "600000" "600519" "601398" "603993" "605599")
  for _c in "${test_codes[@]}"; do
    local f="$d/${_c}_day.csv"
    [ -f "$f" ] || continue
    total=$(($total + 1))
    _ld=$(tail -1 "$f" 2>/dev/null | cut -d, -f1 | cut -d" " -f1)
    if [ -z "$latest" ] || [ "$_ld" \> "$latest" ]; then
      latest="$_ld"
    fi
    if [ "$_ld" = "$expected" ]; then
      fresh=$(($fresh + 1))
    fi
  done

  if [ "$total" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [fresh] CRITICAL: 无指定代表性日线csv可抽查 -> 中止"
    return 1
  fi
  echo "$(date +%H:%M:%S) [fresh] 跨号段抽查$total只标的，最新日期=$latest，期望=$expected，新鲜=$fresh/$total"
  # 至少 7/8 (约85%) 新鲜才放行（允许极个别停牌股票）
  local min_fresh=$(($total * 8 / 10))
  if [ "$fresh" -lt "$min_fresh" ]; then
    echo "$(date +%H:%M:%S) [fresh] CRITICAL: K线过期或未全量同步（新鲜 ${fresh}/${total} < ${min_fresh}），中止扫描。"
    return 1
  fi
  echo "$(date +%H:%M:%S) [fresh] OK: 跨号段全域新鲜度通过"
  return 0
}

# 计算期望的最近交易日（YYYY-MM-DD）
_expected_trading_day() {
  local dow=$(date +%u)
  local today=$(date +%F)
  local hour=$(date +%H)
  # 周六→周五，周日→周五，工作日→今天
  # （法定节假日靠 trading_holidays.txt 在交易日门已拦，这里简化）
  if [ "$dow" -eq 6 ]; then
    date -d "$today -1 day" +%F
  elif [ "$dow" -eq 7 ]; then
    date -d "$today -2 days" +%F
  elif [ "$hour" -lt 15 ]; then
    # 2026-10-13: 15点前用上一个交易日
    if [ "$dow" -eq 1 ]; then
      date -d "$today -3 days" +%F
    else
      date -d "$today -1 day" +%F
    fi
  else
    echo "$today"
  fi
}

# ---- pgrep 防重 + flock 原子锁 (2026-10-04: mini 审查, 防 TOCTOU 双启) ----
# flock 确保"检查+启动"原子性, pgrep 做二次确认
LOCKFILE="/tmp/meet_scan_${SIDE}.lock"
exec 200>"$LOCKFILE"
if ! flock -n 200; then
  echo "$(date +%H:%M:%S) [launch] side=$SIDE lock held -> SKIP"
  exit 0
fi
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
  # K线新鲜度门 (2026-10-10): 确认是最近交易日数据，过期则中止
  if [ "$FORCE" -eq 0 ]; then
    _EXP_DAY=$(_expected_trading_day)
    _cache_freshness "$KLINE_CACHE_DIR" "$_EXP_DAY" || exit 1
    unset _EXP_DAY
  fi
  _FRESH_FLAG=""
  if [ "$FORCE" -eq 1 ]; then _FRESH_FLAG="--no-freshness-check"; fi
  nohup env MITM_LEDGER="$LEDGER" MITM_SLEEP="0.3" MITM_BATCH_TIMEOUT="${MITM_BATCH_TIMEOUT:-600}" \
    python3 meet_in_the_middle_scan.py $COMMON_ARGS $_FRESH_FLAG \
    --ssh-host "$VMB_HOST" --ssh-key ~/.ssh/hermes_key \
    --share-mode rsync \
    --share-remote chanlun-gdrive:mtim_ledger --vm A \
    > $LOGDIR/meet_asc.log 2>&1 &
  echo "$(date +%H:%M:%S) [launch] asc PID=$! KLINE=$KLINE_CACHE_DIR"
else
  # VM-B 降序: 本地扫 + 本地 O_APPEND 账本
  # 需要在 VM-B 上执行, 这里只是 VM-A 转发
  # 2026-10-04: SSH 失败必须报错退出 (mini 审查)
  echo "$(date +%H:%M:%S) [launch] desc — forwarding to VM-B..."
  ssh -o ConnectTimeout=10 -o BatchMode=yes "$VMB_HOST" "bash -s" << REMOTE_EOF || { echo "$(date +%H:%M:%S) [launch] ERROR: SSH to VM-B ($VMB_HOST) failed" >&2; exit 1; }
set -e
# 远程函数定义 (2026-10-04 修复: _cache_health 在远程未定义)
_cache_csv_count() {
  local d="$1"
  [ -d "$d" ] || { echo 0; return; }
  ls "$d"/*.csv 2>/dev/null | wc -l
}
_cache_health() {
  local d="$1"
  local n
  local hour dow today
  hour=$(date +%H)
  dow=$(date +%u)
  today=$(date +%F)
  n=$(_cache_csv_count "$d")
  if [ "$n" -eq 0 ]; then
    echo "$(date +%H:%M:%S) [cache] CRITICAL: KLINE_CACHE_DIR=$d 为空 -> 中止" >&2
    return 1
  fi
  return 0
}
cd ~/TradingAgents-CN/scripts/chanlun-workflow
CODES=~/TradingAgents-CN/kline_cache/_codes_mainboard.txt
LEDGER=~/chan_logs/mtim_ledger/desc.jsonl
mkdir -p ~/chan_logs/mtim_ledger

# flock 原子锁 (2026-10-04)
exec 200>/tmp/meet_scan_desc.lock
if ! flock -n 200; then
  echo "[VM-B launch] lock held -> SKIP"
  exit 0
fi
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
nohup env DUAL2_WORKERS="${DUAL2_WORKERS:-1}" MITM_LEDGER="\$LEDGER" MITM_SLEEP="0.3" MITM_BATCH_TIMEOUT="\${MITM_BATCH_TIMEOUT:-600}" \
  python3 meet_in_the_middle_scan.py \
  --side desc --codes "\$CODES" --ledger "\$LEDGER" --dual2-ledger ~/chan_logs/scan_ledger.jsonl --dual2-dir . \
  --share-mode rsync \
  --share-remote chanlun-gdrive:mtim_ledger --vm B \
  > ~/chan_logs/mtim_postmarket_desc.log 2>&1 &
echo "[VM-B launch] desc PID=\$!"
renice -n 0 -p \$! 2>/dev/null || true
REMOTE_EOF
fi

echo "$(date +%H:%M:%S) [launch] done"
