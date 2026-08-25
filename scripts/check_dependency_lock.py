#!/usr/bin/env python3
"""Verify installed and direct Python requirements against the lock."""

from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent


def normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("-r "):
            continue
        requirement = Requirement(line)
        specifiers = list(requirement.specifier)
        if (
            requirement.url is not None
            or requirement.marker is not None
            or len(specifiers) != 1
            or specifiers[0].operator != "=="
        ):
            raise RuntimeError(f"{path}:{number}: requirement is not exactly pinned")
        result[normalized(requirement.name)] = specifiers[0].version
    return result


def main() -> int:
    locked = pins(ROOT / "requirements-lock.txt")
    direct = {
        **pins(ROOT / "requirements.txt"),
        **pins(ROOT / "requirements-dev.txt"),
    }
    mismatched_direct = {
        name: (version, locked.get(name))
        for name, version in direct.items()
        if locked.get(name) != version
    }
    if mismatched_direct:
        raise RuntimeError(
            f"direct requirements disagree with requirements-lock.txt: {mismatched_direct}")
    installed = {
        normalized(distribution.metadata["Name"]): distribution.version
        for distribution in importlib.metadata.distributions()
        if distribution.metadata["Name"]
    }
    mismatched_installed = {
        name: (version, installed.get(name))
        for name, version in locked.items()
        if installed.get(name) != version
    }
    if mismatched_installed:
        raise RuntimeError(
            f"installed environment disagrees with requirements-lock.txt: "
            f"{mismatched_installed}")
    print(
        f"dependency lock matches {len(direct)} direct and "
        f"{len(locked)} installed packages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
