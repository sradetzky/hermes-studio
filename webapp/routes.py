from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict

from scripts import design_studio as ds
from webapp.config import Settings
from webapp.generation_settings_store import (
    GenerationSettingsError,
    GenerationSettingsStore,
)
from webapp.job_store import ActiveJobError, JobNotFoundError, JobStore
from webapp.media_review_store import (
    MediaNotFoundError,
    MediaReviewError,
    MediaReviewStore,
    UnsupportedMediaError,
)
from webapp.reference_store import (
    ReferenceStore,
    ReferenceStoreError,
    ReferenceTooLargeError,
    UnsupportedReferenceError,
)
from webapp.studio_manager import StudioJobManager


router = APIRouter()
MEDIA_AREAS = {"references", "generations", "final"}


class ProjectIn(BaseModel):
    name: str
    brief: str = ""


class ChatIn(BaseModel):
    message: str
    profile: str | None = None


class MediaActionIn(BaseModel):
    filename: str


class GenerationSettingsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str
    aspect: str
    mp: float
    width: int | None
    height: int | None
    seed: str | int | None
    steps: int
    accel: bool


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> JobStore:
    return request.app.state.job_store


def _manager(request: Request) -> StudioJobManager:
    return request.app.state.job_manager


def _references(request: Request) -> ReferenceStore:
    return request.app.state.reference_store


def _media_reviews(request: Request) -> MediaReviewStore:
    return request.app.state.media_review_store


def _generation_settings(request: Request) -> GenerationSettingsStore:
    return request.app.state.generation_settings_store


def _raise_media_review_error(exc: MediaReviewError) -> NoReturn:
    if isinstance(exc, MediaNotFoundError):
        raise HTTPException(404, str(exc))
    if isinstance(exc, UnsupportedMediaError):
        raise HTTPException(415, str(exc))
    raise HTTPException(400, str(exc))


def resolve_project(request: Request, project_id: str) -> Path:
    try:
        return ds.project_path(_settings(request).studio_root, project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"project not found: {project_id}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/projects")
def list_projects(request: Request):
    root = _settings(request).studio_root
    return {"projects": [
        {
            "id": name,
            "brief": ds.read_project_text(
                root / "projects" / name, "brief.md", limit=200),
        }
        for name in reversed(ds.list_projects(root))
    ]}


@router.get("/api/profiles")
def list_profiles(request: Request):
    roles = {
        "studio": "Orchestrator",
        "studio-storyboarder": "Storyboarder",
        "studio-prompt-engineer": "Prompt engineer",
        "studio-reviewer": "Reviewer",
        "studio-illustrator": "Illustrator",
    }
    return {"profiles": [
        {"id": profile, "label": roles.get(profile, profile)}
        for profile in _settings(request).profiles
    ]}


@router.post("/api/projects")
def create_project(request: Request, body: ProjectIn):
    try:
        project = ds.create_project(
            _settings(request).studio_root, body.name, body.brief)
    except FileExistsError:
        raise HTTPException(409, "project already exists")
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"ok": True, "id": project.name}


@router.get("/api/project/{project_id}")
def get_project(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    store = _store(request)
    store.import_chat_if_empty(project.name, project / "chat.jsonl")
    chat_count, _ = store.chat_events(project.name)
    return {
        "id": project.name,
        "brief": ds.read_project_text(project, "brief.md"),
        "current_prompt": ds.read_project_text(project, "current_prompt.txt"),
        "chat_count": chat_count,
        "generation_settings": _generation_settings(request).describe(project),
    }


@router.get("/api/project/{project_id}/generation-settings")
def get_generation_settings(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    return _generation_settings(request).describe(project, include_options=True)


@router.put("/api/project/{project_id}/generation-settings")
def put_generation_settings(request: Request, project_id: str,
                            body: GenerationSettingsIn):
    project = resolve_project(request, project_id)
    try:
        return _generation_settings(request).save(
            project, body.model_dump())
    except GenerationSettingsError as exc:
        raise HTTPException(400, str(exc))


@router.get("/api/project/{project_id}/chat")
def get_chat(request: Request, project_id: str,
             after: int = Query(0, ge=0)):
    project = resolve_project(request, project_id)
    store = _store(request)
    store.import_chat_if_empty(project.name, project / "chat.jsonl")
    total, events = store.chat_events(project.name, after)
    return {"total": total, "messages": [event.to_dict() for event in events]}


@router.get("/api/project/{project_id}/generations")
def get_generations(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    reviews = _media_reviews(request)
    generations = []
    directory = project / "generations"
    if directory.is_dir():
        for generation in sorted(directory.iterdir(), reverse=True):
            if not generation.is_dir() or generation.is_symlink():
                continue
            try:
                generations.append(reviews.describe_generation(
                    project, generation.name, include_prompt=False))
            except MediaReviewError:
                continue
    return {"generations": generations}


@router.get("/api/project/{project_id}/generations/{generation_id}")
def get_generation(request: Request, project_id: str, generation_id: str):
    project = resolve_project(request, project_id)
    try:
        return _media_reviews(request).describe_generation(
            project, generation_id, include_prompt=True)
    except MediaReviewError as exc:
        _raise_media_review_error(exc)


@router.post(
    "/api/project/{project_id}/generations/{generation_id}/promote")
def promote_generation_media(request: Request, project_id: str,
                             generation_id: str, body: MediaActionIn):
    project = resolve_project(request, project_id)
    try:
        saved = _media_reviews(request).publish(
            project, generation_id, body.filename, "promote")
    except MediaReviewError as exc:
        _raise_media_review_error(exc)
    return {"ok": True, "result": saved.to_dict()}


@router.post(
    "/api/project/{project_id}/generations/{generation_id}/use-as-reference")
def use_generation_as_reference(request: Request, project_id: str,
                                generation_id: str, body: MediaActionIn):
    project = resolve_project(request, project_id)
    try:
        saved = _media_reviews(request).publish(
            project, generation_id, body.filename, "reference")
    except MediaReviewError as exc:
        _raise_media_review_error(exc)
    return {"ok": True, "result": saved.to_dict()}


@router.get("/api/project/{project_id}/references")
def get_references(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    directory = project / "references"
    if directory.is_symlink():
        return {"references": []}
    references = sorted(
        item.name for item in directory.iterdir()
        if item.is_file() and not item.is_symlink()
        and not item.name.startswith(".")
    ) if directory.is_dir() else []
    return {"references": references}


@router.post("/api/project/{project_id}/references", status_code=201)
def upload_references(request: Request, project_id: str,
                      files: list[UploadFile] = File(...)):
    project = resolve_project(request, project_id)
    try:
        saved = _references(request).save_batch(project, files)
    except ReferenceTooLargeError as exc:
        raise HTTPException(413, str(exc))
    except UnsupportedReferenceError as exc:
        raise HTTPException(415, str(exc))
    except ReferenceStoreError as exc:
        raise HTTPException(400, str(exc))
    return {"references": [item.to_dict() for item in saved]}


@router.post("/api/chat", status_code=202)
def chat(request: Request, body: ChatIn,
         project_id: str = Query(alias="pid")):
    project = resolve_project(request, project_id)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "empty message")
    profile = body.profile or _settings(request).studio_profile
    if profile not in _settings(request).profiles:
        raise HTTPException(400, f"unknown Studio profile: {profile}")
    try:
        job = _manager(request).submit_chat(
            project.name, message, profile)
    except ActiveJobError as exc:
        raise HTTPException(409, str(exc))
    return job.to_dict()


@router.get("/api/jobs/{job_id}")
def get_job(request: Request, job_id: str):
    try:
        return _store(request).get_job(job_id).to_dict()
    except JobNotFoundError:
        raise HTTPException(404, "job not found")


@router.get("/api/project/{project_id}/jobs")
def get_project_jobs(request: Request, project_id: str,
                     limit: int = Query(10, ge=1, le=100)):
    project = resolve_project(request, project_id)
    jobs = _store(request).list_jobs(project.name, limit)
    return {"jobs": [job.to_dict() for job in jobs]}


@router.get("/api/project/{project_id}/events")
def get_project_events(request: Request, project_id: str,
                       after: int = Query(0, ge=0)):
    project = resolve_project(request, project_id)
    cursor, events = _store(request).job_events(project.name, after)
    return {
        "cursor": cursor,
        "events": [event.to_dict() for event in events],
    }


@router.get("/media/projects/{project_id}/{area}/{relative_path:path}")
def project_media(request: Request, project_id: str, area: str,
                  relative_path: str):
    if area not in MEDIA_AREAS:
        raise HTTPException(404, "media area not found")
    project = resolve_project(request, project_id)
    area_path = project / area
    if area_path.is_symlink():
        raise HTTPException(404, "media area not found")
    base = area_path.resolve()
    if base.parent != project.resolve():
        raise HTTPException(404, "media area not found")
    relative = Path(relative_path)
    if (not relative_path or relative.is_absolute() or
            any(part.startswith(".") for part in relative.parts)):
        raise HTTPException(400, "invalid media path")
    target = (base / relative).resolve()
    if not target.is_relative_to(base) or not target.is_file():
        raise HTTPException(404, "media not found")
    return FileResponse(target)
