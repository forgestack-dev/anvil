#!/bin/bash
# recover.sh — Recover an Anvil run whose supervisor died (e.g. VM restart).
#
# Anvil has no resume: after a supervisor death, this script launches a new
# run covering the remaining tickets and auto-ports any implementations that
# were filed but never integrated/reviewed.
#
# Usage: recover.sh <dead-run-id> [--runs-dir DIR] [--state-dir DIR]
#
# Environment:
#   ANVIL_BIN        path to the anvil executable (default: anvil on PATH)
#   Any environment the run needs (e.g. DATABASE_URL for DB-backed
#   verification) must be exported by the caller; the relaunched supervisor
#   inherits it.
set -euo pipefail

DEAD_RUN_ID="${1:?usage: recover.sh <dead-run-id> [--runs-dir DIR] [--state-dir DIR]}"
shift
RUNS_DIR="$HOME/workspace/anvil-runs"
STATE_DIR_OVERRIDE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --runs-dir) RUNS_DIR="$2"; shift 2;;
    --state-dir) STATE_DIR_OVERRIDE="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done

ANVIL_BIN="${ANVIL_BIN:-anvil}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RECOVERY_STATE="${ANVIL_RECOVERY_STATE:-$HOME/.local/state/anvil-recovery}"

echo "=== Recovering dead run $DEAD_RUN_ID ==="

# 1. Locate the dead run's directory
DEAD_RUN_DIR=""
for d in "$RUNS_DIR"/*/; do
  if [ -f "$d/run-stderr.log" ] && grep -q "$DEAD_RUN_ID" "$d/run-stderr.log" 2>/dev/null; then
    DEAD_RUN_DIR="$d"; break
  fi
done
if [ -z "$DEAD_RUN_DIR" ]; then echo "cannot find run dir for $DEAD_RUN_ID under $RUNS_DIR"; exit 1; fi
echo "dead run dir: $DEAD_RUN_DIR"

# 2. Safety: supervisor must actually be dead, and the run must not have exited cleanly
if pgrep -f "anvil run" 2>/dev/null | xargs -I{} ps -o cmd= -p {} 2>/dev/null | grep -q "$(basename "$DEAD_RUN_DIR")"; then
  echo "an anvil supervisor for this run still appears alive; aborting"
  exit 1
fi
if grep -q "RUN EXITED" "$DEAD_RUN_DIR/run-stderr.log" 2>/dev/null; then
  echo "run exited cleanly; nothing to recover"
  exit 0
fi

# 3. Read config + task statuses
DEAD_CFG="$DEAD_RUN_DIR/run.json"
STATE_BASE="$(python3 -c "import json;print(json.load(open('$DEAD_CFG')).get('state_dir','$HOME/.local/state/anvil'))")"
[ -n "$STATE_DIR_OVERRIDE" ] && STATE_BASE="$STATE_DIR_OVERRIDE"
DEAD_STATE="$STATE_BASE/$DEAD_RUN_ID"
[ -f "$DEAD_STATE/state.sqlite" ] || { echo "no state.sqlite at $DEAD_STATE"; exit 1; }
DONE_TASKS=$(sqlite3 "$DEAD_STATE/state.sqlite" "SELECT id FROM tasks WHERE status='done';" | tr '\n' ' ')
CANDIDATE_TASKS=$(sqlite3 "$DEAD_STATE/state.sqlite" "SELECT id FROM tasks WHERE status='candidate';" | tr '\n' ' ')
echo "done: ${DONE_TASKS:-<none>}"
echo "candidate (filed, unintegrated): ${CANDIDATE_TASKS:-<none>}"

# 4. New base = dead run's integration HEAD; check out the base repo there
BASE_REPO="$(python3 -c "import json;print(json.load(open('$DEAD_CFG'))['repo'])")"
NEW_BASE=$(git -C "$DEAD_STATE/integration" rev-parse HEAD)
echo "new base: $NEW_BASE"
git -C "$BASE_REPO" checkout -q "$NEW_BASE"

# 5. Port manifest for candidate tasks (ticket -> old worktree)
PORT_MANIFEST=""
for tid in $CANDIDATE_TASKS; do
  ws=$(sqlite3 "$DEAD_STATE/state.sqlite" "SELECT workspace FROM attempts WHERE task_id='$tid' ORDER BY started_at DESC LIMIT 1;")
  if [ -n "$ws" ] && [ -d "$ws" ]; then
    PORT_MANIFEST="$PORT_MANIFEST$tid:$ws
"
    echo "will port ticket $tid from $ws"
  fi
done

# 6. New run dir: increment trailing number, else append -recovery-N
BASE_NAME="$(basename "$DEAD_RUN_DIR")"
if [[ "$BASE_NAME" =~ ^(.*[^0-9])([0-9]+)$ ]]; then
  STEM="${BASH_REMATCH[1]}"; N=$((10#${BASH_REMATCH[2]} + 1))
  NEW_RUN_DIR="$RUNS_DIR/$STEM$N"
else
  N=1
  while [ -e "$RUNS_DIR/$BASE_NAME-recovery-$N" ]; do N=$((N+1)); done
  NEW_RUN_DIR="$RUNS_DIR/$BASE_NAME-recovery-$N"
fi
mkdir -p "$NEW_RUN_DIR"
python3 << EOF
import json
with open('$DEAD_RUN_DIR/tickets.json') as f:
    data = json.load(f)
done = set('$DONE_TASKS'.split())
remaining = [t for t in data['tasks'] if t['id'] not in done]
for t in remaining:
    t['depends_on'] = [d for d in t.get('depends_on', []) if d not in done]
data['tasks'] = remaining
with open('$NEW_RUN_DIR/tickets.json', 'w') as f:
    json.dump(data, f, indent=2)
print("tickets: " + str([t['id'] for t in remaining]))
EOF

# 7. run.json: copy config, point at new tickets file
python3 << EOF
import json
with open('$DEAD_CFG') as f:
    cfg = json.load(f)
cfg['tickets'] = '$NEW_RUN_DIR/tickets.json'
with open('$NEW_RUN_DIR/run.json', 'w') as f:
    json.dump(cfg, f, indent=2)
print("run.json written")
EOF

# 8. Launch the new run
echo -n "$PORT_MANIFEST" > "$NEW_RUN_DIR/port-manifest.txt"
cd "$NEW_RUN_DIR"
nohup "$ANVIL_BIN" run run.json > run-stdout.log 2> run-stderr.log &
echo "launched new run from $NEW_RUN_DIR"

# 9. Wait for the new run id, record it, background the auto-port waiter
NEW_RUN_ID=""
for i in $(seq 1 60); do
  sleep 10
  NEW_RUN_ID=$(grep -oE '^Run [0-9a-f]{32}' run-stderr.log 2>/dev/null | head -1 | cut -d' ' -f2 || true)
  [ -n "$NEW_RUN_ID" ] && break
done
if [ -z "$NEW_RUN_ID" ]; then echo "could not determine new run id"; exit 1; fi
mkdir -p "$RECOVERY_STATE"
echo "$NEW_RUN_ID $NEW_RUN_DIR" > "$RECOVERY_STATE/ACTIVE_RUN"
echo "new run id: $NEW_RUN_ID"
"$SCRIPT_DIR/auto-port.sh" "$NEW_RUN_ID" "$NEW_RUN_DIR/port-manifest.txt" "$STATE_BASE" &
echo "auto-port waiter backgrounded"
echo "=== recovery launched ==="
