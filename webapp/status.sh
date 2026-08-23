#!/usr/bin/env bash
# Report single-instance Hermes Studio server state.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/.runtime/webapp.pid"
if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE")"
  if [[ "$pid" =~ ^[0-9]+$ ]] && [[ -d "/proc/$pid" ]]; then
    command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
    if [[ "$command" == *"webapp/run.sh"* ]]; then
      echo "Hermes Studio running (pid $pid) — http://127.0.0.1:8788"
      exit 0
    fi
  fi
fi
echo "Hermes Studio not running"
exit 1
