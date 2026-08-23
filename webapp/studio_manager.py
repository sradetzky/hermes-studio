from __future__ import annotations

import logging
import os
import re
import signal
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from webapp.config import Settings
from webapp.hermes_events import HermesSessionEventBridge
from webapp.job_store import JobStore, JobStoreError
from webapp.models import Job


log = logging.getLogger(__name__)
SESSION_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
CommandBuilder = Callable[[Job, str | None], list[str]]
PROFILE_TOOLSETS = {
    "studio-storyboarder": "file,terminal,skills",
    "studio-prompt-engineer": "file,terminal,skills",
    "studio-reviewer": "file,terminal,vision,skills",
    "studio-illustrator": "file,terminal,skills",
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
        self._heartbeat = threading.Thread(
            target=self._heartbeat_loop,
            name=f"studio-heartbeat-{self.owner_id[:8]}",
            daemon=False,
        )
        self._heartbeat.start()
        self._scheduler = threading.Thread(
            target=self._scheduler_loop,
            name=f"studio-scheduler-{self.owner_id[:8]}",
            daemon=False,
        )
        self._scheduler.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        with self._process_lock:
            processes = list(self._processes.values())
        had_gpu_process = any(
            job.owner_id == self.owner_id
            and job.profile == self.settings.studio_profile
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
                self._export_chat(job.project)
        self._heartbeat_stop.set()
        if self._heartbeat:
            self._heartbeat.join(timeout=5)
        self.store.unregister_worker(self.owner_id)

    def submit_chat(self, project: str, clip_id: str, message: str,
                    profile: str | None = None) -> Job:
        chat_path = self._chat_path(project)
        self.store.import_chat_if_empty(project, chat_path)
        job = self.store.create_chat_job(
            project, message, profile or self.settings.studio_profile,
            clip_id=clip_id)
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
                self._export_chat(job.project)
                break
            self._execute(job)

    def _heartbeat_loop(self) -> None:
        while not self._heartbeat_stop.wait(1):
            try:
                self.store.heartbeat_worker(self.owner_id)
            except Exception:
                log.exception("Could not update Studio worker heartbeat")

    def _execute(self, job: Job) -> None:
        session_id = self.store.get_session(job.project, job.profile)
        command = self.command_builder(job, session_id)
        bridge = HermesSessionEventBridge(
            self.store,
            self.settings,
            job,
            source="studio-web",
            started_at=time.time(),
            session_id=session_id,
        )
        bridge.prepare()
        process: subprocess.Popen | None = None
        try:
            with self._process_lock:
                if self._stop.is_set():
                    self.store.fail(
                        job.id, "Studio server stopped", self.owner_id)
                    self._export_chat(job.project)
                    return
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=self._job_environment(job),
                )
                self.store.set_pid(job.id, self.owner_id, process.pid)
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
                if job.profile == self.settings.studio_profile:
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
                self._export_chat(job.project)
                return
            if process.returncode:
                if self._stop.is_set():
                    return
                error = f"Studio agent failed ({process.returncode})"
                log.error(
                    "Studio agent failed (%d): %s",
                    process.returncode, stderr.strip(),
                )
                if job.profile == self.settings.studio_profile:
                    self._cleanup_safely()
                self.store.fail(
                    job.id, error, self.owner_id,
                )
                self._export_chat(job.project)
                return
            reply = stdout.strip()
            if not reply:
                if job.profile == self.settings.studio_profile:
                    self._cleanup_safely()
                self.store.fail(
                    job.id, "Studio agent returned an empty reply", self.owner_id)
                self._export_chat(job.project)
                return
            match = SESSION_RE.search(stderr)
            self.store.complete(
                job.id, self.owner_id, reply,
                match.group(1) if match else bridge.session_id,
            )
            self._export_chat(job.project)
        except Exception as exc:
            log.exception("Studio job %s failed", job.id)
            if process:
                self._terminate_process(process)
            if job.profile == self.settings.studio_profile:
                self._cleanup_safely()
            try:
                self.store.fail(job.id, str(exc), self.owner_id)
                self._export_chat(job.project)
            except Exception:
                log.exception("Could not persist failure for job %s", job.id)
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
            "chat", "-Q", "-t", toolsets, "--source", "studio-web",
            "-q", self._agent_query(job),
        ]
        return command

    def _agent_query(self, job: Job) -> str:
        project_path = self.settings.studio_root / "projects" / job.project
        clip_path = project_path / "clips" / job.clip_id
        return (
            "Exact Studio context (do not guess or fuzzy-match paths):\n"
            f"Project ID: {job.project}\n"
            f"Project path: {project_path}\n"
            f"Active clip ID: {job.clip_id}\n"
            f"Active clip path: {clip_path}\n"
            "Project chat and references are shared. Read or write prompt, settings, "
            "and generation files only under the active clip path.\n\n"
            f"User request:\n{job.message}"
        )

    def _job_environment(self, job: Job) -> dict[str, str]:
        environment = os.environ.copy()
        environment.update({
            "DESIGN_STUDIO_ROOT": str(self.settings.studio_root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(self.settings.runtime_root),
            "HERMES_STUDIO_JOB_ID": job.id,
            "HERMES_STUDIO_PROJECT": job.project,
            "HERMES_STUDIO_CLIP": job.clip_id,
            "HERMES_STUDIO_CLIP_PATH": str(
                self.settings.studio_root / "projects" / job.project /
                "clips" / job.clip_id),
            "HERMES_STUDIO_PROFILE": job.profile,
        })
        return environment

    def _chat_path(self, project: str) -> Path:
        return self.settings.studio_root / "projects" / project / "chat.jsonl"

    def _export_chat(self, project: str) -> None:
        try:
            self.store.export_chat(project, self._chat_path(project))
        except Exception:
            log.exception("Could not export chat for project %s", project)

    def _recover_running_jobs(self) -> None:
        while stale := self.store.claim_stale_running(
                self.owner_id,
                time.time() - self.settings.worker_lease_timeout_seconds):
            self._recover_claimed_job(stale)

    def _recover_claimed_job(self, job: Job) -> None:
        if job.pid:
            self._terminate_orphan_pid(job.pid)
        if job.profile == self.settings.studio_profile:
            self._cleanup_safely()
        self.store.fail(
            job.id, "Studio worker lease expired during execution",
            self.owner_id)
        self._export_chat(job.project)

    def _communicate(self, process: subprocess.Popen,
                     bridge: HermesSessionEventBridge) -> tuple[str, str]:
        deadline = time.monotonic() + self.settings.job_timeout_seconds
        while True:
            bridge.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(
                    process.args, self.settings.job_timeout_seconds)
            try:
                output = process.communicate(timeout=min(1.0, remaining))
                bridge.poll()
                return output
            except subprocess.TimeoutExpired:
                self.store.heartbeat_worker(self.owner_id)

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
    def _terminate_orphan_pid(pid: int) -> None:
        cmdline = Path(f"/proc/{pid}/cmdline")
        try:
            command = cmdline.read_bytes()
        except OSError:
            return
        if b"hermes" not in command:
            log.error("Refusing to terminate non-Hermes orphan pid %d", pid)
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        for _ in range(20):
            if not Path(f"/proc/{pid}").exists():
                return
            time.sleep(0.1)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def _cleanup_comfy(self) -> None:
        prompt = (
            "Use only comfyui MCP tools. Cancel the running ComfyUI job and "
            "all pending jobs, verify the queue is stopped, then call "
            "clear_vram with unload_models=true and free_memory=true. "
            "Reply only with the final queue and VRAM status."
        )
        try:
            result = subprocess.run(
                [
                    self.settings.hermes_command,
                    "-p", self.settings.studio_profile,
                    "chat", "-Q", "-t", "comfyui", "-q", prompt,
                ],
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode:
                log.error(
                    "ComfyUI cleanup failed (%d): %s",
                    result.returncode, result.stderr.strip())
        except Exception:
            log.exception("Could not cancel ComfyUI work during job cleanup")

    def _cleanup_safely(self) -> None:
        try:
            self.cleanup_callback()
        except Exception:
            log.exception("Studio cleanup callback failed")
