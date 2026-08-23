#!/usr/bin/env bash
# Single-instance Hermes Studio web UI on http://127.0.0.1:8788
set -euo pipefail
umask 077

if [[ $# -gt 0 ]]; then
  echo "usage: $0" >&2
  exit 2
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUNTIME="$ROOT/.runtime"
LOCK="$RUNTIME/webapp.lock"
PIDFILE="$RUNTIME/webapp.pid"
if [[ -L "$RUNTIME" ]]; then
  echo "refusing symlinked runtime directory: $RUNTIME" >&2
  exit 1
fi
mkdir -p "$RUNTIME"
chmod 700 "$RUNTIME"
cd "$ROOT"

exec 9>"$LOCK"
if ! flock -n 9; then
  existing="$(cat "$PIDFILE" 2>/dev/null || true)"
  echo "Hermes Studio is already running${existing:+ (pid $existing)}" >&2
  exit 1
fi

echo "$$" > "$PIDFILE"
child=""
cleanup() {
  trap - EXIT INT TERM
  if [[ -n "$child" ]] && kill -0 "$child" 2>/dev/null; then
    kill -TERM "$child" 2>/dev/null || true
    wait "$child" 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
}
trap cleanup EXIT INT TERM

.venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788 &
child=$!
wait "$child"
status=$?
child=""
exit "$status"
