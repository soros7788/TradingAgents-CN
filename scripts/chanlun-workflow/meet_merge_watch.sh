#!/bin/bash
# meet_merge_watch.sh — 持久化 C4: 双 VM worker 双终后合并 → 候选 JSON
# 跑在 VM-A (VM-A→VM-B SSH 已通), 合并在 VM-B 账本宿主执行
# 治理: 仅读账本/写候选 JSON; pgrep 防重由启动器负责; 12h 超时保护
set -u

# —— VM-B 侧路径 (经 SSH 引用) ——
VM_B=katelolita7788@35.212.190.147
B_PROJ=/home/katelolita7788/TradingAgents-CN
B_WF=$B_PROJ/scripts/chanlun-workflow
B_PY=$B_PROJ/venv_chan/bin/python
B_SCHED=$B_WF/meet_in_the_middle_scan.py
B_LEDGER=/home/katelolita7788/chan_logs/scan_ledger.jsonl
B_MERGED=${B_LEDGER}.merged
B_DATE=$(date +%Y%m%d)
B_OUT=/home/katelolita7788/chan_logs/candidates_${B_DATE}.json
B_ENRICH=/home/katelolita7788/chan_logs/candidates_enriched.json
B_LOG=/home/katelolita7788/chan_logs/merge_${B_DATE}.log

MAX_WAIT=43200   # 12h
SLEEP=300

# —— 已合并本会话 → 跳过 ——
if ssh -o BatchMode=yes -o ConnectTimeout=15 "$VM_B" "test -f $B_MERGED" 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] already merged this session -> skip"
  exit 0
fi
# —— 账本空 → 跳过 ——
if ! ssh -o BatchMode=yes -o ConnectTimeout=15 "$VM_B" "test -s $B_LEDGER" 2>/dev/null; then
  echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] ledger empty -> skip"
  exit 0
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] waiting for both workers (max $((MAX_WAIT/3600))h)..."
elapsed=0
while [ $elapsed -lt $MAX_WAIT ]; do
  A=$(pgrep -f "[m]eet_in_the_middle_scan.py.*asc" | grep -v bash | head -1)
  B=$(ssh -o BatchMode=yes -o ConnectTimeout=15 "$VM_B" \
      "pgrep -f '[m]eet_in_the_middle_scan.py.*desc' | grep -v bash | head -1" 2>/dev/null)
  if [ -z "$A" ] && [ -z "$B" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] both done -> merge on VM-B"
    ssh -o BatchMode=yes -o ConnectTimeout=60 "$VM_B" \
      "$B_PY $B_SCHED merge --ledger-local $B_LEDGER --json-out $B_OUT > $B_LOG 2>&1; touch $B_MERGED" \
      2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] candidates -> $B_OUT"
    # 富化: VM-B 本地读 kline_cache 补当前价 → candidates_enriched.json (供沙箱侧同步资料库)
    ssh -o BatchMode=yes -o ConnectTimeout=60 "$VM_B" \
      "$B_PY $B_WF/enrich_candidates.py >> $B_LOG 2>&1" 2>&1
    echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] enriched -> $B_ENRICH (沙箱侧 candidates_to_library.py 拉取)"
    exit 0
  fi
  sleep $SLEEP
  elapsed=$((elapsed+SLEEP))
done
echo "$(date '+%Y-%m-%d %H:%M:%S') [merge-watch] TIMEOUT -> manual check needed"
exit 1
