from __future__ import annotations

import json
import sys
from pathlib import Path

import uvicorn

from scripts import design_studio as ds
from webapp.app import create_app
from webapp.config import Settings


class PassiveManager:
    def __init__(self, _settings, store):
        self.store = store

    def start(self):
        self.store.initialize()

    def stop(self):
        pass

    def submit_project_chat(self, project, message, profile=None):
        return self.store.create_project_chat_job(
            project, message, profile or "studio")

    def submit_chat(self, project, clip_id, message, profile=None):
        return self.store.create_chat_job(
            project, message, profile or "studio", clip_id=clip_id)


def main() -> None:
    port = int(sys.argv[1])
    fixture = Path(sys.argv[2]).resolve()
    repo = Path(__file__).resolve().parent.parent
    studio_root = fixture / "studio"
    comfy_output = fixture / "comfy-output"
    runtime_root = fixture / "runtime"
    comfy_output.mkdir(parents=True)

    ds.create_project(studio_root, "Beta", "second project")
    alpha = ds.create_project(studio_root, "Alpha", "browser fixture")
    generation = alpha / "clips" / "clip-001" / "generations" / "001"
    generation.mkdir()
    (generation / "video.mp4").write_bytes(b"browser-fixture-video")
    (generation / "prompt.txt").write_text("fixture prompt\n", encoding="utf-8")
    (generation / "meta.json").write_text(
        json.dumps({"recipe": "r2v", "seed": 42}), encoding="utf-8")

    settings = Settings(
        repo=repo,
        studio_root=studio_root,
        comfy_output=comfy_output,
        runtime_root=runtime_root,
        job_timeout_seconds=5,
        max_reference_bytes=1024,
    )
    uvicorn.run(
        create_app(settings, PassiveManager),
        host="127.0.0.1",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
