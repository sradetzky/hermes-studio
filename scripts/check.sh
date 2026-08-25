#!/usr/bin/env bash
# Run every local non-GPU Hermes Studio release check.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"

[[ -x "$PYTHON" ]] || {
  echo "missing project Python: $PYTHON" >&2
  echo "create .venv and install requirements-dev.txt first" >&2
  exit 1
}
command -v node >/dev/null || {
  echo "node is required for frontend checks" >&2
  exit 1
}
command -v git >/dev/null || {
  echo "git is required for archive checks" >&2
  exit 1
}

cd "$ROOT"
if [[ -n "$(git status --porcelain --untracked-files=normal)" ]]; then
  echo "release checks require a clean tracked and untracked worktree" >&2
  git status --short >&2
  exit 1
fi

archive_dir="$(mktemp -d /tmp/hermes-studio-check-XXXXXX)"
cleanup() {
  rm -rf "$archive_dir"
}
trap cleanup EXIT

echo "== source archive =="
git archive --format=tar --output="$archive_dir/source.tar" HEAD
sha256sum "$archive_dir/source.tar"
mkdir "$archive_dir/source"
tar -xf "$archive_dir/source.tar" -C "$archive_dir/source"
ARCHIVE_SOURCE="$archive_dir/source" "$PYTHON" -c '
import os
from pathlib import Path

root = Path(os.environ["ARCHIVE_SOURCE"])
for excluded in (".venv", ".runtime"):
    if (root / excluded).exists():
        raise SystemExit(f"private/runtime path present in source archive: {excluded}")
projects = root / "studio-root/projects"
if projects.exists() and {entry.name for entry in projects.iterdir()} - {".gitkeep"}:
    raise SystemExit("project data present in source archive")
'

cd "$archive_dir/source"
echo "== Python tests =="
env -u PYTHONPATH "$PYTHON" scripts/run_python_tests.py

echo "== frontend and Chromium tests =="
PYTHON="$PYTHON" node --test \
  tests/test_frontend_contracts.mjs \
  tests/test_frontend_dom.mjs \
  tests/test_frontend_browser.mjs

echo "== Python compilation =="
env -u PYTHONPATH "$PYTHON" -m compileall -q studio_core webapp scripts tests

echo "== Python correctness lint =="
env -u PYTHONPATH "$PYTHON" -m ruff check studio_core webapp scripts tests

echo "== JavaScript syntax =="
for file in webapp/static/*.js webapp/static/*.mjs; do
  node --check "$file"
done

cd "$ROOT"
echo "== Python dependency consistency =="
"$PYTHON" scripts/check_dependency_lock.py
"$PYTHON" -m pip check

echo "== Python dependency audit =="
"$PYTHON" -m pip_audit \
  --cache-dir "$archive_dir/pip-audit-cache" \
  --progress-spinner=off \
  -r requirements-lock.txt

echo "== external tool contracts =="
"$PYTHON" scripts/check_tool_versions.py

echo "== profile drift =="
scripts/sync-profiles.sh --check

echo "== reproducible CSS =="
css_before="$(sha256sum webapp/static/studio.css)"
scripts/build-web-css.sh
css_after="$(sha256sum webapp/static/studio.css)"
if [[ "$css_before" != "$css_after" ]]; then
  echo "webapp/static/studio.css is stale; rebuild and commit it" >&2
  exit 1
fi
printf '%s\n' "$css_after"

echo "== repository integrity =="
git diff --check
git diff --exit-code
echo "all local non-GPU checks passed"
