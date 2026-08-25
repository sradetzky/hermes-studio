from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from studio_core.job_store import JobStore
from studio_core.models import Job
from webapp.config import Settings


log = logging.getLogger(__name__)


class ProcessCancelled(RuntimeError):
    """The manager stopped before the child process could start."""


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class EventBridge(Protocol):
    def poll(self) -> None: ...


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
        item
        for item in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if item
    }


class SupervisedProcessRunner:
    """Own supervised child launch, timeout, termination, and recovery."""

    def __init__(
        self,
        settings: Settings,
        store: JobStore,
        owner_id: str,
        stop: threading.Event,
    ) -> None:
        self.settings = settings
        self.store = store
        self.owner_id = owner_id
        self.stop = stop
        self._processes: dict[str, subprocess.Popen[str]] = {}
        self._lock = threading.Lock()

    def supervised_command(self, command: list[str]) -> list[str]:
        return [
            sys.executable,
            str(self.settings.repo / "scripts" / "supervised_exec.py"),
            str(os.getpid()),
            *command,
        ]

    def run(
        self,
        job: Job,
        command: list[str],
        environment: dict[str, str],
        bridge: EventBridge | None = None,
    ) -> ProcessResult:
        process: subprocess.Popen[str] | None = None
        try:
            with self._lock:
                if self.stop.is_set():
                    raise ProcessCancelled("Studio server stopped")
                process = subprocess.Popen(
                    self.supervised_command(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    start_new_session=True,
                    env=environment,
                )
                self.store.set_process(
                    job.id,
                    self.owner_id,
                    process.pid,
                    process_start_time(process.pid),
                )
                self._processes[job.id] = process
            stdout, stderr = self._communicate(process, bridge)
            return ProcessResult(process.returncode, stdout, stderr)
        except BaseException:
            if process is not None:
                self.terminate_process(process)
                process.communicate()
            raise
        finally:
            with self._lock:
                self._processes.pop(job.id, None)

    def _communicate(
        self,
        process: subprocess.Popen[str],
        bridge: EventBridge | None,
    ) -> tuple[str, str]:
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

    def terminate_job(self, job_id: str) -> None:
        with self._lock:
            process = self._processes.get(job_id)
        if process is not None:
            self.terminate_process(process)

    def terminate_all(self) -> None:
        with self._lock:
            processes = list(self._processes.values())
        for process in processes:
            self.terminate_process(process)

    @staticmethod
    def terminate_process(process: subprocess.Popen) -> None:
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
    def _job_process_tokens(job: Job) -> set[bytes]:
        return {
            f"HERMES_STUDIO_JOB_ID={job.id}".encode(),
            f"HERMES_STUDIO_PROJECT={job.project}".encode(),
            f"HERMES_STUDIO_CLIP={job.clip_id}".encode(),
            f"HERMES_STUDIO_PROFILE={job.profile}".encode(),
        }

    @classmethod
    def orphan_identity_matches(cls, job: Job) -> bool:
        if job.pid is None or job.pid_start_time is None:
            log.error(
                "Refusing to terminate orphan job %s without a complete process identity",
                job.id,
            )
            return False
        try:
            if process_start_time(job.pid) != job.pid_start_time:
                log.error(
                    "Refusing to terminate reused orphan pid %d for job %s",
                    job.pid,
                    job.id,
                )
                return False
            if os.getpgid(job.pid) != job.pid:
                log.error(
                    "Refusing to terminate orphan pid %d outside its own process group",
                    job.pid,
                )
                return False
            environment = process_environment(job.pid)
        except (OSError, ProcessLookupError, ValueError):
            return False
        if not cls._job_process_tokens(job).issubset(environment):
            log.error(
                "Refusing to terminate orphan pid %d without exact job ownership",
                job.pid,
            )
            return False
        return True

    @staticmethod
    def process_stopped(pid: int, expected_start_time: int | None) -> bool:
        try:
            value = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            command_end = value.rfind(")")
            if command_end < 0:
                return True
            fields = value[command_end + 2:].split()
            if not fields or fields[0] == "Z":
                return True
            return (
                expected_start_time is not None
                and int(fields[19]) != expected_start_time
            )
        except (OSError, ValueError, IndexError):
            return True

    @classmethod
    def terminate_orphan_process(cls, job: Job) -> bool:
        if not cls.orphan_identity_matches(job):
            return False
        assert job.pid is not None
        try:
            os.killpg(job.pid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        for _ in range(20):
            if cls.process_stopped(job.pid, job.pid_start_time):
                return True
            time.sleep(0.1)
        if not cls.orphan_identity_matches(job):
            return cls.process_stopped(job.pid, job.pid_start_time)
        try:
            os.killpg(job.pid, signal.SIGKILL)
        except ProcessLookupError:
            return True
        for _ in range(20):
            if cls.process_stopped(job.pid, job.pid_start_time):
                return True
            time.sleep(0.1)
        return False

    @classmethod
    def matching_job_processes(cls, job: Job) -> list[tuple[int, int]]:
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
    def terminate_orphan_job(cls, job: Job) -> bool:
        if job.pid is not None and job.pid_start_time is not None:
            if cls.terminate_orphan_process(job):
                return True
        matches = cls.matching_job_processes(job)
        if not matches:
            return True
        for pid, start_time in matches:
            candidate = replace(job, pid=pid, pid_start_time=start_time)
            if not cls.terminate_orphan_process(candidate):
                return False
        return not cls.matching_job_processes(job)
