from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from studio_core.comfyui_mcp import cleanup_comfyui, mcp_environment
from studio_core.generation_archive import parse_generation_job_payload
from studio_core.projects import (
    next_generation_dir,
    project_path,
    read_project_text,
)
from studio_core.projects import ClipStore
from webapp.config import Settings

from studio_core.job_store import ActiveJobError, JobStore, JobStoreError
from studio_core.models import Job, JobStatus
from webapp.agent_runner import AgentJobRunner, CommandBuilder
from webapp.generation_runner import GenerationWorkerRunner
from webapp.generation_settings_store import GenerationSettingsStore
from webapp.media_review_store import MediaReviewStore
from webapp.movie_runner import MovieJobRunner
from webapp.movie_store import MovieStore
from webapp.process_runner import (
    SupervisedProcessRunner,
    process_environment,
    process_start_time,
)
from webapp.project_jobs import project_job_guard


__all__ = ["StudioJobManager", "process_environment", "process_start_time"]


log = logging.getLogger(__name__)


class JobRunner(Protocol):
    def execute(self, job: Job) -> None: ...


class StudioJobManager:
    def __init__(self, settings: Settings, store: JobStore,
                 command_builder: CommandBuilder | None = None,
                 cleanup_callback: Callable[[], None] | None = None):
        self.settings = settings
        self.store = store
        self.owner_id = uuid.uuid4().hex
        self.cleanup_callback = cleanup_callback or self._cleanup_comfy
        self._stop = threading.Event()
        self._heartbeat_stop = threading.Event()
        self._wake = threading.Event()
        self._scheduler: threading.Thread | None = None
        self._heartbeat: threading.Thread | None = None
        self.process_runner = SupervisedProcessRunner(
            settings, store, self.owner_id, self._stop)
        self.agent_runner = AgentJobRunner(
            settings, store, self.owner_id, self._stop, self.process_runner,
            self._job_environment, self._export_job_chat,
            self._cleanup_safely, self._job_may_own_gpu, command_builder)
        self.generation_runner = GenerationWorkerRunner(
            settings, store, self.owner_id, self.process_runner,
            self._job_environment, self._export_job_chat,
            self._cleanup_safely, self._validate_generation_job,
            self._verify_generation_completion)
        self.movie_runner = MovieJobRunner(
            settings, store, self.owner_id, self.process_runner,
            self._job_environment, self._export_job_chat)
        self._runners: dict[str, JobRunner] = {
            "chat": self.agent_runner,
            "generate": self.generation_runner,
            "export_movie": self.movie_runner,
        }

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
        had_gpu_process = any(
            job.owner_id == self.owner_id and self._job_may_own_gpu(job)
            for job in self.store.active_jobs()
        )
        self.process_runner.terminate_all()
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
        self.process_runner.terminate_job(job.id)
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
        try:
            runner = self._runners[job.kind]
        except KeyError:
            self.store.fail(
                job.id, f"Unsupported Studio job kind: {job.kind}", self.owner_id)
            self._export_job_chat(job)
            return
        runner.execute(job)

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
        if not self.process_runner.terminate_orphan_job(job):
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

    def _job_may_own_gpu(self, job: Job) -> bool:
        return (
            job.profile == self.settings.studio_profile
            and job.kind != "export_movie"
        )

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
