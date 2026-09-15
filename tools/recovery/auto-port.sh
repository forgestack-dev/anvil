#!/bin/bash
# auto-port.sh — Port filed-but-unintegrated implementations into a new run's handoffs.
#
# Usage: auto-port.sh <new-run-id> <port-manifest> [state-base]
# Manifest lines: <ticket-id>:<old-worktree-path>
#
# Waits for each ticket's new worker handoff to appear, then applies the old
# worktree's tracked diff plus any untracked files. Tickets whose diff does
# not apply cleanly are reported and left for manual porting.
set -euo pipefail

NEW_RUN_ID="${1:?usage: auto-port.sh <new-run-id> <manifest> [state-base]}"
MANIFEST="${2:?usage: auto-port.sh <new-run-id> <manifest> [state-base]}"
STATE_BASE="${3:-$HOME/.local/state/anvil}"
NEW_STATE="$STATE_BASE/$NEW_RUN_ID"

echo "=== auto-port for run $NEW_RUN_ID ==="
while IFS=: read -r tid old_ws; do
  [ -z "$tid" ] && continue
  echo "waiting for ticket $tid handoff..."
  new_aid=""
  for i in $(seq 1 180); do  # up to 30 min
    sleep 10
    for d in "$NEW_STATE"/artifacts/*/; do
      [ -d "$d/worker" ] || continue
      if grep -q '"id": "'"$tid"'"' "$d/worker/prompt.txt" 2>/dev/null; then
        new_aid=$(basename "$d"); break 2
      fi
    done
  done
  if [ -z "$new_aid" ]; then echo "TIMEOUT waiting for ticket $tid handoff"; continue; fi
  new_ws=$(sqlite3 "$NEW_STATE/state.sqlite" "SELECT workspace FROM attempts WHERE task_id='$tid' ORDER BY started_at DESC LIMIT 1;")
  if [ -z "$new_ws" ] || [ ! -d "$new_ws" ]; then echo "no worktree for $tid"; continue; fi
  echo "porting ticket $tid -> $new_ws"
  patch="/tmp/anvil-port-$tid.patch"
  ( cd "$old_ws" && git diff HEAD > "$patch" )
  if git -C "$new_ws" apply --check "$patch" 2>/dev/null; then
    git -C "$new_ws" apply "$patch" && echo "tracked files applied"
  else
    echo "PATCH CONFLICT for ticket $tid — manual port needed"
    continue
  fi
  ( cd "$old_ws" && git status --porcelain | grep '^??' | cut -c4- ) | while read -r f; do
    mkdir -p "$new_ws/$(dirname "$f")"
    cp -r "$old_ws/$f" "$new_ws/$f"
  done
  echo "ticket $tid ported"
done < "$MANIFEST"
echo "=== auto-port done ==="
