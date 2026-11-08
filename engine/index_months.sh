#!/bin/zsh
# Index the library one month at a time, newest first, so search improves from recent to old.
#   ./index_months.sh 280 2026-07 2015-11     # tokens, first month, last month (inclusive)
# Each month is its own index.py run (resumable: already-indexed items are skipped).
# Stop with: pkill -f index_months.sh; pkill -f 'index.py --since'
set -u
tokens=${1:?tokens}; from=${2:?first month YYYY-MM}; to=${3:?last month YYYY-MM}
cd "${0:A:h}" || exit 1
log=../data/index-months.log
m=$from
while [[ "$m" > "$to" || "$m" == "$to" ]]; do
  next=$(date -j -v+1m -f "%Y-%m-%d" "$m-01" +%Y-%m-%d)
  free=$(df -g ../data | awk 'NR==2 {print $4}')
  if (( free < 20 )); then echo "$(date +%T) stop: only ${free} GB free" >> $log; exit 1; fi
  echo "$(date +%T) month $m start" >> $log
  # caffeinate: keep the Mac awake for this batch only
  if caffeinate -i uv run index.py --since "$m-01" --until "$next" --tokens "$tokens" >> $log 2>&1; then
    echo "\n$(date +%T) month $m done" >> $log
  else
    echo "\n$(date +%T) month $m FAILED (exit $?), continuing" >> $log
  fi
  m=$(date -j -v-1m -f "%Y-%m-%d" "$m-01" +%Y-%m)
done
echo "$(date +%T) all months done" >> $log
