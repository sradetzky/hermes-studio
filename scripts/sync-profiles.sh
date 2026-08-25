#!/usr/bin/env bash
# Deploy repo-owned SOULs and skills to the live Hermes Studio profiles.
# Usage: scripts/sync-profiles.sh [--check]

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
HERMES_ROOT="$(
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m studio_core.paths hermes-root
)"
HERMES_PROFILES="$HERMES_ROOT/profiles"
MODE="${1:-sync}"
[[ "$MODE" == "sync" || "$MODE" == "--check" ]] || {
  echo "usage: $0 [--check]" >&2
  exit 2
}

drift=0

sync_file() {
  local source="$1" target="$2"
  if [[ ! -f "$source" ]]; then
    echo "missing repo source: $source" >&2
    exit 1
  fi
  if cmp -s "$source" "$target" 2>/dev/null; then
    printf 'ok      %s\n' "$target"
    return
  fi
  if [[ "$MODE" == "--check" ]]; then
    printf 'DRIFT   %s\n' "$target"
    drift=1
    return
  fi
  install -Dm644 "$source" "$target"
  printf 'updated %s\n' "$target"
}

profiles=(
  studio
  studio-storyboarder
  studio-prompt-engineer
  studio-reviewer
  studio-illustrator
)

for profile in "${profiles[@]}"; do
  live="$HERMES_PROFILES/$profile"
  [[ -f "$live/config.yaml" ]] || {
    echo "missing live profile: $live" >&2
    exit 1
  }
  sync_file "$ROOT/hermes/profiles/$profile/SOUL.md" "$live/SOUL.md"
  sync_file "$ROOT/hermes/skills/design-studio/SKILL.md" \
            "$live/skills/design-studio/SKILL.md"
done

sync_file "$ROOT/hermes/skills/krea2-images/SKILL.md" \
          "$HERMES_PROFILES/studio-illustrator/skills/krea2-images/SKILL.md"

# Cloud specialist: intentionally excluded from design-studio skill deployment
# and fleet-wide model switching; it stays fixed on Grok 4.6.
grok_live="$HERMES_PROFILES/studio-grok"
if [[ -f "$grok_live/config.yaml" ]]; then
  sync_file "$ROOT/hermes/profiles/studio-grok/SOUL.md" "$grok_live/SOUL.md"
  sync_file "$ROOT/hermes/skills/grok-research-imagine/SKILL.md" \
            "$grok_live/skills/grok-research-imagine/SKILL.md"
else
  echo "skipped optional studio-grok profile"
fi

exit "$drift"
