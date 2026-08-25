from __future__ import annotations

import json
import logging
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from studio_core.comfyui_mcp import (
    McpCall,
    call_mcp,
    cleanup_comfyui,
    mcp_environment,
    submit_exact_h3_graph,
    unwrap_content,
)
from studio_core.generation_archive import (
    archive_outputs,
    parse_generation_job_payload,
)
from studio_core.job_contracts import JobEventType, JobPhase
from studio_core.safe_files import (
    SafeFilesystemError,
    open_regular_file,
    read_opened_text,
)
from studio_core.job_store import JobStore
from studio_core.models import Job
from webapp.config import Settings
from webapp.process_runner import ProcessCancelled, SupervisedProcessRunner


log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GenerationRuntime:
    studio_root: Path
    runtime_root: Path
    profile_home: Path
    real_home: Path
    comfy_root: Path
    comfy_url: str
    comfy_python: Path
    timeout_seconds: int


EventCallback = Callable[..., None]
CommandRunner = Callable[..., subprocess.CompletedProcess]
Archiver = Callable[..., Path]


class GenerationJobRunner:
    def __init__(
            self, runtime: GenerationRuntime, *,
            command_runner: CommandRunner = subprocess.run,
            mcp_call: McpCall = call_mcp,
            archiver: Archiver = archive_outputs,
            event_callback: EventCallback | None = None):
        self.runtime = runtime
        self.command_runner = command_runner
        self.mcp_call = mcp_call
        self.archiver = archiver
        self.event_callback = event_callback or (lambda *_args, **_kwargs: None)

    def _event(
            self, event_type: JobEventType, summary: str, *,
            phase: JobPhase = JobPhase.RUNNING,
            detail: dict[str, Any] | None = None) -> None:
        self.event_callback(
            event_type, summary, phase=phase, detail=detail or {})

    def _graph_command(
            self, project: str, clip_id: str, contract: dict,
            output: Path) -> list[str]:
        project_path = self.runtime.studio_root / "projects" / project
        clip_path = project_path / "clips" / clip_id
        settings = contract["settings_manifest"]
        execution = contract["execution"]
        command = [
            "python3",
            str(self.runtime.profile_home /
                "skills/minimax-h3-run/scripts/run_h3.py"),
            "--mode", settings["mode"],
            "--prompt-file", str(clip_path / "current_prompt.txt"),
            "--width", str(execution["resolution"]["width"]),
            "--height", str(execution["resolution"]["height"]),
            "--length", str(execution["timing"]["frames"]),
            "--steps", str(settings["steps"]),
        ]
        if settings["seed"] is not None:
            command.extend(("--seed", str(settings["seed"])))
        if settings["accel"]:
            command.append("--accel")
        for filename in execution["references"]:
            command.extend((
                "--image", str(project_path / "references" / filename)))
        command.extend(("--dry-run", "--output-json", str(output)))
        return command

    @staticmethod
    def _read_graph(path: Path) -> dict:
        try:
            with open_regular_file(path) as opened:
                document = json.loads(read_opened_text(opened))
        except (
            FileNotFoundError,
            OSError,
            SafeFilesystemError,
            json.JSONDecodeError,
        ) as exc:
            raise RuntimeError("generated H3 graph is not a regular JSON file") from exc
        if not isinstance(document, dict):
            raise RuntimeError("generated H3 graph is invalid")
        return document

    @staticmethod
    def _completed_batch(result: Any, batch_id: str, prompt_id: str) -> bool:
        value = unwrap_content(result)
        if not isinstance(value, dict) or value.get("batch_id") != batch_id:
            raise RuntimeError("comfyui-mcp batch wait returned an invalid result")
        jobs = value.get("jobs")
        if not isinstance(jobs, list) or len(jobs) != 1:
            raise RuntimeError("comfyui-mcp batch wait did not contain one job")
        job = jobs[0]
        if not isinstance(job, dict) or job.get("prompt_id") != prompt_id:
            raise RuntimeError("comfyui-mcp batch wait prompt does not match submission")
        state = job.get("state")
        if state == "error":
            detail = job.get("error_message")
            raise RuntimeError(
                detail if isinstance(detail, str) and detail else
                "ComfyUI generation failed")
        if state == "done":
            if value.get("all_terminal") is not True:
                raise RuntimeError("completed ComfyUI batch is not terminal")
            return True
        if state not in {"pending", "running", "unknown"}:
            raise RuntimeError("comfyui-mcp batch wait returned an invalid state")
        if value.get("timed_out") is not True:
            raise RuntimeError("non-terminal ComfyUI batch wait did not time out")
        return False

    @staticmethod
    def _output_files(
            result: Any, batch_id: str, prompt_id: str,
            graph_path: Path) -> list[str]:
        value = unwrap_content(result)
        if (not isinstance(value, dict) or value.get("batch_id") != batch_id
                or value.get("completed") != 1):
            raise RuntimeError("comfyui-mcp batch output returned an invalid result")
        entries = value.get("outputs")
        if not isinstance(entries, list) or len(entries) != 1:
            raise RuntimeError("comfyui-mcp batch output did not contain one job")
        entry = entries[0]
        if (not isinstance(entry, dict) or entry.get("prompt_id") != prompt_id
                or entry.get("state") != "done"
                or not isinstance(entry.get("outputs"), dict)):
            raise RuntimeError("comfyui-mcp batch output does not match submission")

        document = GenerationJobRunner._read_graph(graph_path)
        graph = document.get("prompt_stage1_h3")
        if not isinstance(graph, dict):
            raise RuntimeError("generated H3 graph is invalid")
        save_nodes = [
            node_id for node_id, node in graph.items()
            if isinstance(node_id, str) and isinstance(node, dict)
            and node.get("class_type") == "SaveVideo"
        ]
        if len(save_nodes) != 1:
            raise RuntimeError("generated H3 graph must contain one SaveVideo node")
        node_output = entry["outputs"].get(save_nodes[0])
        if not isinstance(node_output, dict):
            raise RuntimeError("ComfyUI batch output is missing SaveVideo output")

        files: list[str] = []
        for items in node_output.values():
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict) or item.get("type") != "output":
                    continue
                filename = item.get("filename")
                subfolder = item.get("subfolder", "")
                if (not isinstance(filename, str) or not filename
                        or "/" in filename or "\\" in filename
                        or not isinstance(subfolder, str)
                        or "\\" in subfolder or subfolder.startswith("/")
                        or (subfolder and any(
                            part in {"", ".", ".."}
                            for part in subfolder.split("/")))):
                    raise RuntimeError("ComfyUI returned an invalid output path")
                files.append(
                    f"{subfolder}/{filename}" if subfolder else filename)
        if not files:
            raise RuntimeError("ComfyUI SaveVideo node returned no output file")
        if len(files) != len(set(files)):
            raise RuntimeError("ComfyUI SaveVideo node returned duplicate output files")
        return files

    def run(
            self, job_id: str, project: str, clip_id: str,
            contract: dict) -> dict[str, Any]:
        parsed = parse_generation_job_payload(json.dumps(contract))
        project_path = self.runtime.studio_root / "projects" / project
        clip_path = project_path / "clips" / clip_id
        references = [
            project_path / "references" / filename
            for filename in parsed["execution"]["references"]
        ]
        environment = mcp_environment(
            self.runtime.comfy_url,
            self.runtime.comfy_root,
            self.runtime.comfy_python,
        )
        graph_environment = environment.copy()
        graph_environment.update({
            "HOME": str(self.runtime.profile_home),
            "HERMES_HOME": str(self.runtime.profile_home),
            "HERMES_REAL_HOME": str(self.runtime.real_home),
        })
        self.runtime.runtime_root.mkdir(parents=True, exist_ok=True)
        prompt_id: str | None = None
        completed = False
        primary_error: Exception | None = None
        try:
            with tempfile.TemporaryDirectory(
                    prefix=f"generation-{job_id}-",
                    dir=self.runtime.runtime_root) as directory:
                graph_path = Path(directory) / "h3-graph.json"
                command = self._graph_command(
                    project, clip_id, parsed, graph_path)
                self._event(
                    JobEventType.GENERATION_GRAPH,
                    "Building the exact H3 graph",
                    detail={"mode": parsed["settings_manifest"]["mode"]})
                result = self.command_runner(
                    command,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=min(300, self.runtime.timeout_seconds),
                    env=graph_environment,
                )
                if result.returncode:
                    detail = (result.stderr or result.stdout).strip()
                    raise RuntimeError(
                        f"H3 graph builder failed ({result.returncode}): {detail}")
                self._read_graph(graph_path)

                self._event(
                    JobEventType.GENERATION_SUBMIT,
                    "Uploading references and submitting one exact workflow",
                    detail={"reference_count": len(references)})
                submission = submit_exact_h3_graph(
                    graph_path,
                    clip_path / "current_prompt.txt",
                    clip_path / "current_generation.json",
                    parsed["prompt_sha256"],
                    parsed["settings_updated_at"],
                    references,
                    environment,
                    mcp_call=self.mcp_call,
                )
                batch_id = submission["batch_id"]
                prompt_id = submission["prompt_id"]
                if (not isinstance(batch_id, str) or not batch_id
                        or not isinstance(prompt_id, str) or not prompt_id):
                    raise RuntimeError("H3 submission returned invalid identifiers")
                self._event(
                    JobEventType.GENERATION_WAIT,
                    "Waiting for ComfyUI generation",
                    detail={"prompt_id": prompt_id, "batch_id": batch_id})
                while True:
                    waited = self.mcp_call(
                        "batch",
                        {"action": "wait", "batch_id": batch_id,
                         "timeout_s": 600},
                        environment,
                        660,
                    )
                    if self._completed_batch(waited, batch_id, prompt_id):
                        break
                output = self.mcp_call(
                    "batch",
                    {"action": "output", "batch_id": batch_id},
                    environment,
                    180,
                )
                files = self._output_files(
                    output, batch_id, prompt_id, graph_path)
                self._event(
                    JobEventType.GENERATION_ARCHIVE,
                    "Archiving verified ComfyUI output",
                    detail={"prompt_id": prompt_id, "files": files})
                generation = self.archiver(
                    self.runtime.studio_root,
                    project,
                    clip_id,
                    files,
                    {"prompt_id": prompt_id},
                    source_root=self.runtime.comfy_root / "output",
                    transport="comfyui-mcp",
                )
                if generation.name != parsed["expected_generation_id"]:
                    raise RuntimeError(
                        "generation archive does not match the expected sequence")
                completed = True
                return {
                    "generation_id": generation.name,
                    "prompt_id": prompt_id,
                    "outputs": files,
                }
        except Exception as exc:
            primary_error = exc
            raise
        finally:
            try:
                self._event(
                    JobEventType.COMFYUI_CLEANUP,
                    "Releasing ComfyUI queue and VRAM",
                    detail={"cancel": not completed})
                cleanup_comfyui(
                    environment,
                    prompt_id=prompt_id,
                    cancel=not completed,
                    mcp_call=self.mcp_call,
                )
            except Exception as cleanup_error:
                if primary_error is not None:
                    primary_error.add_note(
                        f"ComfyUI cleanup also failed: {cleanup_error}")
                else:
                    raise


class GenerationWorkerRunner:
    """Supervise the deterministic generation worker process for one job."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        owner_id: str,
        process_runner: SupervisedProcessRunner,
        environment: Callable[[Job], dict[str, str]],
        export_chat: Callable[[Job], None],
        cleanup: Callable[[], None],
        validate: Callable[[Job], dict],
        verify: Callable[[Job], None],
    ) -> None:
        self.settings = settings
        self.store = store
        self.owner_id = owner_id
        self.process_runner = process_runner
        self.environment = environment
        self.export_chat = export_chat
        self.cleanup = cleanup
        self.validate = validate
        self.verify = verify

    def command(self, job: Job) -> list[str]:
        return [
            sys.executable,
            str(self.settings.repo / "webapp" / "generation_worker.py"),
            "--job-id", job.id,
            "--project", job.project,
            "--clip", job.clip_id,
            "--profile", job.profile,
            "--studio-root", str(self.settings.studio_root),
            "--runtime-root", str(self.settings.runtime_root),
            "--profile-home", str(self.settings.profile_home(job.profile)),
            "--real-home", str(self.settings.real_home),
            "--comfyui-root", str(self.settings.comfy_root),
            "--comfyui-url", self.settings.comfy_url,
            "--comfyui-python", str(
                self.settings.comfy_root / ".venv/bin/python"),
            "--timeout-seconds", str(self.settings.job_timeout_seconds),
        ]

    def _fail(self, job: Job, error: str) -> None:
        self.store.fail(job.id, error, self.owner_id)
        self.export_chat(job)

    def execute(self, job: Job) -> None:
        try:
            self.validate(job)
        except Exception as exc:
            error = f"Generation request validation failed: {exc}"
            self.store.append_job_event(
                job.id, job.profile, JobEventType.GENERATION_VALIDATION, error,
                phase=JobPhase.FAILED)
            self._fail(job, error)
            return
        try:
            result = self.process_runner.run(
                job, self.command(job), self.environment(job))
        except ProcessCancelled:
            self._fail(job, "Studio server stopped")
            return
        except subprocess.TimeoutExpired:
            self.store.append_job_event(
                job.id, job.profile, JobEventType.JOB_TIMEOUT,
                f"Exceeded the {self.settings.job_timeout_seconds}s job limit",
                phase=JobPhase.FAILED)
            self.cleanup()
            self._fail(
                job,
                f"Generation timed out after {self.settings.job_timeout_seconds}s",
            )
            return
        except Exception as exc:
            log.exception("Generation job %s failed", job.id)
            self.cleanup()
            try:
                self._fail(job, str(exc))
            except Exception:
                log.exception(
                    "Could not persist generation failure for job %s", job.id)
            return
        if result.returncode:
            detail = (
                result.stderr.strip().splitlines()[-1]
                if result.stderr.strip() else "")
            error = f"Generation worker failed ({result.returncode})"
            if detail:
                error += f": {detail}"
            self.cleanup()
            self._fail(job, error)
            return
        try:
            document = json.loads(result.stdout)
            if (not isinstance(document, dict)
                    or not isinstance(document.get("generation_id"), str)
                    or not isinstance(document.get("prompt_id"), str)
                    or not isinstance(document.get("outputs"), list)):
                raise ValueError("generation worker returned an invalid result")
            self.verify(job)
            self.store.complete(
                job.id,
                self.owner_id,
                f"Generation completed: {document['generation_id']} "
                f"(prompt_id: {document['prompt_id']})",
                None,
            )
            self.export_chat(job)
        except Exception as exc:
            log.exception("Generation completion for job %s failed", job.id)
            self.cleanup()
            self._fail(job, str(exc))
