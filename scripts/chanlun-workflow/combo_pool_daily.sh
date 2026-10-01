#!/bin/bash
# 组合池(周线顺 ∩ 日线底背驰) · 每日自动版
# 挂载点: 每交易日 09:20 (在 08:20 顺势池 / 09:00 背驰池之后, 盘前三池就绪)
#
# 与顺势池(要同向) / 背驰池(要反向) 并列, 三池性质不同、不可混用:
#   组合池 = 周线向上(类R2顺) AND 日线底背驰(类R1转折) = 大顺+小背驰 区间套买点
#
# 选源: combo_pool.py 内部自动 union 最近 24h 内所有 dualscan_*.json 的 dual 记录
#        (按 code dedup 保最新, 跳过历史 union 产物), 绕开分批小 json 陷阱,
#        与 trend/beichi 两池策略一致。
# 数据源: dualscan json(R1 verdict+dlp) + weekly_nesting.analyze 实时算周线锚(读 ~/kline_cache_local, 零网络)
#
# 治理: 仅读 dualscan + 写 ~/chan_logs 产物; 不碰 sealed 模块。
set -u
HD=$HOME
WF=$HD/TradingAgents-CN/scripts/chanlun-workflow
LOGDIR=$HD/chan_logs
mkdir -p "$LOGDIR"

# 防重: pidfile + 存活检查(避免 cron 重入叠加)
# P2-7 修复 (2026-10-01): pidfile check-then-write 有 TOCTOU 竞态, 改用 flock(锁随 fd 释放)
LOCK="$LOGDIR/.combo_pool.lock"
exec 9>"$LOCK"
if ! flock -n 9; then
  echo "$(date '+%F %T') [combo-pool] 已有实例在运行 -> skip"
  exit 0
fi

cd "$WF" || exit 1
echo "$(date '+%F %T') [combo-pool] 启动"
python3 combo_pool.py
rc=$?
echo "$(date '+%F %T') [combo-pool] 结束 rc=$rc"
# P2-9 (2026-10-01): 清理 7 天前的 union 中间文件, 防 ~/chan_logs 堆积
find "$LOGDIR" -maxdepth 1 -name 'dualscan_union_*.json' -mtime +7 -delete 2>/dev/null || true
exit $rc
