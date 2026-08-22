"""Studio web UI — thin read-mostly window onto the studio-root filesystem.

Run: .venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import design_studio as ds  # noqa: E402

WEB = REPO / "webapp"
HERMES = "hermes"
STUDIO_PROFILE = "studio"
COMFY_OUTPUT = Path.home() / "ComfyUI" / "output"

app = FastAPI(title="MiniMax Design Studio")


# ------------------------------------------------------------------ helpers

def root() -> Path:
    return ds.studio_root(os.environ.get("DESIGN_STUDIO_ROOT"))


def safe_project(pid: str) -> Path:
    """Resolve project id and refuse anything escaping the projects dir."""
    try:
        pp = ds.project_path(root(), pid)
    except FileNotFoundError:
        raise HTTPException(404, f"project not found: {pid}")
    projects = (root() / "projects").resolve()
    if not str(pp).startswith(str(projects)):
        raise HTTPException(400, "bad project id")
    return pp


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


# --------------------------------------------------------------- api: projects

@app.get("/api/projects")
def list_projects():
    return {"projects": [
        {"id": name,
         "brief": ((root() / "projects" / name / "brief.md").read_text(encoding="utf-8")[:200]
                   if (root() / "projects" / name / "brief.md").exists() else "")}
        for name in ds.list_projects(root())]}


class ProjectIn(BaseModel):
    name: str
    brief: str = ""


@app.post("/api/projects")
def create_project(body: ProjectIn):
    try:
        pp = ds.create_project(root(), body.name, body.brief)
    except FileExistsError:
        raise HTTPException(409, "project already exists")
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "id": pp.name}


@app.get("/api/project/{pid}")
def get_project(pid: str):
    pp = safe_project(pid)
    prompt_file = pp / "current_prompt.txt"
    return {
        "id": pp.name,
        "brief": (pp / "brief.md").read_text(encoding="utf-8") if (pp / "brief.md").exists() else "",
        "current_prompt": prompt_file.read_text(encoding="utf-8") if prompt_file.exists() else "",
        "chat_count": len(read_jsonl(pp / "chat.jsonl")),
    }


@app.get("/api/project/{pid}/chat")
def get_chat(pid: str, after: int = 0):
    pp = safe_project(pid)
    lines = read_jsonl(pp / "chat.jsonl")
    return {"total": len(lines), "messages": lines[after:]}


@app.get("/api/project/{pid}/generations")
def get_generations(pid: str):
    pp = safe_project(pid)
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
    pp = safe_project(pid)
    refs = pp / "references"
    return {"references": sorted(f.name for f in refs.iterdir() if f.is_file()) if refs.is_dir() else []}


class ChatIn(BaseModel):
    message: str


@app.post("/api/chat")
def chat(pid: str, body: ChatIn):
    """Send a message to the studio profile; returns full reply (v1, no stream)."""
    pp = safe_project(pid)
    msg = body.message.strip()
    if not msg:
        raise HTTPException(400, "empty message")
    ds.append_chat(root(), pp.name, "user", msg)
    try:
        r = subprocess.run([HERMES, "-p", STUDIO_PROFILE, "chat", "-q", msg, "--quiet"],
                           capture_output=True, text=True, timeout=600)
        reply = r.stdout.strip()
        # strip banner noise lines (warnings etc.) that hermes prints to stdout
        keep = [ln for ln in reply.splitlines()
                if not ln.startswith(("Warning:", "⚠", "session_id:"))]
        reply = "\n".join(keep).strip() or "(no reply)"
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "studio agent timed out")
    ds.append_chat(root(), pp.name, "assistant", reply)
    return {"reply": reply}



# -------------------------------------------------------------------- static

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(WEB / "static" / "index.html")


_media = StaticFiles(directory=str(root()), follow_symlink=False)
app.mount("/media", _media, name="media")

if COMFY_OUTPUT.is_dir():
    app.mount("/comfy", StaticFiles(directory=str(COMFY_OUTPUT)), name="comfy")
