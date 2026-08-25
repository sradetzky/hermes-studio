from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from studio_core.paths import StudioPaths


LOCAL_TRUSTED_HOSTS = ("127.0.0.1", "localhost", "testserver")


def _trusted_hosts(extra_hosts: str) -> tuple[str, ...]:
    hosts: list[str] = list(LOCAL_TRUSTED_HOSTS)
    for candidate in extra_hosts.split(","):
        host = candidate.strip().lower().rstrip(".")
        if not host:
            continue
        labels = host.split(".")
        if (
            len(host) > 253
            or any(not label or len(label) > 63 for label in labels)
            or any(label.startswith("-") or label.endswith("-") for label in labels)
            or any(not char.isascii() or not (char.isalnum() or char == "-")
                   for label in labels for char in label)
        ):
            raise ValueError(
                "HERMES_STUDIO_TRUSTED_HOSTS must contain exact DNS names "
                "without ports or wildcards")
        if host not in hosts:
            hosts.append(host)
    return tuple(hosts)


@dataclass(frozen=True)
class Settings:
    repo: Path
    studio_root: Path
    comfy_output: Path
    runtime_root: Path
    comfy_url: str = "http://127.0.0.1:8188"
    trusted_hosts: tuple[str, ...] = LOCAL_TRUSTED_HOSTS
    hermes_command: str = "hermes"
    studio_profile: str = "studio"
    specialist_profiles: tuple[str, ...] = (
        "studio-storyboarder",
        "studio-prompt-engineer",
        "studio-reviewer",
        "studio-illustrator",
    )
    job_timeout_seconds: int = 10_800
    worker_lease_timeout_seconds: int = 10
    max_reference_bytes: int = 256 * 1024 * 1024
    max_upload_files: int = 20
    runtime_paths: StudioPaths = field(default_factory=StudioPaths.from_environment)

    @property
    def web_root(self) -> Path:
        return self.repo / "webapp"

    @property
    def database_path(self) -> Path:
        return self.runtime_root / "studio.db"

    @property
    def profiles(self) -> tuple[str, ...]:
        return (self.studio_profile, *self.specialist_profiles)

    @property
    def paths(self) -> StudioPaths:
        return self.runtime_paths

    @property
    def real_home(self) -> Path:
        return self.paths.real_home

    @property
    def hermes_root(self) -> Path:
        return self.paths.hermes_root

    @property
    def active_profile_home(self) -> Path:
        return self.paths.active_profile_home

    @property
    def comfy_root(self) -> Path:
        return self.paths.comfy_root

    def profile_home(self, profile: str) -> Path:
        return self.paths.profile_home(profile)

    def profile_state_path(self, profile: str) -> Path:
        return self.profile_home(profile) / "state.db"

    @classmethod
    def from_environment(cls) -> "Settings":
        repo = Path(__file__).resolve().parent.parent
        runtime_paths = StudioPaths.from_environment()
        studio_root = Path(
            os.environ.get("DESIGN_STUDIO_ROOT", repo / "studio-root")
        ).expanduser().resolve()
        return cls(
            repo=repo,
            studio_root=studio_root,
            comfy_output=runtime_paths.comfy_root / "output",
            runtime_root=repo / ".runtime",
            comfy_url=os.environ.get(
                "COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            trusted_hosts=_trusted_hosts(os.environ.get(
                "HERMES_STUDIO_TRUSTED_HOSTS", "")),
            job_timeout_seconds=int(os.environ.get(
                "HERMES_STUDIO_JOB_TIMEOUT_SECONDS", "10800")),
            runtime_paths=runtime_paths,
        )
