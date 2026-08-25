from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from studio_core.generation_archive import parse_generation_job_payload
from studio_core.comfyui_mcp import cleanup_comfyui, mcp_environment
from studio_core.projects import (
    next_generation_dir,
    project_path,
    read_project_text,
)
from studio_core.projects import ClipStore
from webapp.config import Settings

from webapp.generation_settings_store import GenerationSettingsStore
from studio_core.hermes_events import HermesSessionEventBridge
from studio_core.job_store import ActiveJobError, JobStore, JobStoreError
from webapp.media_review_store import MediaReviewStore
from studio_core.models import Job, JobStatus
from webapp.movie_store import MOVIE_FILENAME, MovieStore
from webapp.project_jobs import project_job_guard


log = logging.getLogger(__name__)
SESSION_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
CommandBuilder = Callable[[Job, str | None], list[str]]
PROFILE_TOOLSETS = {
    "studio-storyboarder": "file,terminal,skills",
    "studio-prompt-engineer": "file,terminal,skills",
    "studio-reviewer": "file,terminal,vision,skills",
    "studio-illustrator": "file,terminal,skills",
}


def process_start_time(pid: int) -> int:
    """Return Linux process start ticks, which disambiguate PID reuse."""
    value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    command_end = value.rfind(")")
    if command_end < 0:
        raise OSError(f"invalid process stat for pid {pid}")
    fields = value[command_end + 2:].split()
    if len(fields) <= 19:
        raise OSError(f"incomplete process stat for pid {pid}")
    return int(fields[19])


def process_environment(pid: int) -> set[bytes]:
    return {
        item for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if item
    }


class StudioJobManager:
    def __init__(self, settings: Settings, store: JobStore,
                 command_builder: CommandBuilder | None = None,
                 cleanup_callback: Callable[[], None] | None = None):
        self.settings = settings
        self.store = store
        self.owner_id = uuid.uuid4().hex
        self.command_builder = command_builder or self._default_command
        self.cleanup_callback = cleanup_callback or self._cleanup_comfy
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._wake = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self._processes: dict[str, subprocess.Popen] = {}
        self._process_lock = threading.Lock()

    def start(self) -> None:
        self.store.initialize()
        self.store.register_worker(self.owner_id)
        self._recover_running_jobs()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name=f"studio-scheduler-{self.owner_id[:8]}",
            daemon=False,
        )
        self._scheduler.start()
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"studio-heartbeat-{self.owner_id[:8]}",
            daemon=False,
        )
        self._heartbeat.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._process_lock:
            processes = list(self._processes.values())
        had_gpu_process = any(
            job.owner_id == self.owner_id and self._job_may_own_gpu(job)
            for job in self.store.active_jobs()
        )
        for process in processes:
            self._terminate_process(process)
        if had_gpu_process:
            self._cleanup_safely()
        if self._scheduler:
            self._scheduler.join(timeout=30)
            if self._scheduler.is_alive():
                log.error("Studio scheduler did not stop within 30 seconds")
        for job in self.store.active_jobs():
            if job.owner_id == self.owner_id:
                self.store.fail(job.id, "Studio server stopped", self.owner_id)
                self._export_job_chat(job)
        self._heartbeat_stop.set()
        if self._heartbeat:
            self._heartbeat.join(timeout=5)
        self.store.unregister_worker(self.owner_id)

    def submit_project_chat(self, project: str, message: str,
                            profile: str | None = None) -> Job:
        project_directory = project_path(self.settings.studio_root, project)
        with project_job_guard(project_directory):
            chat_path = self._chat_path(project)
            self.store.import_chat_if_empty(project, chat_path)
            job = self.store.create_project_chat_job(
                project, message, profile or self.settings.studio_profile)
        self._export_chat(project)
        self._wake.set()
        return job

    def submit_chat(self, project: str, clip_id: str, message: str,
                    profile: str | None = None) -> Job:
        project_directory = project_path(self.settings.studio_root, project)
        with project_job_guard(project_directory):
            chat_path = self._chat_path(project, clip_id)
            self.store.import_chat_if_empty(
                project, chat_path, clip_id=clip_id)
            job = self.store.create_chat_job(
                project, message, profile or self.settings.studio_profile,
                clip_id=clip_id)
        self._export_chat(project, clip_id)
        self._wake.set()
        return job

    def submit_generation(self, project: str, clip_id: str,
                          prompt_sha256: str,
                          settings_updated_at: str) -> Job:
        project_directory = project_path(self.settings.studio_root, project)
        with project_job_guard(project_directory):
            request = self._generation_request(
                project_directory, project, clip_id,
                prompt_sha256, settings_updated_at)
            chat_path = self._chat_path(project, clip_id)
            self.store.import_chat_if_empty(
                project, chat_path, clip_id=clip_id)
            job = self.store.create_generation_job(
                project, request, self.settings.studio_profile, clip_id=clip_id)
        self._export_chat(project, clip_id)
        self._wake.set()
        return job

    def submit_movie_export(self, project: str) -> Job:
        project_directory = project_path(self.settings.studio_root, project)
        with project_job_guard(project_directory):
            if any(job.project == project for job in self.store.active_jobs()):
                raise ActiveJobError("project already has an active Studio job")
            contract = MovieStore().build_contract(project_directory)
            job = self.store.create_movie_export_job(
                project,
                json.dumps(
                    contract, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False),
                self.settings.studio_profile,
            )
        self._export_chat(project)
        self._wake.set()
        return job

    def _generation_request(
            self, project_path: Path, project: str, clip_id: str,
            prompt_sha256: str, settings_updated_at: str) -> str:
        clip_store = ClipStore()
        project_manifest = clip_store.describe(project_path)
        entry = next(
            (item for item in project_manifest["clips"]
             if item["id"] == clip_id),
            None,
        )
        if entry is None:
            raise ValueError(f"clip not found: {clip_id}")
        if not entry["enabled"]:
            raise ValueError("clip is disabled")
        clip = clip_store.resolve_clip(project_path, clip_id)
        settings_store = GenerationSettingsStore(self.settings)
        current = settings_store.validate_generation_request(
            project_path, clip, prompt_sha256, settings_updated_at)
        normalized_settings = settings_store.normalize(current["settings"])
        prompt = read_project_text(
            clip, "current_prompt.txt", required=True)
        payload = {
            "schema_version": 1,
            "action": "generate-current-prompt",
            "prompt": prompt,
            "prompt_sha256": prompt_sha256,
            "settings_updated_at": settings_updated_at,
            "settings_manifest": {
                "schema_version": current["manifest"]["schema_version"],
                "prompt_sha256": prompt_sha256,
                "updated_at": settings_updated_at,
                **normalized_settings,
            },
            "execution": {
                "resolution": current["readiness"]["resolution"],
                "timing": current["readiness"]["timing"],
                "references": current["readiness"]["references"],
            },
            "expected_generation_id": next_generation_dir(
                self.settings.studio_root, project, clip_id).name,
        }
        request = json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)
        parse_generation_job_payload(request)
        return request

    def _scheduler_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.heartbeat_worker(self.owner_id)
                stale = self.store.claim_stale_running(
                    self.owner_id,
                    time.time() - self.settings.worker_lease_timeout_seconds,
                )
                if stale:
                    self._recover_claimed_job(stale)
                    continue
                job = self.store.claim_next(self.owner_id)
            except (JobStoreError, OSError, sqlite3.Error):
                log.exception("Could not claim next Studio job")
                self._stop.wait(0.5)
                continue
            if not job:
                self._wake.wait(0.5)
                self._wake.clear()
                continue
            if self._stop.is_set():
                self.store.fail(job.id, "Studio server stopped", self.owner_id)
                self._export_job_chat(job)
                break
            try:
                self._execute(job)
            except Exception as exc:
                log.exception("Unexpected Studio execution failure for job %s", job.id)
                if not self._contain_execution_failure(job, exc):
                    raise

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(1):
            scheduler = self._scheduler
            if (scheduler is not None and not scheduler.is_alive()
                    and not self._stop.is_set()):
                log.critical(
                    "Studio scheduler stopped unexpectedly; releasing worker lease %s",
                    self.owner_id,
                )
                try:
                    self.store.unregister_worker(self.owner_id)
                except Exception:
                    log.exception("Could not unregister failed Studio worker")
                return
            try:
                self.store.heartbeat_worker(self.owner_id)
            except Exception:
                log.exception("Could not update Studio worker heartbeat")

    def _contain_execution_failure(self, job: Job, exc: Exception) -> bool:
        with self._process_lock:
            process = self._processes.get(job.id)
        if process is not None:
            self._terminate_process(process)
        if self._job_may_own_gpu(job):
            self._cleanup_safely()
        try:
            current = self.store.get_job(job.id)
            if (current.status is JobStatus.RUNNING
                    and current.owner_id == self.owner_id):
                self.store.fail(
                    job.id,
                    f"Unexpected Studio execution failure: {exc}",
                    self.owner_id,
                )
            self._export_job_chat(job)
            return True
        except Exception:
            log.exception("Could not contain execution failure for job %s", job.id)
            return False

    def _execute(self, job: Job) -> None:
        if job.kind == "export_movie":
            self._execute_movie(job)
            return
        if job.kind == "generate":
            self._execute_generation(job)
            return
        try:
            session_id = self.store.get_session(
                job.project, job.profile, clip_id=self._scope_clip_id(job))
            command = self.command_builder(job, session_id)
        except Exception as exc:
            error = f"Could not prepare Studio job: {exc}"
            self.store.append_job_event(
                job.id,
                job.profile,
                "job.prepare",
                error,
                status="failed",
            )
            self.store.fail(job.id, error, self.owner_id)
            self._export_job_chat(job)
            return
        bridge = HermesSessionEventBridge(
            self.store,
            self.settings,
            job,
            source=self._session_source(job),
            started_at=time.time(),
            session_id=session_id,
        )
        if not self._prepare_event_bridge(bridge):
            error = "Could not establish Hermes session event baseline"
            self.store.append_job_event(
                job.id,
                job.profile,
                "job.prepare",
                error,
                status="failed",
            )
            self.store.fail(job.id, error, self.owner_id)
            self._export_job_chat(job)
            return
        process: subprocess.Popen | None = None
        try:
            with self._process_lock:
                if self._stop.is_set():
                    self.store.fail(
                        job.id, "Studio server stopped", self.owner_id)
                    self._export_job_chat(job)
                    return
                process = subprocess.Popen(
                    self._supervised_command(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=self._job_environment(job),
                )
                self.store.set_process(
                    job.id, self.owner_id, process.pid,
                    process_start_time(process.pid))
                self._processes[job.id] = process
            try:
                stdout, stderr = self._communicate(process, bridge)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                process.communicate()
                self.store.append_job_event(
                    job.id,
                    job.profile,
                    "job.timeout",
                    f"Exceeded the {self.settings.job_timeout_seconds}s job limit",
                    status="failed",
                )
                if self._job_may_own_gpu(job):
                    self.store.append_job_event(
                        job.id,
                        job.profile,
                        "comfyui.cleanup",
                        "Cancelling ComfyUI work after Studio timeout",
                        status="running",
                    )
                    self._cleanup_safely()
                self.store.fail(
                    job.id,
                    f"Studio agent timed out after "
                    f"{self.settings.job_timeout_seconds}s",
                    self.owner_id,
                )
                self._export_job_chat(job)
                return
            if process.returncode:
                if self._stop.is_set():
                    return
                error = f"Studio agent failed ({process.returncode})"
                log.error(
                    "Studio agent failed (%d): %s",
                    process.returncode, stderr.strip(),
                )
                if self._job_may_own_gpu(job):
                    self._cleanup_safely()
                self.store.fail(
                    job.id, error, self.owner_id,
                )
                self._export_job_chat(job)
                return
            reply = stdout.strip()
            if not reply:
                if self._job_may_own_gpu(job):
                    self._cleanup_safely()
                self.store.fail(
                    job.id, "Studio agent returned an empty reply", self.owner_id)
                self._export_job_chat(job)
                return
            match = SESSION_RE.search(stderr)
            self.store.complete(
                job.id, self.owner_id, reply,
                match.group(1) if match else bridge.session_id,
            )
            self._export_job_chat(job)
        except Exception as exc:
            log.exception("Studio job %s failed", job.id)
            if process:
                self._terminate_process(process)
            if self._job_may_own_gpu(job):
                self._cleanup_safely()
            try:
                self.store.fail(job.id, str(exc), self.owner_id)
                self._export_job_chat(job)
            except Exception:
                log.exception("Could not persist failure for job %s", job.id)
        finally:
            with self._process_lock:
                self._processes.pop(job.id, None)

    def _generation_worker_command(self, job: Job) -> list[str]:
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

    def _execute_generation(self, job: Job) -> None:
        try:
            self._validate_generation_job(job)
        except Exception as exc:
            error = f"Generation request validation failed: {exc}"
            self.store.append_job_event(
                job.id, job.profile, "generation.validation", error,
                status="failed")
            self.store.fail(job.id, error, self.owner_id)
            self._export_job_chat(job)
            return

        process: subprocess.Popen | None = None
        try:
            with self._process_lock:
                if self._stop.is_set():
                    self.store.fail(
                        job.id, "Studio server stopped", self.owner_id)
                    self._export_job_chat(job)
                    return
                process = subprocess.Popen(
                    self._supervised_command(
                        self._generation_worker_command(job)),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=self._job_environment(job),
                )
                self.store.set_process(
                    job.id, self.owner_id, process.pid,
                    process_start_time(process.pid))
                self._processes[job.id] = process
            try:
                stdout, stderr = self._communicate(process, None)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                process.communicate()
                self.store.append_job_event(
                    job.id,
                    job.profile,
                    "job.timeout",
                    f"Exceeded the {self.settings.job_timeout_seconds}s job limit",
                    status="failed",
                )
                self._cleanup_safely()
                self.store.fail(
                    job.id,
                    f"Generation timed out after "
                    f"{self.settings.job_timeout_seconds}s",
                    self.owner_id,
                )
                self._export_job_chat(job)
                return
            if process.returncode:
                detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
                error = f"Generation worker failed ({process.returncode})"
                if detail:
                    error += f": {detail}"
                self._cleanup_safely()
                self.store.fail(job.id, error, self.owner_id)
                self._export_job_chat(job)
                return
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("generation worker returned invalid output") from exc
            if (not isinstance(result, dict)
                    or not isinstance(result.get("generation_id"), str)
                    or not isinstance(result.get("prompt_id"), str)
                    or not isinstance(result.get("outputs"), list)):
                raise ValueError("generation worker returned an invalid result")
            self._verify_generation_completion(job)
            self.store.complete(
                job.id,
                self.owner_id,
                f"Generation completed: {result['generation_id']} "
                f"(prompt_id: {result['prompt_id']})",
                None,
            )
            self._export_job_chat(job)
        except Exception as exc:
            log.exception("Generation job %s failed", job.id)
            if process:
                self._terminate_process(process)
            self._cleanup_safely()
            try:
                self.store.fail(job.id, str(exc), self.owner_id)
                self._export_job_chat(job)
            except Exception:
                log.exception("Could not persist generation failure for job %s", job.id)
        finally:
            with self._process_lock:
                self._processes.pop(job.id, None)

    def _execute_movie(self, job: Job) -> None:
        project = project_path(self.settings.studio_root, job.project)
        try:
            contract = json.loads(job.message)
            MovieStore._validate_contract(contract)
        except (json.JSONDecodeError, ValueError) as exc:
            self.store.fail(
                job.id, f"Movie export contract is invalid: {exc}", self.owner_id)
            self._export_job_chat(job)
            return
        command = [
            sys.executable,
            str(self.settings.repo / "webapp" / "movie_runner.py"),
            "--project", str(project),
            "--job-id", job.id,
            "--contract", job.message,
        ]
        self.store.append_job_event(
            job.id, job.profile, "movie.export",
            f"Assembling {len(contract['sources'])} selected takes with hard cuts",
            status="running",
            detail={"mode": contract["assembly"]["mode"]},
        )
        process: subprocess.Popen | None = None
        try:
            with self._process_lock:
                if self._stop.is_set():
                    self.store.fail(
                        job.id, "Studio server stopped", self.owner_id)
                    self._export_job_chat(job)
                    return
                process = subprocess.Popen(
                    self._supervised_command(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=self._job_environment(job),
                )
                self.store.set_process(
                    job.id, self.owner_id, process.pid,
                    process_start_time(process.pid))
                self._processes[job.id] = process
            try:
                stdout, stderr = self._communicate(process, None)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                process.communicate()
                self.store.fail(
                    job.id,
                    f"Movie export timed out after "
                    f"{self.settings.job_timeout_seconds}s",
                    self.owner_id,
                )
                self._export_job_chat(job)
                return
            if process.returncode:
                detail = stderr.strip().splitlines()[-1] if stderr.strip() else ""
                error = f"Movie export failed ({process.returncode})"
                if detail:
                    error += f": {detail}"
                self.store.fail(job.id, error, self.owner_id)
                self._export_job_chat(job)
                return
            try:
                result = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise ValueError("movie exporter returned invalid output") from exc
            verified = MovieStore().verify_export(project, contract, job.id)
            if result.get("id") != verified["id"]:
                raise ValueError("movie exporter result does not match publication")
            self.store.complete(
                job.id,
                self.owner_id,
                f"Movie export completed: {verified['id']}/{MOVIE_FILENAME}",
                None,
            )
            self._export_job_chat(job)
        except Exception as exc:
            log.exception("Movie export job %s failed", job.id)
            if process:
                self._terminate_process(process)
            self.store.fail(job.id, str(exc), self.owner_id)
            self._export_job_chat(job)
        finally:
            with self._process_lock:
                self._processes.pop(job.id, None)

    def _default_command(self, job: Job, session_id: str | None) -> list[str]:
        command = [
            self.settings.hermes_command,
            "-p", job.profile,
        ]
        if session_id and re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            command += ["-r", session_id]
        toolsets = PROFILE_TOOLSETS.get(job.profile, "all")
        command += [
            "chat", "-Q", "-t", toolsets, "--source", self._session_source(job),
            "-q", self._agent_query(job),
        ]
        return command

    @staticmethod
    def _session_source(job: Job) -> str:
        return f"studio-web:{job.id}"

    def _prepare_event_bridge(self, bridge: HermesSessionEventBridge) -> bool:
        deadline = time.monotonic() + 2.0
        while not self._stop.is_set():
            if bridge.prepare():
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                self.store.heartbeat_worker(self.owner_id)
            except Exception:
                log.exception("Could not heartbeat while preparing Hermes events")
            self._stop.wait(0.1)
        return False

    def _supervised_command(self, command: list[str]) -> list[str]:
        return [
            sys.executable,
            str(self.settings.repo / "scripts" / "supervised_exec.py"),
            str(os.getpid()),
            *command,
        ]

    def _agent_query(self, job: Job) -> str:
        if job.kind == "generate":
            raise ValueError("generation jobs never execute through Hermes chat")
        project_path = self.settings.studio_root / "projects" / job.project
        path_context = (
            "Path roots: use $HERMES_HOME for active-profile Hermes data and "
            "skills; use $HERMES_REAL_HOME for account files such as ComfyUI, "
            "Documents, and repos. Do not derive either root from $HOME or `~`.\n"
        )
        if job.chat_scope == "project":
            target_context = (
                f"This migrated project-scope job explicitly targets clip "
                f"{job.clip_id}.\n"
                if job.clip_id else "There is no active clip.\n")
            return (
                "Exact Studio context (do not guess or fuzzy-match paths):\n"
                f"{path_context}"
                f"Project ID: {job.project}\n"
                f"Project path: {project_path}\n"
                f"Conversation scope: project chat. {target_context}"
                "Work only on project-wide planning, continuity, shared references, "
                "research, or final assembly. Do not choose or mutate a clip unless "
                "the user explicitly identifies one.\n\n"
                f"User request:\n{job.message}"
            )
        clip_path = project_path / "clips" / job.clip_id
        context = (
            "Exact Studio context (do not guess or fuzzy-match paths):\n"
            f"{path_context}"
            f"Project ID: {job.project}\n"
            f"Project path: {project_path}\n"
            f"Active clip ID: {job.clip_id}\n"
            f"Active clip path: {clip_path}\n"
            "Project chat and references are shared. Read or write prompt, settings, "
            "and generation files only under the active clip path.\n\n"
        )
        return context + f"User request:\n{job.message}"

    def _validate_generation_job(self, job: Job) -> dict:
        request = parse_generation_job_payload(job.message)
        project = project_path(self.settings.studio_root, job.project)
        clip_store = ClipStore()
        manifest = clip_store.describe(project)
        entry = next(
            (item for item in manifest["clips"] if item["id"] == job.clip_id), None)
        if entry is None:
            raise ValueError(f"clip not found: {job.clip_id}")
        if not entry["enabled"]:
            raise ValueError("clip is disabled")
        clip = clip_store.resolve_clip(project, job.clip_id)
        settings_store = GenerationSettingsStore(self.settings)
        current = settings_store.validate_generation_request(
            project,
            clip,
            request["prompt_sha256"],
            request["settings_updated_at"],
        )
        current_settings = {
            "schema_version": current["manifest"]["schema_version"],
            "prompt_sha256": current["manifest"]["prompt_sha256"],
            "updated_at": current["manifest"]["updated_at"],
            **settings_store.normalize(current["settings"]),
        }
        current_execution = {
            "resolution": current["readiness"]["resolution"],
            "timing": current["readiness"]["timing"],
            "references": current["readiness"]["references"],
        }
        if (read_project_text(clip, "current_prompt.txt", required=True)
                != request["prompt"]
                or current_settings != request["settings_manifest"]
                or current_execution != request["execution"]):
            raise ValueError("generation contract changed after enqueue")
        expected_generation_id = next_generation_dir(
            self.settings.studio_root, job.project, job.clip_id).name
        if expected_generation_id != request["expected_generation_id"]:
            raise ValueError("generation archive sequence changed after enqueue")
        return request

    def _verify_generation_completion(self, job: Job) -> None:
        contract = parse_generation_job_payload(job.message)
        project = project_path(self.settings.studio_root, job.project)
        clip = ClipStore().resolve_clip(project, job.clip_id)
        generation_id = contract["expected_generation_id"]
        try:
            details = MediaReviewStore().describe_generation(
                project, clip, generation_id, include_prompt=True)
        except Exception as exc:
            raise ValueError(
                "generation archive postcondition was not satisfied") from exc
        meta = details["meta"]
        if (meta.get("studio_job_id") != job.id
                or meta.get("generation_contract_version") != 1
                or meta.get("prompt_sha256") != contract["prompt_sha256"]
                or meta.get("settings_updated_at")
                != contract["settings_updated_at"]
                or not isinstance(meta.get("prompt_id"), str)
                or not meta["prompt_id"]
                or not details["files"]
                or sorted(meta.get("files", [])) != sorted(details["files"])
                or details.get("prompt") != contract["prompt"]):
            raise ValueError(
                "generation archive does not match its immutable job contract")
        try:
            archived_settings = json.loads(read_project_text(
                clip / "generations" / generation_id,
                "settings.json",
                required=True,
            ))
        except json.JSONDecodeError as exc:
            raise ValueError("generation archive settings are invalid") from exc
        if archived_settings != contract["settings_manifest"]:
            raise ValueError(
                "generation archive settings do not match its immutable job contract")

    def _job_environment(self, job: Job) -> dict[str, str]:
        environment = os.environ.copy()
        clip_path = (
            self.settings.studio_root / "projects" / job.project /
            "clips" / job.clip_id
            if job.clip_id else None)
        environment.update({
            "DESIGN_STUDIO_ROOT": str(self.settings.studio_root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(self.settings.runtime_root),
            "HERMES_STUDIO_JOB_ID": job.id,
            "HERMES_STUDIO_PROJECT": job.project,
            "HERMES_STUDIO_CLIP": job.clip_id,
            "HERMES_STUDIO_CLIP_PATH": str(clip_path) if clip_path else "",
            "HERMES_STUDIO_CHAT_SCOPE": job.chat_scope,
            "HERMES_STUDIO_PROFILE": job.profile,
            "HERMES_STUDIO_JOB_KIND": job.kind,
        })
        return environment

    def _chat_path(self, project: str, clip_id: str = "") -> Path:
        project_path = self.settings.studio_root / "projects" / project
        if clip_id:
            return project_path / "clips" / clip_id / "chat.jsonl"
        return project_path / "chat.jsonl"

    @staticmethod
    def _scope_clip_id(job: Job) -> str:
        return job.clip_id if job.chat_scope == "clip" else ""

    def _export_job_chat(self, job: Job) -> None:
        self._export_chat(job.project, self._scope_clip_id(job))

    def _export_chat(self, project: str, clip_id: str = "") -> None:
        try:
            self.store.export_chat(
                project, self._chat_path(project, clip_id), clip_id=clip_id)
        except Exception:
            log.exception("Could not export chat for project %s", project)

    def _recover_running_jobs(self) -> None:
        while stale := self.store.claim_stale_running(
                self.owner_id,
                time.time() - self.settings.worker_lease_timeout_seconds):
            self._recover_claimed_job(stale)

    def _recover_claimed_job(self, job: Job) -> bool:
        if not self._terminate_orphan_job(job):
            log.critical(
                "Keeping recovered job %s running because process termination "
                "could not be proven",
                job.id,
            )
            try:
                self.store.append_job_event(
                    job.id,
                    job.profile,
                    "job.recovery_blocked",
                    "Recovery blocked: possible Studio process is still alive",
                    status="running",
                )
            except Exception:
                log.exception("Could not record blocked recovery for job %s", job.id)
            return False
        if self._job_may_own_gpu(job):
            self._cleanup_safely()
        self.store.fail(
            job.id, "Studio worker lease expired during execution",
            self.owner_id)
        self._export_job_chat(job)
        return True

    def _communicate(self, process: subprocess.Popen,
                     bridge: HermesSessionEventBridge | None) -> tuple[str, str]:
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        while True:
            if bridge is not None:
                bridge.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    process.args, self.settings.job_timeout_seconds)
            try:
                output = process.communicate(timeout=min(1.0, remaining))
                if bridge is not None:
                    bridge.poll()
                return output
            except subprocess.TimeoutExpired:
                self.store.heartbeat_worker(self.owner_id)

    def _job_may_own_gpu(self, job: Job) -> bool:
        return (
            job.profile == self.settings.studio_profile
            and job.kind != "export_movie"
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass

    @staticmethod
    def _orphan_identity_matches(job: Job) -> bool:
        if job.pid is None or job.pid_start_time is None:
            log.error(
                "Refusing to terminate orphan job %s without a complete process identity",
                job.id)
            return False
        try:
            if process_start_time(job.pid) != job.pid_start_time:
                log.error(
                    "Refusing to terminate reused orphan pid %d for job %s",
                    job.pid, job.id)
                return False
            if os.getpgid(job.pid) != job.pid:
                log.error(
                    "Refusing to terminate orphan pid %d outside its own process group",
                    job.pid)
                return False
            environment = process_environment(job.pid)
        except (OSError, ProcessLookupError, ValueError):
            return False
        expected = {
            f"HERMES_STUDIO_JOB_ID={job.id}".encode(),
            f"HERMES_STUDIO_PROJECT={job.project}".encode(),
            f"HERMES_STUDIO_CLIP={job.clip_id}".encode(),
            f"HERMES_STUDIO_PROFILE={job.profile}".encode(),
        }
        if not expected.issubset(environment):
            log.error(
                "Refusing to terminate orphan pid %d without exact job ownership",
                job.pid)
            return False
        return True

    @classmethod
    def _terminate_orphan_process(cls, job: Job) -> bool:
        if not cls._orphan_identity_matches(job):
            return False
        assert job.pid is not None
        try:
            os.killpg(job.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        for _ in range(20):
            if cls._process_stopped(job.pid, job.pid_start_time):
                return True
            time.sleep(0.1)
        if not cls._orphan_identity_matches(job):
            return cls._process_stopped(job.pid, job.pid_start_time)
        try:
            os.killpg(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        for _ in range(20):
            if cls._process_stopped(job.pid, job.pid_start_time):
                return True
            time.sleep(0.1)
        return False

    @staticmethod
    def _process_stopped(pid: int, expected_start_time: int | None) -> bool:
        try:
            value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            command_end = value.rfind(")")
            if command_end < 0:
                return True
            fields = value[command_end + 2:].split()
            if not fields or fields[0] == "Z":
                return True
            return expected_start_time is not None and int(fields[19]) != expected_start_time
        except (OSError, ValueError, IndexError):
            return True

    @staticmethod
    def _job_process_tokens(job: Job) -> set[bytes]:
        return {
            f"HERMES_STUDIO_JOB_ID={job.id}".encode(),
            f"HERMES_STUDIO_PROJECT={job.project}".encode(),
            f"HERMES_STUDIO_CLIP={job.clip_id}".encode(),
            f"HERMES_STUDIO_PROFILE={job.profile}".encode(),
        }

    @classmethod
    def _matching_job_processes(cls, job: Job) -> list[tuple[int, int]]:
        expected = cls._job_process_tokens(job)
        matches: list[tuple[int, int]] = []
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                environment = process_environment(pid)
                if expected.issubset(environment):
                    matches.append((pid, process_start_time(pid)))
            except (OSError, ProcessLookupError, ValueError):
                continue
        return matches

    @classmethod
    def _terminate_orphan_job(cls, job: Job) -> bool:
        if job.pid is not None and job.pid_start_time is not None:
            if cls._terminate_orphan_process(job):
                return True

        matches = cls._matching_job_processes(job)
        if not matches:
            return True
        for pid, start_time in matches:
            candidate = replace(job, pid=pid, pid_start_time=start_time)
            if not cls._terminate_orphan_process(candidate):
                return False
        return not cls._matching_job_processes(job)

    def _cleanup_comfy(self) -> None:
        environment = mcp_environment(
            self.settings.comfy_url,
            self.settings.comfy_root,
            self.settings.comfy_root / ".venv/bin/python",
        )
        cleanup_comfyui(environment, cancel=True)

    def _cleanup_safely(self) -> None:
        try:
            self.cleanup_callback()
        except Exception:
            log.exception("Studio cleanup callback failed")
