"""Studio web UI — thin read-mostly window onto the studio-root filesystem.

Run: .venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from scripts import design_studio as ds

REPO = Path(__file__).resolve().parent.parent
WEB = REPO / "webapp"
HERMES = "hermes"
STUDIO_PROFILE = "studio"
COMFY_OUTPUT = Path.home() / "ComfyUI" / "output"
STUDIO_ROOT = ds.studio_root(os.environ.get("DESIGN_STUDIO_ROOT"))
RUNTIME = REPO / ".runtime"
SESSIONS = RUNTIME / "sessions"
JOBS = RUNTIME / "jobs"
SESSIONS.mkdir(parents=True, exist_ok=True)
JOBS.mkdir(parents=True, exist_ok=True)
SESSION_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()
_jobs_guard = threading.Lock()
log = logging.getLogger(__name__)

ACTIVE_JOB_STATES = {"queued", "running"}
REFERENCE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".mov", ".webm", ".wav", ".mp3", ".flac", ".m4a",
}
MAX_REFERENCE_BYTES = 256 * 1024 * 1024
MAX_UPLOAD_FILES = 20

app = FastAPI(title="Hermes Studio")


# ------------------------------------------------------------------ helpers

def resolve_project(pid: str) -> Path:
    """Resolve an exact project id and map lookup errors to HTTP responses."""
    try:
        return ds.project_path(STUDIO_ROOT, pid)
    except FileNotFoundError:
        raise HTTPException(404, f"project not found: {pid}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def project_lock(pid: str) -> threading.Lock:
    with _project_locks_guard:
        return _project_locks.setdefault(pid, threading.Lock())


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_path(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise ValueError("invalid job id")
    return JOBS / f"{job_id}.json"


def write_job(job: dict) -> None:
    path = job_path(job["id"])
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(job, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def read_job(job_id: str) -> dict:
    try:
        path = job_path(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not path.is_file():
        raise HTTPException(404, "job not found")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        log.error("Corrupt job record %s: %s", path, exc)
        raise HTTPException(500, "corrupt job record")


def project_jobs(pid: str, limit: int = 20) -> list[dict]:
    jobs = []
    for path in JOBS.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("Skipping unreadable job record: %s", path)
            continue
        if job.get("project") == pid:
            jobs.append(job)
    jobs.sort(key=lambda job: job.get("created_at", ""), reverse=True)
    return jobs[:limit]


def update_job(job_id: str, **changes) -> dict:
    with _jobs_guard:
        job = read_job(job_id)
        job.update(changes)
        write_job(job)
        return job


def recover_interrupted_jobs() -> None:
    for path in JOBS.glob("*.json"):
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if job.get("status") in ACTIVE_JOB_STATES:
            job.update({
                "status": "failed",
                "finished_at": utc_now(),
                "error": "Studio server restarted before this job completed",
            })
            write_job(job)


recover_interrupted_jobs()


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError as exc:
                log.warning("Skipping corrupt JSONL record %s:%d: %s",
                            path, line_number, exc)
                continue
    return out


# --------------------------------------------------------------- api: projects

@app.get("/api/projects")
def list_projects():
    return {"projects": [
        {"id": name,
         "brief": ((STUDIO_ROOT / "projects" / name / "brief.md").read_text(encoding="utf-8")[:200]
                   if (STUDIO_ROOT / "projects" / name / "brief.md").exists() else "")}
        for name in reversed(ds.list_projects(STUDIO_ROOT))]}


class ProjectIn(BaseModel):
    name: str
    brief: str = ""


@app.post("/api/projects")
def create_project(body: ProjectIn):
    try:
        pp = ds.create_project(STUDIO_ROOT, body.name, body.brief)
    except FileExistsError:
        raise HTTPException(409, "project already exists")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": pp.name}


@app.get("/api/project/{pid}")
def get_project(pid: str):
    pp = resolve_project(pid)
    prompt_file = pp / "current_prompt.txt"
    return {
        "id": pp.name,
        "brief": (pp / "brief.md").read_text(encoding="utf-8") if (pp / "brief.md").exists() else "",
        "current_prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
        "chat_count": len(read_jsonl(pp / "chat.jsonl")),
    }


@app.get("/api/project/{pid}/chat")
def get_chat(pid: str, after: int = Query(0, ge=0)):
    pp = resolve_project(pid)
    lines = read_jsonl(pp / "chat.jsonl")
    return {"total": len(lines), "messages": lines[after:]}


@app.get("/api/project/{pid}/generations")
def get_generations(pid: str):
    pp = resolve_project(pid)
    gens_dir = pp / "generations"
    out = []
    if gens_dir.is_dir():
        for d in sorted(gens_dir.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            meta = {}
            if (d / "meta.json").exists():
                try:
                    meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    pass
            files = [f.name for f in d.iterdir() if f.is_file()]
            out.append({"gen": d.name, "files": files, "meta": meta})
    return {"generations": out}


@app.get("/api/project/{pid}/references")
def get_references(pid: str):
    pp = resolve_project(pid)
    refs = pp / "references"
    return {"references": sorted(
        f.name for f in refs.iterdir()
        if f.is_file() and not f.name.startswith(".")
    ) if refs.is_dir() else []}


def reference_target(directory: Path, filename: str) -> Path:
    if (not filename or "/" in filename or "\\" in filename or
            Path(filename).name != filename):
        raise HTTPException(400, f"invalid upload filename: {filename!r}")
    suffix = Path(filename).suffix.lower()
    if suffix not in REFERENCE_EXTENSIONS:
        raise HTTPException(415, f"unsupported reference type: {suffix or 'none'}")
    target = directory / filename
    index = 2
    while target.exists():
        target = directory / f"{Path(filename).stem}_{index}{suffix}"
        index += 1
    return target


@app.post("/api/project/{pid}/references", status_code=201)
async def upload_references(pid: str, files: list[UploadFile] = File(...)):
    pp = resolve_project(pid)
    if not files:
        raise HTTPException(400, "no files supplied")
    if len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, f"maximum {MAX_UPLOAD_FILES} files per upload")
    directory = pp / "references"
    directory.mkdir(exist_ok=True)
    saved = []
    temporary = []
    try:
        for upload in files:
            target = reference_target(directory, upload.filename or "")
            temp = directory / f".{uuid.uuid4().hex}.upload"
            temporary.append(temp)
            size = 0
            with temp.open("wb") as handle:
                while chunk := await upload.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_REFERENCE_BYTES:
                        raise HTTPException(
                            413, f"{upload.filename} exceeds 256MB limit")
                    handle.write(chunk)
            if size == 0:
                raise HTTPException(400, f"empty upload: {upload.filename}")
            temp.replace(target)
            temporary.remove(temp)
            saved.append({
                "name": target.name,
                "size": size,
                "url": f"/media/projects/{pp.name}/references/{target.name}",
            })
    except Exception:
        for item in saved:
            (directory / item["name"]).unlink(missing_ok=True)
        for temp in temporary:
            temp.unlink(missing_ok=True)
        raise
    finally:
        for upload in files:
            await upload.close()
    return {"references": saved}


class ChatIn(BaseModel):
    message: str


def execute_chat(pp: Path, message: str) -> str:
    """Run one message in the project's persistent Studio session."""
    ds.append_chat(STUDIO_ROOT, pp.name, "user", message)
    session_file = SESSIONS / f"{pp.name}.session"
    cmd = [HERMES, "-p", STUDIO_PROFILE]
    if session_file.exists():
        session_id = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            cmd += ["-r", session_id]
    cmd += ["chat", "-Q", "-t", "all", "-q", message]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode:
        raise RuntimeError(
            f"Studio agent failed ({result.returncode}): {result.stderr.strip()}")
    reply = result.stdout.strip()
    if not reply:
        raise RuntimeError("Studio agent returned an empty reply")
    match = SESSION_RE.search(result.stderr)
    if match:
        temp = session_file.with_suffix(".tmp")
        temp.write_text(match.group(1) + "\n", encoding="utf-8")
        temp.replace(session_file)
    ds.append_chat(STUDIO_ROOT, pp.name, "assistant", reply)
    return reply


def run_chat_job(job_id: str) -> None:
    job = read_job(job_id)
    update_job(job_id, status="running", started_at=utc_now())
    try:
        pp = ds.project_path(STUDIO_ROOT, job["project"])
        with project_lock(pp.name):
            reply = execute_chat(pp, job["message"])
        update_job(job_id, status="completed", reply=reply,
                   finished_at=utc_now(), error="")
    except Exception as exc:
        log.exception("Studio job %s failed", job_id)
        update_job(job_id, status="failed", error=str(exc),
                   finished_at=utc_now())


def create_chat_job(pp: Path, message: str) -> dict:
    with _jobs_guard:
        if any(job.get("status") in ACTIVE_JOB_STATES
               for job in project_jobs(pp.name, limit=100)):
            raise HTTPException(409, "project already has an active Studio job")
        job = {
            "id": uuid.uuid4().hex,
            "project": pp.name,
            "kind": "chat",
            "status": "queued",
            "message": message,
            "reply": "",
            "error": "",
            "created_at": utc_now(),
            "started_at": "",
            "finished_at": "",
        }
        write_job(job)
    threading.Thread(target=run_chat_job, args=(job["id"],),
                     daemon=True, name=f"studio-job-{job['id'][:8]}").start()
    return job


@app.post("/api/chat", status_code=202)
def chat(pid: str, body: ChatIn):
    pp = resolve_project(pid)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "empty message")
    return create_chat_job(pp, message)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    return read_job(job_id)


@app.get("/api/project/{pid}/jobs")
def get_project_jobs(pid: str, limit: int = Query(10, ge=1, le=100)):
    pp = resolve_project(pid)
    return {"jobs": project_jobs(pp.name, limit)}



# -------------------------------------------------------------------- static

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "static" / "index.html")


_media = StaticFiles(directory=str(STUDIO_ROOT), follow_symlink=False)
app.mount("/media", _media, name="media")

if COMFY_OUTPUT.is_dir():
    app.mount("/comfy", StaticFiles(directory=str(COMFY_OUTPUT)), name="comfy")
