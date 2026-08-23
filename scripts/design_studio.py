#!/usr/bin/env python3
"""design_studio.py — core library + CLI for the Hermes Studio.

Manages the on-disk project structure (source of truth) and wraps H3
generation via the proven minimax-h3-run runner.

Root resolution order:
  1. --root flag / DESIGN_STUDIO_ROOT env var
  2. ~/repos/hermes-studio/studio-root

Usage:
  python3 scripts/design_studio.py create-project my-idea "A brief..."
  python3 scripts/design_studio.py list-projects
  python3 scripts/design_studio.py list-clips 2026-08-23_my-idea
  python3 scripts/design_studio.py write-prompt 2026-08-23_my-idea clip-001 "..."
  python3 scripts/design_studio.py append-chat my-idea user "hello"
  python3 scripts/design_studio.py generate my-idea clip-001 --handoff h3_handoff_x.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import ExitStack
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""} and str(REPO_ROOT) not in sys.path:
    # Direct `python scripts/design_studio.py` execution needs the repo package
    # root for shared runtime modules. Installed/package imports need no shim.
    sys.path.insert(0, str(REPO_ROOT))

from webapp.clip_store import ClipStore
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_publish_directory,
    copy_opened_file,
    open_regular_beneath,
    open_regular_file,
    read_opened_text,
)

RUN_H3 = Path.home() / ".hermes/skills/minimax-h3-run/scripts/run_h3.py"
KREA2 = Path(__file__).resolve().parent / "krea2_image.py"
COMFY_ROOT = Path.home() / "ComfyUI"
COMFY_OUTPUT = COMFY_ROOT / "output"
GROK_IMAGE_OUTPUT = (Path.home() / ".hermes" / "profiles" / "studio-grok" /
                     "cache" / "images")
DEFAULT_ROOT = REPO_ROOT / "studio-root"
DEFAULT_RUNTIME = DEFAULT_ROOT.parent / ".runtime"
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
CLIP_STORE = ClipStore()


def free_comfy_vram() -> dict:
    """Cleanup for legacy direct execution; production uses MCP clear_vram."""
    request = urllib.request.Request(
        "http://127.0.0.1:8188/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def interrupt_comfy() -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8188/interrupt", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def studio_root(override: str | None = None) -> Path:
    root = override or os.environ.get("DESIGN_STUDIO_ROOT") or str(DEFAULT_ROOT)
    p = Path(root).expanduser().resolve()
    for sub in ("projects", "shared", "tmp"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-_]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError(f"cannot slugify project name: {name!r}")
    return s


def project_path(root: Path, name: str, must_exist: bool = True) -> Path:
    """Resolve a project by exact folder name. No fuzzy matching."""
    projects_path = root / "projects"
    if projects_path.is_symlink():
        raise ValueError("projects directory may not be a symlink")
    projects = projects_path.resolve()
    if not must_exist:
        today = _dt.date.today().isoformat()
        return projects / f"{today}_{slugify(name)}"
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid project id: {name!r}")
    candidate = projects / name
    if candidate.is_symlink():
        raise ValueError(f"project may not be a symlink: {name!r}")
    p = candidate.resolve()
    if p.parent != projects:
        raise ValueError(f"project escapes projects directory: {name!r}")
    if p.is_dir():
        return p
    raise FileNotFoundError(
        f"project {name!r} not found; use an exact folder name (see list-projects)")


# ---------------------------------------------------------------- project mgmt

def create_project(root: Path, name: str, brief: str = "") -> Path:
    pp = project_path(root, name, must_exist=False)
    if os.path.lexists(pp):
        raise FileExistsError(f"project already exists: {pp}")
    pp.mkdir(parents=True)
    try:
        for sub in ("references", "final", "research"):
            (pp / sub).mkdir()
        (pp / "brief.md").write_text(
            f"# {name}\n\n{brief}\n\nCreated {_dt.datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8")
        (pp / "chat.jsonl").touch()
        CLIP_STORE.initialize(pp, name)
    except Exception:
        shutil.rmtree(pp, ignore_errors=True)
        raise
    return pp


def list_projects(root: Path) -> list[str]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    return sorted(d.name for d in projects.iterdir()
                  if d.is_dir() and not d.is_symlink())


# ------------------------------------------------------------- prompt & chat

def clip_path(root: Path, project: str, clip_id: str) -> Path:
    """Resolve one exact manifest-owned clip ID within an exact project."""
    pp = project_path(root, project)
    return CLIP_STORE.resolve_clip(pp, clip_id)


def write_prompt(root: Path, project: str, clip_id: str, prompt: str) -> Path:
    clip = clip_path(root, project, clip_id)
    out = clip / "current_prompt.txt"
    if os.path.lexists(out) and (out.is_symlink() or not out.is_file()):
        raise ValueError("current prompt is not a regular clip file")
    temp = clip / f".{uuid.uuid4().hex}.current-prompt"
    try:
        temp.write_text(prompt.rstrip() + "\n", encoding="utf-8")
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)
    return out


def append_chat(root: Path, project: str, role: str, content: str) -> None:
    pp = project_path(root, project)
    configured_root = Path(
        os.environ.get("DESIGN_STUDIO_ROOT", DEFAULT_ROOT)
    ).expanduser().resolve()
    if root.resolve() == configured_root:
        # Lazy import avoids a module cycle: webapp routes import this CLI core.
        from webapp.job_store import JobStore

        runtime = Path(os.environ.get(
            "HERMES_STUDIO_RUNTIME_ROOT", DEFAULT_RUNTIME
        )).expanduser().resolve()
        store = JobStore(runtime / "studio.db")
        store.initialize()
        store.import_chat_if_empty(pp.name, pp / "chat.jsonl")
        store.append_external_event(pp.name, role, content)
        store.export_chat(pp.name, pp / "chat.jsonl")
        return
    entry = {"role": role,
             "content": content,
             "ts": _dt.datetime.now().isoformat(timespec="seconds")}
    # O_APPEND + single write: atomic enough for line-sized records even with
    # concurrent writers (threads / CLI + webapp).
    data = json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"
    lock_path = pp / ".chat.lock"
    chat_path = pp / "chat.jsonl"
    if lock_path.is_symlink() or chat_path.is_symlink():
        raise ValueError("project chat files may not be symlinks")
    lock_fd = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fd = os.open(
                chat_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- generation

def next_generation_dir(root: Path, project: str, clip_id: str) -> Path:
    clip = clip_path(root, project, clip_id)
    generations = clip / "generations"
    if (generations.is_symlink() or not generations.is_dir()
            or generations.resolve().parent != clip):
        raise ValueError("generations directory is not a regular clip directory")
    numbers = []
    for entry in generations.iterdir():
        if not entry.name.isdigit():
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise ValueError(f"generation entry is unsafe: {entry.name}")
        numbers.append(int(entry.name))
    return generations / f"{max(numbers, default=0) + 1:03d}"


def read_project_text(project: Path, filename: str, limit: int | None = None,
                      *, required: bool = False) -> str:
    """Read safe metadata, returning empty for unsafe optional entries."""
    if Path(filename).name != filename:
        raise ValueError(f"invalid project filename: {filename!r}")
    path = project / filename
    try:
        with open_regular_file(path) as opened:
            value = read_opened_text(opened)
    except FileNotFoundError as exc:
        if required:
            raise ValueError(f"{filename} is missing") from exc
        return ""
    except (SafeFilesystemError, OSError, UnicodeDecodeError) as exc:
        if required:
            raise ValueError(f"{filename} is not a regular file") from exc
        return ""
    return value[:limit] if limit is not None else value


def archive_outputs(root: Path, project: str, clip_id: str,
                    outputs: list[str], metadata: dict | None = None,
                    source_root: Path | None = None,
                    transport: str = "comfyui-mcp",
                    prompt_text: str | None = None) -> Path:
    """Archive outputs beneath one exact clip from one trusted source root."""
    clip = clip_path(root, project, clip_id)
    output_root = (source_root or COMFY_OUTPUT).resolve()
    with ExitStack() as source_descriptors:
        sources = []
        for output in outputs:
            try:
                source = source_descriptors.enter_context(
                    open_regular_beneath(output_root, output))
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"ComfyUI output not found: {output}") from exc
            except SafeFilesystemError as exc:
                raise ValueError(
                    "output may not be a symlink, special file, or escape "
                    f"the ComfyUI output directory: {output!r}"
                ) from exc
            if source.name in {"prompt.txt", "settings.json", "meta.json"}:
                raise ValueError(f"output filename is reserved: {source.name}")
            sources.append(source)
        if not sources:
            raise ValueError("at least one output file is required")

        generations = clip / "generations"
        if (generations.is_symlink() or not generations.is_dir()
                or generations.resolve().parent != clip):
            raise ValueError("generations directory is not a regular clip directory")
        lock_path = clip / ".generation-archive.lock"
        if lock_path.is_symlink():
            raise ValueError("generation archive lock may not be a symlink")
        lock_fd = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600)
        with os.fdopen(lock_fd, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            gen_dir = next_generation_dir(root, project, clip_id)
            staging = generations / f".publishing-{uuid.uuid4().hex}"
            staging.mkdir()
            copied = []
            try:
                for source in sources:
                    target = staging / source.name
                    if target.exists():
                        raise FileExistsError(
                            f"duplicate output filename: {source.name}")
                    copy_opened_file(source, target)
                    copied.append(target.name)
                archived_prompt = (
                    read_project_text(clip, "current_prompt.txt", required=True)
                    if prompt_text is None else prompt_text.rstrip() + "\n"
                )
                (staging / "prompt.txt").write_text(
                    archived_prompt, encoding="utf-8")
                settings_path = clip / "current_generation.json"
                try:
                    with open_regular_file(settings_path) as settings:
                        copy_opened_file(settings, staging / "settings.json")
                except FileNotFoundError:
                    # JSON null is the stable snapshot for an unsaved state.
                    (staging / "settings.json").write_text(
                        "null\n", encoding="utf-8")
                except SafeFilesystemError as exc:
                    raise ValueError(
                        "current generation settings are not a regular clip file"
                    ) from exc
                meta = {
                    **(metadata or {}),
                    "generated": _dt.datetime.now().isoformat(timespec="seconds"),
                    "transport": transport,
                    "files": copied,
                    "sources": [str(source.path) for source in sources],
                }
                (staging / "meta.json").write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8")
                atomic_publish_directory(staging, gen_dir)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return gen_dir


def dispatch_profile(root: Path, project: str, profile: str, task: str,
                     timeout: int = 1800) -> str:
    """Run one serialized local specialist handoff with a persistent session."""
    if profile not in LOCAL_SPECIALIST_PROFILES:
        raise ValueError(f"unsupported Studio specialist profile: {profile}")
    pp = project_path(root, project)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{pp.name}.{profile}"
    lock_path = root / "tmp" / ".profile-dispatch.lock"
    command = ["hermes", "-p", profile]
    session_id = None
    if session_file.exists():
        candidate = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            session_id = candidate
            command += ["-r", candidate]
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n\n"
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
            from webapp.config import Settings
            from webapp.hermes_events import HermesSessionEventBridge
            from webapp.job_store import JobStore

            settings = Settings.from_environment()
            store = JobStore(settings.database_path)
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
                "handoff.started",
                f"Handoff started: {profile}",
                status="running",
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
                        job_id, profile, "handoff.failed",
                        f"Handoff timed out after {timeout}s", status="failed")
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
                job_id, profile, "handoff.failed",
                f"{profile} failed", status="failed",
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
            job_id, profile, "handoff.completed",
            f"Handoff completed: {profile}", status="completed",
            detail={"session_id": resolved_session or ""})
    return reply


def dispatch_grok(root: Path, project: str, task: str,
                  timeout: int = 600) -> str:
    """Run a persistent per-project task on the xAI backup profile."""
    pp = project_path(root, project)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{pp.name}.studio-grok"
    cmd = ["hermes", "-p", "studio-grok"]
    if session_file.exists():
        session_id = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            cmd += ["-r", session_id]
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n\n"
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


def run_generation(root: Path, project: str, clip_id: str,
                   handoff: str | None = None,
                   extra_args: list[str] | None = None,
                   timeout: int = 7200, dry_run: bool = False) -> dict:
    """Submit an H3 generation via the proven run_h3.py runner, then archive."""
    clip_path(root, project, clip_id)
    cmd = [sys.executable, str(RUN_H3), "--comfy-root", str(COMFY_ROOT),
           "-o", "/dev/stdout"]
    if handoff:
        h = Path(handoff).expanduser()
        if not h.exists():  # same archive fallback run_h3.py uses
            h = Path.home() / "Documents/MinimaxH3" / h.name
        if not h.exists():
            raise FileNotFoundError(f"handoff not found: {handoff}")
        cmd += ["--handoff", str(h)]
    if extra_args:
        cmd += extra_args

    if dry_run:
        result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True)
        return {"dry_run": True, "ok": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        interrupt_comfy()
        free_comfy_vram()
        raise
    cleanup = free_comfy_vram()

    summary = {}
    try:  # runner writes its summary JSON via -o /dev/stdout
        summary = json.loads(result.stdout[result.stdout.index("{"):])
    except Exception:
        pass
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-3000:], "summary": summary,
                "vram_cleanup": cleanup}

    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "handoff": str(handoff or ""), "runner": str(RUN_H3),
            "vram_cleanup": cleanup, **summary}

    video = None
    for key in ("video", "output", "path"):  # locate produced file in summary
        v = summary.get(key)
        if v and Path(v).exists():
            video = Path(v)
            break
    if not video:
        return {"ok": False, "error": "runner completed without a video output",
                "summary": summary, "vram_cleanup": cleanup}
    meta["source"] = str(video)
    gen_dir = archive_outputs(
        root, project, clip_id, [str(video)], meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct")
    # preview.jpg is created by the reviewer/user; do not extract frames automatically.
    return {"ok": cleanup.get("ok", False), "generation": str(gen_dir),
            "meta": meta}


def run_image_generation(root: Path, project: str, clip_id: str,
                         recipe: str, prompt: str = "",
                         image: str | None = None, extra_args: list[str] | None = None,
                         timeout: int = 900) -> dict:
    """Krea 2 still image via scripts/krea2_image.py, archived like generations."""
    clip_path(root, project, clip_id)
    prefix = f"studio_{slugify(project)}"
    cmd = [sys.executable, str(KREA2), "--recipe", recipe, "--prefix", prefix]
    if prompt:
        cmd += ["--prompt", prompt]
    if image:
        cmd += ["--image", str(Path(image).expanduser())]
    if extra_args:
        cmd += extra_args

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    seed, prompt_id, files = None, None, []
    # runner prints a one-line summary JSON then an indented result JSON
    dec, pos = json.JSONDecoder(), 0
    while True:
        start = result.stdout.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = dec.raw_decode(result.stdout[start:])
        except ValueError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            seed = obj.get("seed", seed)
            prompt_id = obj.get("prompt_id", prompt_id)
            f = obj.get("files")
            if isinstance(f, list) and f:
                files = [x for x in f if isinstance(x, str)]
        pos = start + end
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-2000:] or result.stdout[-2000:]}

    if not files:
        return {"ok": False, "error": "runner completed without image outputs",
                "prompt_id": prompt_id}
    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "kind": "image", "recipe": recipe, "prompt": prompt,
            "input_image": str(image or ""), "seed": seed,
            "prompt_id": prompt_id}
    gen_dir = archive_outputs(
        root, project, clip_id, files, meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct", prompt_text=prompt)
    meta = json.loads((gen_dir / "meta.json").read_text(encoding="utf-8"))
    return {"ok": True, "generation": str(gen_dir), "meta": meta}


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="studio root (default: $DESIGN_STUDIO_ROOT or repo studio-root/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create-project")
    sp.add_argument("name"); sp.add_argument("brief", nargs="*", default=[])

    sub.add_parser("list-projects")

    sp = sub.add_parser("list-clips")
    sp.add_argument("project")

    sp = sub.add_parser("create-clip")
    sp.add_argument("project"); sp.add_argument("title")

    sp = sub.add_parser("update-clip")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--title")
    enabled = sp.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_true")
    enabled.add_argument("--disable", dest="enabled", action="store_false")
    sp.set_defaults(enabled=None)

    sp = sub.add_parser("reorder-clips")
    sp.add_argument("project"); sp.add_argument("clips", nargs="+")

    sp = sub.add_parser("select-take")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("generation", nargs="?"); sp.add_argument("filename", nargs="?")
    sp.add_argument("--clear", action="store_true")

    sp = sub.add_parser("write-prompt")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("prompt", nargs="+")

    sp = sub.add_parser("append-chat")
    sp.add_argument("project"); sp.add_argument("role"); sp.add_argument("content")

    sp = sub.add_parser("generate")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--handoff")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra run_h3.py arg, repeatable. '--arg --turbo' style: use e.g. --arg=--mp --arg=0.9")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("generate-image")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--recipe", required=True,
                    choices=["t2i", "t2i-nvfp4", "style-ref", "upscale", "edit"])
    sp.add_argument("--prompt", default="")
    sp.add_argument("--image", help="input image for style-ref / upscale / edit")
    sp.add_argument("--ref-boost", type=float, default=None, help="edit recipe")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra krea2_image.py arg, e.g. --arg=--aspect --arg=16:9")
    sp.add_argument("--timeout", type=int, default=900)

    sp = sub.add_parser("archive-output")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--kind", default="")
    sp.add_argument("--recipe", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("archive-grok")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("dispatch-grok")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=600)

    sp = sub.add_parser("dispatch-profile")
    sp.add_argument("project")
    sp.add_argument("profile", choices=sorted(LOCAL_SPECIALIST_PROFILES))
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=1800)

    args = ap.parse_args(argv)
    root = studio_root(args.root)

    if args.cmd == "create-project":
        print(create_project(root, args.name, " ".join(args.brief)))
    elif args.cmd == "list-projects":
        print("\n".join(list_projects(root)) or "(no projects)")
    elif args.cmd == "list-clips":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.describe(pp), indent=2, ensure_ascii=False))
    elif args.cmd == "create-clip":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.create_clip(pp, args.title), indent=2, ensure_ascii=False))
    elif args.cmd == "update-clip":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.update_clip(
            pp, args.clip, title=args.title, enabled=args.enabled),
            indent=2, ensure_ascii=False))
    elif args.cmd == "reorder-clips":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.reorder(pp, args.clips), indent=2, ensure_ascii=False))
    elif args.cmd == "select-take":
        if args.clear and (args.generation is not None or args.filename is not None):
            ap.error("--clear cannot be combined with a generation or filename")
        if not args.clear and (args.generation is None or args.filename is None):
            ap.error("selection requires both generation and filename, or --clear")
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.select_take(
            pp, args.clip, None if args.clear else args.generation,
            None if args.clear else args.filename), indent=2, ensure_ascii=False))
    elif args.cmd == "write-prompt":
        print(write_prompt(
            root, args.project, args.clip, " ".join(args.prompt)))
    elif args.cmd == "append-chat":
        append_chat(root, args.project, args.role, args.content)
        print("appended")
    elif args.cmd == "generate":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        out = run_generation(root, args.project, args.clip, args.handoff, extra,
                             timeout=args.timeout, dry_run=args.dry_run)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") or out.get("dry_run") else 1
    elif args.cmd == "generate-image":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        if args.ref_boost is not None:
            extra += ["--ref-boost", str(args.ref_boost)]
        out = run_image_generation(
            root, args.project, args.clip, args.recipe, args.prompt,
            image=args.image, extra_args=extra, timeout=args.timeout)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    elif args.cmd == "archive-output":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        if args.kind:
            meta["kind"] = args.kind
        if args.recipe:
            meta["recipe"] = args.recipe
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta))
    elif args.cmd == "archive-grok":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        meta.setdefault("kind", "image")
        meta.setdefault("recipe", "grok-imagine-image-quality")
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta,
            source_root=GROK_IMAGE_OUTPUT, transport="xai-imagine"))
    elif args.cmd == "dispatch-grok":
        print(dispatch_grok(root, args.project, args.task, args.timeout))
    elif args.cmd == "dispatch-profile":
        print(dispatch_profile(
            root, args.project, args.profile, args.task, args.timeout))
    return 0


if __name__ == "__main__":
    sys.exit(main())
