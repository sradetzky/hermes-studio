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

cmd="${1:-show}"
case "$cmd" in
  show)
    echo "Fleet model status:"
    for p in "${FLEET[@]}"; do
      cfg="$HOME/.hermes/profiles/$p/config.yaml"
      if [[ -f "$cfg" ]]; then
        m=$(grep -m1 'default:' "$cfg" | sed 's/.*default:[[:space:]]*//')
        pr=$(grep -m1 'provider:' "$cfg" | sed 's/.*provider:[[:space:]]*//')
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
    for p in "${targets[@]}"; do
      cfg="$HOME/.hermes/profiles/$p/config.yaml"
      if [[ ! -f "$cfg" ]]; then
        echo "skip $p (no profile at $cfg)" >&2
        continue
      fi
      # Set both fields explicitly; provider-specific auth remains profile-owned.
      python3 - "$cfg" "$provider" "$model" <<'EOF'
import re, sys
path, provider, model = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(path).read()
def setkey(text, key, value):
    pat = re.compile(rf"^(\s*){key}:[^\n]*$", re.M)
    if pat.search(text):
        return pat.sub(rf"\g<1>{key}: {value}", text, count=1)
    if re.search(r"^model:\s*$", text, re.M):
        return re.sub(r"^model:\s*$", f"model:\n  {key}: {value}", text, count=1)
    return text + f"\nmodel:\n  {key}: {value}\n"
text = setkey(text, "provider", provider)
text = setkey(text, "default", model)
open(path, "w").write(text)
print(f"  {path}: provider={provider} default={model}")
EOF
    done
    echo "Done. Restart any running gateways/sessions to pick up the new model."
    ;;
esac
