#!/usr/bin/env python3
"""Verify the external CLI contracts Hermes Studio executes."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""} and str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.submit_h3_graph_mcp import COMFYUI_MCP, MCPORTER

MIN_HERMES_VERSION = (0, 20, 5)
REQUIRED_CHAT_OPTIONS = (
    "--query", "--quiet", "--toolsets", "--resume", "--source",
)


def _run(arguments: Sequence[str]) -> str:
    completed = subprocess.run(
        list(arguments),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(
            f"external tool check failed: {' '.join(arguments)}: {detail}")
    return completed.stdout


def check_hermes() -> str:
    output = _run(["hermes", "--version"])
    match = re.search(r"Hermes Agent v(\d+)\.(\d+)\.(\d+)", output)
    if not match:
        raise RuntimeError("could not parse `hermes --version` output")
    version = tuple(int(part) for part in match.groups())
    if version < MIN_HERMES_VERSION:
        required = ".".join(str(part) for part in MIN_HERMES_VERSION)
        actual = ".".join(str(part) for part in version)
        raise RuntimeError(
            f"Hermes Agent >= {required} is required; found {actual}. "
            "Run `hermes update` and re-run scripts/check.sh.")
    help_text = _run(["hermes", "chat", "--help"])
    missing = [option for option in REQUIRED_CHAT_OPTIONS if option not in help_text]
    if missing:
        raise RuntimeError(
            "installed Hermes chat CLI lacks required options: " + ", ".join(missing))
    return ".".join(str(part) for part in version)


def check_mcp_packages() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package in (MCPORTER, COMFYUI_MCP):
        output = _run(["npm", "view", package, "version", "--json"])
        try:
            actual = json.loads(output)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"npm returned invalid version JSON for {package}") from exc
        expected = package.rsplit("@", 1)[1]
        if actual != expected:
            raise RuntimeError(
                f"pinned package {package} resolved as {actual!r}; expected {expected!r}")
        versions[package.rsplit("@", 1)[0]] = actual
    return versions


def main() -> int:
    hermes = check_hermes()
    packages = check_mcp_packages()
    print(f"Hermes Agent {hermes}: supported CLI contract")
    for package, version in packages.items():
        print(f"{package} {version}: pinned package available")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
