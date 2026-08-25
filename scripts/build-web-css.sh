#!/usr/bin/env bash
# Rebuild the local, committed Tailwind stylesheet used by Hermes Studio.
set -euo pipefail
cd "$(dirname "$0")/.."
npm ci --include=dev --ignore-scripts --no-audit --no-fund >/dev/null
# The lock pins the latest available dataset; the host clock is ahead of its
# release timestamp, so Browserslist's age heuristic would still warn.
export BROWSERSLIST_IGNORE_OLD_DATA=true
exec ./node_modules/.bin/tailwindcss \
  -i webapp/styles.css \
  -o webapp/static/studio.css \
  --minify \
  --content 'webapp/static/index.html,webapp/static/*.js'
