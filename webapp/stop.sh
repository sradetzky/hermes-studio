#!/usr/bin/env bash
# Gracefully stop the single Hermes Studio web server.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PIDFILE="$ROOT/.runtime/webapp.pid"

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

kill -TERM "$pid"
for _ in {1..100}; do
  [[ ! -d "/proc/$pid" ]] && break
  sleep .05
done
if [[ -d "/proc/$pid" ]]; then
  echo "Hermes Studio did not stop within 5 seconds" >&2
  exit 1
fi
rm -f "$PIDFILE"
echo "Hermes Studio stopped"
