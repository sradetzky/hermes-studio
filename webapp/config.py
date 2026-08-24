from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    repo: Path
    studio_root: Path
    comfy_output: Path
    runtime_root: Path
    comfy_url: str = "http://127.0.0.1:8188"
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
    def hermes_home(self) -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser()

    def profile_state_path(self, profile: str) -> Path:
        if profile == "default":
            return self.hermes_home / "state.db"
        return self.hermes_home / "profiles" / profile / "state.db"

    @classmethod
    def from_environment(cls) -> "Settings":
        repo = Path(__file__).resolve().parent.parent
        studio_root = Path(
            os.environ.get("DESIGN_STUDIO_ROOT", repo / "studio-root")
        ).expanduser().resolve()
        return cls(
            repo=repo,
            studio_root=studio_root,
            comfy_output=(Path.home() / "ComfyUI" / "output").resolve(),
            runtime_root=repo / ".runtime",
            comfy_url=os.environ.get(
                "COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/"),
            job_timeout_seconds=int(os.environ.get(
                "HERMES_STUDIO_JOB_TIMEOUT_SECONDS", "10800")),
        )
