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
from studio_core.job_contracts import (
    ChatScope,
    JobEventType,
    JobKind,
    JobPhase,
)
from studio_core.projects import project_path
from webapp.config import Settings

from studio_core.job_store import ActiveJobError, JobStore, JobStoreError
from studio_core.models import Job, JobStatus
from webapp.agent_runner import AgentJobRunner, CommandBuilder
from webapp.generation_job_service import GenerationJobService
from webapp.generation_runner import GenerationWorkerRunner
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
            self._job_environment, self._export_job_chat, command_builder)
        self.generation_jobs = GenerationJobService(settings)
        self.generation_runner = GenerationWorkerRunner(
            settings, store, self.owner_id, self.process_runner,
            self._job_environment, self._export_job_chat,
            self._cleanup_safely, self.generation_jobs)
        self.movie_runner = MovieJobRunner(
            settings, store, self.owner_id, self.process_runner,
            self._job_environment, self._export_job_chat)
        self._runners: dict[JobKind, JobRunner] = {
            JobKind.CHAT: self.agent_runner,
            JobKind.GENERATE: self.generation_runner,
            JobKind.EXPORT_MOVIE: self.movie_runner,
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
            job.owner_id == self.owner_id and self._job_owns_gpu(job)
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
                          settings_updated_at: str,
                          use_previous_take_last_frame: bool = False) -> Job:
        project_directory = project_path(self.settings.studio_root, project)
        with project_job_guard(project_directory):
            request = self.generation_jobs.build_request(
                project_directory, project, clip_id,
                prompt_sha256, settings_updated_at,
                use_previous_take_last_frame)
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
                    contract.to_dict(), sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False),
                self.settings.studio_profile,
            )
        self._export_chat(project)
        self._wake.set()
        return job


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
        if self._job_owns_gpu(job):
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
            "HERMES_STUDIO_CHAT_SCOPE": job.chat_scope.value,
            "HERMES_STUDIO_PROFILE": job.profile,
            "HERMES_STUDIO_JOB_KIND": job.kind.value,
        })
        return environment

    def _chat_path(self, project: str, clip_id: str = "") -> Path:
        project_path = self.settings.studio_root / "projects" / project
        if clip_id:
            return project_path / "clips" / clip_id / "chat.jsonl"
        return project_path / "chat.jsonl"

    @staticmethod
    def _scope_clip_id(job: Job) -> str:
        return job.clip_id if job.chat_scope is ChatScope.CLIP else ""

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
                    JobEventType.JOB_RECOVERY_BLOCKED,
                    "Recovery blocked: possible Studio process is still alive",
                    phase=JobPhase.RUNNING,
                )
            except Exception:
                log.exception("Could not record blocked recovery for job %s", job.id)
            return False
        if self._job_owns_gpu(job):
            self._cleanup_safely()
        self.store.fail(
            job.id, "Studio worker lease expired during execution",
            self.owner_id)
        self._export_job_chat(job)
        return True

    @staticmethod
    def _job_owns_gpu(job: Job) -> bool:
        return job.kind is JobKind.GENERATE

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
