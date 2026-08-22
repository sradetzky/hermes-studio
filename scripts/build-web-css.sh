#!/usr/bin/env bash
# Rebuild the local, committed Tailwind stylesheet used by Hermes Studio.
set -euo pipefail
cd "$(dirname "$0")/.."
exec npx -y tailwindcss@3.4.17 \
  -i webapp/styles.css \
  -o webapp/static/studio.css \
  --minify \
  --content 'webapp/static/index.html,webapp/static/app.js'
