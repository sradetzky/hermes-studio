from __future__ import annotations

import fcntl
import os
import re
import signal
import subprocess
import time
from dataclasses import replace
from pathlib import Path

from studio_core.hermes_events import HermesSessionEventBridge
from studio_core.job_contracts import JobEventType, JobPhase
from studio_core.job_store import JobStore
from studio_core.paths import StudioPaths
from studio_core.projects import clip_path, project_path

STUDIO_PATHS = StudioPaths.from_environment()
DEFAULT_RUNTIME = Path(__file__).resolve().parent.parent / ".runtime"
SESSION_ID_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
LOCAL_SPECIALIST_PROFILES = {
    "studio-storyboarder",
    "studio-prompt-engineer",
    "studio-reviewer",
    "studio-illustrator",
}
SPECIALIST_TOOLSETS = {
    "studio-storyboarder": "file,terminal,skills",
    "studio-prompt-engineer": "file,terminal,skills",
    "studio-reviewer": "file,terminal,vision,skills",
    "studio-illustrator": "file,terminal,skills",
}


class _ProfileStateLocator:
    def profile_state_path(self, profile: str) -> Path:
        return STUDIO_PATHS.profile_home(profile) / "state.db"


def _dispatch_clip_scope(
        root: Path, project: Path, clip_id: str | None) -> tuple[str, Path | None]:
    if clip_id is None:
        if (os.environ.get("HERMES_STUDIO_PROJECT", "").strip() == project.name
                and os.environ.get("HERMES_STUDIO_CHAT_SCOPE", "").strip()
                == "clip"):
            clip_id = os.environ.get("HERMES_STUDIO_CLIP", "").strip()
    if not clip_id:
        return "", None
    clip = clip_path(root, project.name, clip_id)
    return clip.name, clip

def _profile_session_path(
        session_dir: Path, project: str, profile: str, clip_id: str) -> Path:
    scope = f".{clip_id}" if clip_id else ""
    return session_dir / f"{project}{scope}.{profile}"

def dispatch_profile(root: Path, project: str, profile: str, task: str,
                     timeout: int = 1800, *, clip_id: str | None = None) -> str:
    """Run one serialized local specialist handoff with a persistent session."""
    if profile not in LOCAL_SPECIALIST_PROFILES:
        raise ValueError(f"unsupported Studio specialist profile: {profile}")
    pp = project_path(root, project)
    scoped_clip_id, scoped_clip = _dispatch_clip_scope(root, pp, clip_id)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = _profile_session_path(
        session_dir, pp.name, profile, scoped_clip_id)
    lock_path = root / "tmp" / ".profile-dispatch.lock"
    command = ["hermes", "-p", profile]
    session_id = None
    if session_file.exists():
        candidate = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            session_id = candidate
            command += ["-r", candidate]
    scope_context = (
        f"Conversation scope: clip\n"
        f"Active clip id: {scoped_clip_id}\n"
        f"Active clip path: {scoped_clip}\n"
        if scoped_clip else
        "Conversation scope: project. There is no active clip.\n"
    )
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n"
        f"{scope_context}\n"
        f"Handoff from the studio orchestrator:\n{task}"
    )
    command += [
        "chat", "-Q", "-t", SPECIALIST_TOOLSETS[profile],
        "--source", "studio-handoff",
        "-q", prompt,
    ]

    bridge = None
    store = None
    settings = None
    specialist_job = None
    bridge_type = None
    job_id = os.environ.get("HERMES_STUDIO_JOB_ID", "").strip()
    if job_id:
        try:
            runtime = Path(os.environ.get(
                "HERMES_STUDIO_RUNTIME_ROOT", DEFAULT_RUNTIME
            )).expanduser().resolve()
            settings = _ProfileStateLocator()
            store = JobStore(runtime / "studio.db")
            store.initialize()
            parent_job = store.get_job(job_id)
            specialist_job = replace(parent_job, profile=profile)
            bridge_type = HermesSessionEventBridge
        except Exception:
            store = None
            settings = None
            specialist_job = None
            bridge_type = None

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if store and settings and specialist_job and bridge_type:
            store.append_job_event(
                job_id,
                profile,
                JobEventType.HANDOFF_STARTED,
                f"Handoff started: {profile}",
                phase=JobPhase.RUNNING,
                detail={"task": task[:500]},
            )
            bridge = bridge_type(
                store,
                settings,
                specialist_job,
                source="studio-handoff",
                started_at=time.time(),
                session_id=session_id,
            )
            bridge.prepare()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if bridge:
                bridge.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.communicate()
                if store:
                    store.append_job_event(
                        job_id, profile, JobEventType.HANDOFF_FAILED,
                        f"Handoff timed out after {timeout}s",
                        phase=JobPhase.FAILED)
                raise TimeoutError(
                    f"Studio specialist {profile} timed out after {timeout}s")
            try:
                stdout, stderr = process.communicate(timeout=min(1.0, remaining))
                if bridge:
                    bridge.poll()
                break
            except subprocess.TimeoutExpired:
                continue

    if process.returncode:
        error = stderr.strip() or f"exit code {process.returncode}"
        if store:
            store.append_job_event(
                job_id, profile, JobEventType.HANDOFF_FAILED,
                f"{profile} failed", phase=JobPhase.FAILED,
                detail={"error": error[:500]})
        raise RuntimeError(f"{profile} failed ({process.returncode}): {error}")
    reply = stdout.strip()
    if not reply:
        raise RuntimeError(f"{profile} returned an empty reply")
    match = SESSION_ID_RE.search(stderr)
    resolved_session = match.group(1) if match else (
        bridge.session_id if bridge else session_id)
    if resolved_session:
        temp = session_file.with_suffix(session_file.suffix + ".tmp")
        temp.write_text(resolved_session + "\n", encoding="utf-8")
        temp.replace(session_file)
    if store:
        store.append_job_event(
            job_id, profile, JobEventType.HANDOFF_COMPLETED,
            f"Handoff completed: {profile}", phase=JobPhase.COMPLETED,
            detail={"session_id": resolved_session or ""})
    return reply

def dispatch_grok(root: Path, project: str, task: str,
                  timeout: int = 600, *, clip_id: str | None = None) -> str:
    """Run a persistent scope-local task on the xAI backup profile."""
    pp = project_path(root, project)
    scoped_clip_id, scoped_clip = _dispatch_clip_scope(root, pp, clip_id)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = _profile_session_path(
        session_dir, pp.name, "studio-grok", scoped_clip_id)
    cmd = ["hermes", "-p", "studio-grok"]
    if session_file.exists():
        session_id = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            cmd += ["-r", session_id]
    scope_context = (
        f"Conversation scope: clip\n"
        f"Active clip id: {scoped_clip_id}\n"
        f"Active clip path: {scoped_clip}\n"
        if scoped_clip else
        "Conversation scope: project. There is no active clip.\n"
    )
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n"
        f"{scope_context}\n"
        f"Task from the Studio orchestrator:\n{task}"
    )
    cmd += ["chat", "-Q", "-t",
            "web,x_search,image_gen,vision,file,terminal", "-q", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            f"studio-grok failed ({result.returncode}): {result.stderr.strip()}")
    reply = result.stdout.strip()
    if not reply:
        raise RuntimeError("studio-grok returned an empty reply")
    match = SESSION_ID_RE.search(result.stderr)
    if match:
        temp = session_file.with_suffix(".tmp")
        temp.write_text(match.group(1) + "\n", encoding="utf-8")
        temp.replace(session_file)
    return reply
