#!/bin/bash
# watchdog.sh — Watch an Anvil run's supervisor; recover automatically if it dies.
#
# Usage: watchdog.sh [--state-dir DIR]
# Safe to run repeatedly (e.g. from cron every few minutes).
#
# State (default $ANVIL_RECOVERY_STATE or ~/.local/state/anvil-recovery):
#   ACTIVE_RUN  "<run-id> <run-dir>" of the run being watched
#   RECOVERED   run ids already recovered (never double-recover)
#   watchdog.log
#
# The active run is recorded by recover.sh. To watch a manually launched run,
# write "<run-id> <run-dir>" to $STATE/ACTIVE_RUN yourself.
set -euo pipefail

STATE_DIR="$HOME/.local/state/anvil-recovery"
while [ $# -gt 0 ]; do
  case "$1" in
    --state-dir) STATE_DIR="$2"; shift 2;;
    *) echo "unknown arg: $1"; exit 1;;
  esac
done
[ -n "${ANVIL_RECOVERY_STATE:-}" ] && STATE_DIR="$ANVIL_RECOVERY_STATE"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ACTIVE="$STATE_DIR/ACTIVE_RUN"
RECOVERED="$STATE_DIR/RECOVERED"

[ -f "$ACTIVE" ] || exit 0  # nothing to watch
read -r RUN_ID RUN_DIR < "$ACTIVE"
[ -n "${RUN_ID:-}" ] || exit 0

# Already recovered this run? Never double-recover.
if [ -f "$RECOVERED" ] && grep -q "^$RUN_ID$" "$RECOVERED"; then exit 0; fi

# Clean finish? Stop watching.
if [ -f "$RUN_DIR/run-stderr.log" ] && grep -q "RUN EXITED" "$RUN_DIR/run-stderr.log" 2>/dev/null; then
  echo "$(date -u +%FT%TZ) run $RUN_ID exited cleanly" >> "$STATE_DIR/watchdog.log"
  rm -f "$ACTIVE"
  exit 0
fi

# Supervisor process alive for this run dir?
if pgrep -f "anvil run" 2>/dev/null | xargs -I{} ps -o cmd= -p {} 2>/dev/null | grep -q "$(basename "$RUN_DIR")"; then
  exit 0
fi

# Dead: recover.
mkdir -p "$STATE_DIR"
echo "$(date -u +%FT%TZ) supervisor for run $RUN_ID appears dead; recovering" >> "$STATE_DIR/watchdog.log"
echo "$RUN_ID" >> "$RECOVERED"
RUNS_DIR_HINT="$(dirname "$RUN_DIR")"
"$SCRIPT_DIR/recover.sh" "$RUN_ID" --runs-dir "$RUNS_DIR_HINT" >> "$STATE_DIR/watchdog.log" 2>&1
echo "$(date -u +%FT%TZ) recovery launched for $RUN_ID" >> "$STATE_DIR/watchdog.log"
