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
    hermes_command: str = "hermes"
    studio_profile: str = "studio"
    job_timeout_seconds: int = 600
    worker_lease_timeout_seconds: int = 10
    max_reference_bytes: int = 256 * 1024 * 1024
    max_upload_files: int = 20

    @property
    def web_root(self) -> Path:
        return self.repo / "webapp"

    @property
    def database_path(self) -> Path:
        return self.runtime_root / "studio.db"

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
        )
