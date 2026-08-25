#!/usr/bin/env python3
"""Run unittest discovery and fail on leaked-resource diagnostics."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_FAILURE_MARKERS = ("ResourceWarning:", "Exception ignored")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--start-directory", default="tests",
        help="unittest discovery start directory (default: tests)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    start_directory = Path(args.start_directory)
    command = [
        sys.executable,
        "-W", "always::ResourceWarning",
        "-m", "unittest", "discover",
        "-s", str(start_directory),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    output = completed.stdout
    sys.stdout.write(output)
    if completed.returncode:
        return completed.returncode
    failures = [
        line for line in output.splitlines()
        if any(marker in line for marker in _FAILURE_MARKERS)
    ]
    if failures:
        print("strict test gate rejected leaked-resource diagnostics:",
              file=sys.stderr)
        for line in failures:
            print(f"  {line}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
