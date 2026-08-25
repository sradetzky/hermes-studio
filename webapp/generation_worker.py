from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_core.job_contracts import (
    GenerationJobPayload,
    JobEventType,
    JobPhase,
)
from studio_core.job_store import JobStore
from studio_core.models import JobStatus
from webapp.generation_runner import GenerationJobRunner, GenerationRuntime


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Execute one immutable Studio H3 generation contract")
    parser.add_argument("--job-id", required=True)
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
    store = JobStore(args.runtime_root / "studio.db")
    job = store.get_job(args.job_id)
    if (job.status is not JobStatus.RUNNING
            or not isinstance(job.payload, GenerationJobPayload)):
        raise ValueError("generation worker requires one running generation job")
    contract = job.payload.contract

    def event(
            event_type: JobEventType, summary: str, *,
            phase: JobPhase = JobPhase.RUNNING,
            detail: dict | None = None) -> None:
        store.append_job_event(
            job.id,
            job.profile,
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
    result = runner.run(job.id, job.project, job.clip_id, contract)
    print(json.dumps(result, separators=(",", ":"), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
