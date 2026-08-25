#!/usr/bin/env bash
# Install the current checkout as a user-level systemd web service.
set -euo pipefail
umask 077

usage() {
  echo "usage: $0 [--enable]" >&2
}

ENABLE=0
case "${1:-}" in
  "") ;;
  --enable) ENABLE=1 ;;
  *) usage; exit 2 ;;
esac
[[ $# -le 1 ]] || { usage; exit 2; }

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT/webapp/run.sh"
UNIT_SOURCE="$ROOT/webapp/hermes-studio.service"
[[ -x "$RUNNER" ]] || { echo "missing executable launcher: $RUNNER" >&2; exit 1; }
[[ -f "$UNIT_SOURCE" ]] || { echo "missing service unit: $UNIT_SOURCE" >&2; exit 1; }

LOCAL_BIN="${HOME:?HOME is required}/.local/bin"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
UNIT_DIR="$CONFIG_HOME/systemd/user"
LAUNCHER="$LOCAL_BIN/hermes-studio-web"
UNIT_TARGET="$UNIT_DIR/hermes-studio.service"
PYTHON="${PYTHON:-python3}"

install -d -m700 "$LOCAL_BIN" "$UNIT_DIR" "$CONFIG_HOME/hermes-studio"
RUNNER="$RUNNER" LAUNCHER="$LAUNCHER" "$PYTHON" -c '
import os
import shlex
import tempfile
from pathlib import Path

runner = Path(os.environ["RUNNER"])
launcher = Path(os.environ["LAUNCHER"])
content = (
    "#!/usr/bin/env bash\n"
    "set -euo pipefail\n"
    f"exec {shlex.quote(str(runner))}\n"
)
descriptor, temporary = tempfile.mkstemp(prefix=f".{launcher.name}.", dir=launcher.parent)
try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o755)
    os.replace(temporary, launcher)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
if launcher.read_text(encoding="utf-8") != content:
    raise SystemExit("launcher read-back verification failed")
'
install -m600 "$UNIT_SOURCE" "$UNIT_TARGET"
cmp -s "$UNIT_SOURCE" "$UNIT_TARGET" || {
  echo "service unit read-back verification failed" >&2
  exit 1
}

systemctl --user daemon-reload
if ((ENABLE)); then
  systemctl --user enable --now hermes-studio.service
fi

printf 'verified launcher: %s -> %s\n' "$LAUNCHER" "$RUNNER"
printf 'verified service: %s\n' "$UNIT_TARGET"
if ((! ENABLE)); then
  echo "enable with: systemctl --user enable --now hermes-studio.service"
fi
