#!/usr/bin/env bash
# Gracefully stop the single Hermes Studio web server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/.runtime/webapp.pid"
DATABASE="$ROOT/.runtime/studio.db"
FORCE=false
if [[ "${1:-}" == "--force" ]]; then
  FORCE=true
elif [[ $# -gt 0 ]]; then
  echo "usage: $0 [--force]" >&2
  exit 2
fi

if [[ ! -f "$PIDFILE" ]]; then
  echo "Hermes Studio is not running"
  exit 0
fi

pid="$(cat "$PIDFILE")"
if [[ ! "$pid" =~ ^[0-9]+$ ]] || [[ ! -d "/proc/$pid" ]]; then
  rm -f "$PIDFILE"
  echo "Removed stale Hermes Studio pid file"
  exit 0
fi

command="$(tr '\0' ' ' < "/proc/$pid/cmdline")"
if [[ "$command" != *"webapp/run.sh"* ]]; then
  echo "Refusing to stop unrelated pid $pid: $command" >&2
  rm -f "$PIDFILE"
  exit 1
fi

if [[ "$FORCE" == false && -f "$DATABASE" ]]; then
  active="$($ROOT/.venv/bin/python - "$DATABASE" <<'PY'
import sqlite3
import sys
from contextlib import closing

with closing(sqlite3.connect(sys.argv[1])) as connection:
    rows = connection.execute(
        "SELECT id, project, profile, status FROM jobs "
        "WHERE status IN ('queued', 'running') ORDER BY created_at"
    ).fetchall()
for row in rows:
    print("\t".join(str(value) for value in row))
PY
)"
  if [[ -n "$active" ]]; then
    echo "Refusing to stop Hermes Studio while jobs are active:" >&2
    while IFS=$'\t' read -r job project profile status; do
      printf '  %s  %s  %s  %s\n' "$job" "$project" "$profile" "$status" >&2
    done <<< "$active"
    echo "Wait for completion or run $0 --force to terminate active work." >&2
    exit 1
  fi
fi

timeout="${HERMES_STUDIO_STOP_TIMEOUT_SECONDS:-240}"
if [[ ! "$timeout" =~ ^[1-9][0-9]*$ ]]; then
  echo "HERMES_STUDIO_STOP_TIMEOUT_SECONDS must be a positive integer" >&2
  exit 2
fi
service_pid="$(systemctl --user show hermes-studio.service \
  --property=MainPID --value 2>/dev/null || true)"
if [[ "$service_pid" == "$pid" ]] && \
   systemctl --user is-active --quiet hermes-studio.service 2>/dev/null; then
  systemctl --user stop hermes-studio.service
else
  kill -TERM "$pid"
fi
for ((attempt = 0; attempt < timeout * 10; attempt++)); do
  [[ ! -d "/proc/$pid" ]] && break
  sleep .1
done
if [[ -d "/proc/$pid" ]]; then
  echo "Hermes Studio did not stop within ${timeout} seconds" >&2
  exit 1
fi
rm -f "$PIDFILE"
echo "Hermes Studio stopped"
