#!/usr/bin/env bash
# Studio web UI on http://127.0.0.1:8788
cd "$(dirname "$0")/.."
exec .venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788 "$@"
