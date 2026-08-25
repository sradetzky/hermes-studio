#!/usr/bin/env bash
# switch-model.sh — point ALL studio-agent profiles at one model/provider.
#
# The studio runs a fleet of full Hermes profiles (studio + subagent profiles
# like studio-storyboarder). All of them share the same model config so the
# whole fleet switches together.
#
# Usage:
#   scripts/switch-model.sh <provider> <model>            # apply to all
#   scripts/switch-model.sh <provider> <model> <profile>  # apply to one
#   scripts/switch-model.sh show                          # current state
#
# Examples:
#   scripts/switch-model.sh openrouter stealth/ox-alpha
#   scripts/switch-model.sh openrouter deepseek/deepseek-v4-flash-0731

set -euo pipefail

# Managed fleet: keep in sync with hermes/profiles/ in this repo.
FLEET=(studio studio-storyboarder studio-prompt-engineer studio-reviewer studio-illustrator)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
HERMES_ROOT="$(
  PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON" -m studio_core.paths hermes-root
)"
HERMES_PROFILES="$HERMES_ROOT/profiles"

cmd="${1:-show}"
case "$cmd" in
  show)
    echo "Fleet model status:"
    for p in "${FLEET[@]}"; do
      cfg="$HERMES_PROFILES/$p/config.yaml"
      if [[ -f "$cfg" ]]; then
        readarray -t values < <("$PYTHON" - "$cfg" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8")
for key in ("provider", "default"):
    match = re.search(rf"^\s*{key}:\s*(.*?)\s*$", text, re.M)
    print(match.group(1) if match else "(unset)")
PY
        )
        pr="${values[0]}"
        m="${values[1]}"
        printf '  %-22s %-12s %s\n' "$p" "$pr" "$m"
      else
        printf '  %-22s (no profile)\n' "$p"
      fi
    done
    ;;
  *)
    provider="$1"; model="${2:?usage: switch-model.sh <provider> <model> [profile]}"
    targets=("${FLEET[@]}")
    [[ -n "${3:-}" ]] && targets=("$3")
    eligible=()
    changed=()
    missing=()
    for p in "${targets[@]}"; do
      cfg="$HERMES_PROFILES/$p/config.yaml"
      if [[ ! -f "$cfg" ]]; then
        echo "skip $p (no profile at $cfg)" >&2
        missing+=("$p")
        continue
      fi
      eligible+=("$p")
      # Set both fields explicitly; provider-specific auth remains profile-owned.
      result="$("$PYTHON" - "$cfg" "$provider" "$model" <<'PY'
import os
import re
import sys
import tempfile
from pathlib import Path

path = Path(sys.argv[1])
provider, model = sys.argv[2], sys.argv[3]
text = path.read_text(encoding="utf-8")
def setkey(text, key, value):
    pat = re.compile(rf"^(\s*){key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(
            lambda match: f"{match.group(1)}{key}: {value}", text, count=1)
    if re.search(r"^model:\s*$", text, re.M):
        return re.sub(r"^model:\s*$", f"model:\n  {key}: {value}", text, count=1)
    return text + f"\nmodel:\n  {key}: {value}\n"
updated = setkey(setkey(text, "provider", provider), "default", model)
if updated == text:
    print("unchanged")
else:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    print("changed")
PY
      )"
      if [[ "$result" == "changed" ]]; then
        changed+=("$p")
      elif [[ "$result" != "unchanged" ]]; then
        echo "unexpected model update result for $p: $result" >&2
        exit 1
      fi
    done
    if ((${#eligible[@]} == 0)); then
      printf 'no intended profiles could be updated: %s\n' "${missing[*]}" >&2
      exit 1
    fi
    printf 'Changed profiles: %s\n' "${changed[*]:-(none)}"
    echo "Done. Restart any running gateways/sessions to pick up the new model."
    ;;
esac
