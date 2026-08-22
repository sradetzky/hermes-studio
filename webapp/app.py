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
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
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
RUNTIME = REPO / ".runtime" / "sessions"
RUNTIME.mkdir(parents=True, exist_ok=True)
SESSION_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()
log = logging.getLogger(__name__)

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
    return {"references": sorted(f.name for f in refs.iterdir() if f.is_file()) if refs.is_dir() else []}


class ChatIn(BaseModel):
    message: str


@app.post("/api/chat")
def chat(pid: str, body: ChatIn):
    """Send a message to the project's persistent studio-agent session."""
    pp = resolve_project(pid)
    msg = body.message.strip()
    if not msg:
        raise HTTPException(400, "empty message")
    with project_lock(pp.name):
        ds.append_chat(STUDIO_ROOT, pp.name, "user", msg)
        session_file = RUNTIME / f"{pp.name}.session"
        cmd = [HERMES, "-p", STUDIO_PROFILE]
        if session_file.exists():
            session_id = session_file.read_text(encoding="utf-8").strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
                cmd += ["-r", session_id]
        cmd += ["chat", "-Q", "-t", "all", "-q", msg]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=600)
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "studio agent timed out")
        if result.returncode:
            log.error("Hermes chat failed (%d): %s", result.returncode,
                      result.stderr.strip())
            raise HTTPException(502, "studio agent failed")
        reply = result.stdout.strip()
        if not reply:
            raise HTTPException(502, "studio agent returned an empty reply")
        match = SESSION_RE.search(result.stderr)
        if match:
            tmp = session_file.with_suffix(".tmp")
            tmp.write_text(match.group(1) + "\n", encoding="utf-8")
            tmp.replace(session_file)
        ds.append_chat(STUDIO_ROOT, pp.name, "assistant", reply)
        return {"reply": reply}



# -------------------------------------------------------------------- static

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "static" / "index.html")


_media = StaticFiles(directory=str(STUDIO_ROOT), follow_symlink=False)
app.mount("/media", _media, name="media")

if COMFY_OUTPUT.is_dir():
    app.mount("/comfy", StaticFiles(directory=str(COMFY_OUTPUT)), name="comfy")
