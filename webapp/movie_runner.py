from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from studio_core.job_contracts import (
    JobEventType,
    JobPhase,
    MovieExportJobPayload,
)
from studio_core.job_store import JobStore
from studio_core.models import Job
from studio_core.projects import project_path
from webapp.config import Settings
from webapp.movie_store import MOVIE_FILENAME, MovieStore
from webapp.process_runner import ProcessCancelled, SupervisedProcessRunner


log = logging.getLogger(__name__)


class MovieJobRunner:
    """Validate, execute, and verify deterministic movie export jobs."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        owner_id: str,
        process_runner: SupervisedProcessRunner,
        environment: Callable[[Job], dict[str, str]],
        export_chat: Callable[[Job], None],
    ) -> None:
        self.settings = settings
        self.store = store
        self.owner_id = owner_id
        self.process_runner = process_runner
        self.environment = environment
        self.export_chat = export_chat

    def command(self, job: Job, project: Path, contract: dict) -> list[str]:
        return [
            sys.executable,
            str(self.settings.repo / "webapp" / "movie_runner.py"),
            "--project", str(project),
            "--job-id", job.id,
            "--contract", json.dumps(
                contract, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False),
        ]

    def _fail(self, job: Job, error: str) -> None:
        self.store.fail(job.id, error, self.owner_id)
        self.export_chat(job)

    def execute(self, job: Job) -> None:
        project = project_path(self.settings.studio_root, job.project)
        try:
            if not isinstance(job.payload, MovieExportJobPayload):
                raise ValueError("movie runner received the wrong payload type")
            contract = job.payload.contract
            MovieStore._validate_contract(contract)
        except ValueError as exc:
            self._fail(job, f"Movie export contract is invalid: {exc}")
            return
        self.store.append_job_event(
            job.id,
            job.profile,
            JobEventType.MOVIE_EXPORT,
            f"Assembling {len(contract['sources'])} selected takes with hard cuts",
            phase=JobPhase.RUNNING,
            detail={"mode": contract["assembly"]["mode"]},
        )
        try:
            result = self.process_runner.run(
                job,
                self.command(job, project, contract),
                self.environment(job),
            )
        except ProcessCancelled:
            self._fail(job, "Studio server stopped")
            return
        except subprocess.TimeoutExpired:
            self._fail(
                job,
                f"Movie export timed out after "
                f"{self.settings.job_timeout_seconds}s",
            )
            return
        except Exception as exc:
            log.exception("Movie export job %s failed", job.id)
            self._fail(job, str(exc))
            return
        if result.returncode:
            detail = (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip() else "")
            error = f"Movie export failed ({result.returncode})"
            if detail:
                error += f": {detail}"
            self._fail(job, error)
            return
        try:
            document = json.loads(result.stdout)
            verified = MovieStore().verify_export(project, contract, job.id)
            if document.get("id") != verified["id"]:
                raise ValueError(
                    "movie exporter result does not match publication")
            self.store.complete(
                job.id,
                self.owner_id,
                f"Movie export completed: {verified['id']}/{MOVIE_FILENAME}",
                None,
            )
            self.export_chat(job)
        except Exception as exc:
            log.exception("Movie export completion for job %s failed", job.id)
            self._fail(job, str(exc))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Assemble one immutable Hermes Studio movie export contract")
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--contract", required=True)
    args = parser.parse_args()

    contract = json.loads(args.contract)
    result = MovieStore().export(args.project, contract, args.job_id)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
