from __future__ import annotations

import argparse
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _explicit_path(
        environment: Mapping[str, str], name: str, fallback: Path) -> Path:
    if name not in environment:
        return fallback
    value = environment[name]
    if not value:
        raise ValueError(f"{name} may not be empty")
    return Path(value)


@dataclass(frozen=True)
class StudioPaths:
    """Canonical account, Hermes fleet/profile, and ComfyUI roots."""

    real_home: Path
    hermes_root: Path
    active_profile_home: Path
    comfy_root: Path

    @property
    def profiles_root(self) -> Path:
        return self.hermes_root / "profiles"

    def profile_home(self, profile: str) -> Path:
        if profile == "default":
            return self.hermes_root
        candidate = Path(profile)
        if (
            not profile
            or candidate.name != profile
            or profile in {".", ".."}
        ):
            raise ValueError(f"invalid Hermes profile name: {profile!r}")
        return self.profiles_root / profile

    @classmethod
    def from_environment(
            cls,
            environment: Mapping[str, str] | None = None,
            *,
            account_home: Path | None = None,
    ) -> StudioPaths:
        source = os.environ if environment is None else environment
        fallback_home = account_home if account_home is not None else Path.home()
        real_home = _explicit_path(source, "HERMES_REAL_HOME", fallback_home)
        active_profile_home = _explicit_path(
            source, "HERMES_HOME", real_home / ".hermes")
        if (
            active_profile_home.parent.name == "profiles"
            and active_profile_home.name not in {"", ".", ".."}
        ):
            hermes_root = active_profile_home.parent.parent
        else:
            hermes_root = active_profile_home
        comfy_root = _explicit_path(
            source, "COMFYUI_PATH", real_home / "ComfyUI")
        return cls(
            real_home=real_home,
            hermes_root=hermes_root,
            active_profile_home=active_profile_home,
            comfy_root=comfy_root,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Resolve canonical Studio paths")
    parser.add_argument("path", choices=(
        "real-home", "hermes-root", "active-profile-home", "profiles-root",
        "comfy-root",
    ))
    args = parser.parse_args(argv)
    paths = StudioPaths.from_environment()
    values = {
        "real-home": paths.real_home,
        "hermes-root": paths.hermes_root,
        "active-profile-home": paths.active_profile_home,
        "profiles-root": paths.profiles_root,
        "comfy-root": paths.comfy_root,
    }
    print(values[args.path])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
