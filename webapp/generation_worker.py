from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_core.generation_archive import load_running_generation_contract
from studio_core.job_contracts import JobEventType, JobPhase
from studio_core.job_store import JobStore
from webapp.generation_runner import GenerationJobRunner, GenerationRuntime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute one immutable Studio H3 generation contract")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--clip", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--studio-root", required=True, type=Path)
    parser.add_argument("--runtime-root", required=True, type=Path)
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument("--real-home", required=True, type=Path)
    parser.add_argument("--comfyui-root", required=True, type=Path)
    parser.add_argument("--comfyui-url", required=True)
    parser.add_argument("--comfyui-python", required=True, type=Path)
    parser.add_argument("--timeout-seconds", required=True, type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    contract = load_running_generation_contract(
        args.runtime_root, args.job_id, args.project, args.clip)
    os.environ.update({
        "DESIGN_STUDIO_ROOT": str(args.studio_root),
        "HERMES_STUDIO_RUNTIME_ROOT": str(args.runtime_root),
        "HERMES_STUDIO_JOB_ID": args.job_id,
        "HERMES_STUDIO_PROJECT": args.project,
        "HERMES_STUDIO_CLIP": args.clip,
        "HERMES_STUDIO_PROFILE": args.profile,
        "HERMES_STUDIO_JOB_KIND": "generate",
        "COMFYUI_URL": args.comfyui_url,
    })
    store = JobStore(args.runtime_root / "studio.db")

    def event(
            event_type: JobEventType, summary: str, *,
            phase: JobPhase = JobPhase.RUNNING,
            detail: dict | None = None) -> None:
        store.append_job_event(
            args.job_id,
            args.profile,
            event_type,
            summary,
            phase=phase,
            detail=detail,
        )

    runner = GenerationJobRunner(
        GenerationRuntime(
            studio_root=args.studio_root,
            runtime_root=args.runtime_root,
            profile_home=args.profile_home,
            real_home=args.real_home,
            comfy_root=args.comfyui_root,
            comfy_url=args.comfyui_url,
            comfy_python=args.comfyui_python,
            timeout_seconds=args.timeout_seconds,
        ),
        event_callback=event,
    )
    result = runner.run(args.job_id, args.project, args.clip, contract)
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
