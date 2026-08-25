from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from collections.abc import Callable

from studio_core.hermes_events import HermesSessionEventBridge
from studio_core.job_contracts import (
    ChatJobPayload,
    ChatScope,
    JobEventType,
    JobKind,
    JobPhase,
)
from studio_core.job_store import JobStore
from studio_core.models import Job
from webapp.config import Settings
from webapp.process_runner import ProcessCancelled, SupervisedProcessRunner


log = logging.getLogger(__name__)
SESSION_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
CommandBuilder = Callable[[Job, str | None], list[str]]
PROFILE_TOOLSETS = {
    "studio": "file,terminal,vision,web,skills",
    "studio-storyboarder": "file,terminal,skills",
    "studio-prompt-engineer": "file,terminal,skills",
    "studio-reviewer": "file,terminal,vision,skills",
    "studio-illustrator": "file,terminal,skills",
}


class AgentJobRunner:
    """Execute scoped Hermes chat jobs and project their session events."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        owner_id: str,
        stop: threading.Event,
        process_runner: SupervisedProcessRunner,
        environment: Callable[[Job], dict[str, str]],
        export_chat: Callable[[Job], None],
        command_builder: CommandBuilder | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.owner_id = owner_id
        self.stop = stop
        self.process_runner = process_runner
        self.environment = environment
        self.export_chat = export_chat
        self.command_builder = command_builder or self.default_command

    @staticmethod
    def scope_clip_id(job: Job) -> str:
        return job.clip_id if job.chat_scope is ChatScope.CLIP else ""

    @staticmethod
    def session_source(job: Job) -> str:
        return f"studio-web:{job.id}"

    def default_command(self, job: Job, session_id: str | None) -> list[str]:
        command = [self.settings.hermes_command, "-p", job.profile]
        if session_id and re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            command += ["-r", session_id]
        toolsets = PROFILE_TOOLSETS.get(job.profile, "file,terminal,skills")
        command += [
            "chat",
            "-Q",
            "-t",
            toolsets,
            "--source",
            self.session_source(job),
            "-q",
            self.agent_query(job),
        ]
        return command

    def agent_query(self, job: Job) -> str:
        if job.kind is not JobKind.CHAT or not isinstance(
                job.payload, ChatJobPayload):
            raise ValueError("agent runner requires a validated chat payload")
        message = job.payload.message
        project_path = self.settings.studio_root / "projects" / job.project
        path_context = (
            "Path roots: use $HERMES_HOME for active-profile Hermes data and "
            "skills; use $HERMES_REAL_HOME for account files such as ComfyUI, "
            "Documents, and repos. Do not derive either root from $HOME or `~`.\n"
        )
        if job.chat_scope is ChatScope.PROJECT:
            target_context = (
                f"This migrated project-scope job explicitly targets clip "
                f"{job.clip_id}.\n"
                if job.clip_id
                else "There is no active clip.\n"
            )
            return (
                "Exact Studio context (do not guess or fuzzy-match paths):\n"
                f"{path_context}"
                f"Project ID: {job.project}\n"
                f"Project path: {project_path}\n"
                f"Conversation scope: project chat. {target_context}"
                "Work only on project-wide planning, continuity, shared references, "
                "research, or final assembly. Do not choose or mutate a clip unless "
                "the user explicitly identifies one.\n\n"
                f"User request:\n{message}"
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
        return context + f"User request:\n{message}"

    def _prepare_event_bridge(self, bridge: HermesSessionEventBridge) -> bool:
        deadline = time.monotonic() + 2.0
        while not self.stop.is_set():
            if bridge.prepare():
                return True
            if time.monotonic() >= deadline:
                return False
            try:
                self.store.heartbeat_worker(self.owner_id)
            except Exception:
                log.exception("Could not heartbeat while preparing Hermes events")
            self.stop.wait(0.1)
        return False

    def _fail(self, job: Job, error: str) -> None:
        self.store.fail(job.id, error, self.owner_id)
        self.export_chat(job)

    def execute(self, job: Job) -> None:
        try:
            session_id = self.store.get_session(
                job.project,
                job.profile,
                clip_id=self.scope_clip_id(job),
            )
            command = self.command_builder(job, session_id)
        except Exception as exc:
            error = f"Could not prepare Studio job: {exc}"
            self.store.append_job_event(
                job.id,
                job.profile,
                JobEventType.JOB_PREPARE,
                error,
                phase=JobPhase.FAILED,
            )
            self._fail(job, error)
            return
        bridge = HermesSessionEventBridge(
            self.store,
            self.settings,
            job,
            source=self.session_source(job),
            started_at=time.time(),
            session_id=session_id,
        )
        if not self._prepare_event_bridge(bridge):
            error = "Could not establish Hermes session event baseline"
            self.store.append_job_event(
                job.id,
                job.profile,
                JobEventType.JOB_PREPARE,
                error,
                phase=JobPhase.FAILED,
            )
            self._fail(job, error)
            return
        try:
            result = self.process_runner.run(
                job,
                command,
                self.environment(job),
                bridge,
            )
        except ProcessCancelled:
            self._fail(job, "Studio server stopped")
            return
        except subprocess.TimeoutExpired:
            self.store.append_job_event(
                job.id,
                job.profile,
                JobEventType.JOB_TIMEOUT,
                f"Exceeded the {self.settings.job_timeout_seconds}s job limit",
                phase=JobPhase.FAILED,
            )
            self._fail(
                job,
                f"Studio agent timed out after {self.settings.job_timeout_seconds}s",
            )
            return
        except Exception as exc:
            log.exception("Studio agent job %s failed", job.id)
            try:
                self._fail(job, str(exc))
            except Exception:
                log.exception("Could not persist failure for job %s", job.id)
            return
        if result.returncode:
            if self.stop.is_set():
                return
            error = f"Studio agent failed ({result.returncode})"
            log.error(
                "Studio agent failed (%d): %s",
                result.returncode,
                result.stderr.strip(),
            )
            self._fail(job, error)
            return
        reply = result.stdout.strip()
        if not reply:
            self._fail(job, "Studio agent returned an empty reply")
            return
        match = SESSION_RE.search(result.stderr)
        self.store.complete(
            job.id,
            self.owner_id,
            reply,
            match.group(1) if match else bridge.session_id,
        )
        self.export_chat(job)
