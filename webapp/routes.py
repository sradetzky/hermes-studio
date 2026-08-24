from __future__ import annotations

from pathlib import Path
from typing import NoReturn

from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field

from scripts import design_studio as ds
from webapp.clip_store import (
    ClipNotFoundError,
    ClipStore,
    ClipStoreError,
    TakeNotFoundError,
)
from webapp.comfy_queue import ComfyQueueClient
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
from webapp.safe_response import DescriptorFileResponse
from webapp.studio_manager import StudioJobManager


router = APIRouter()
MEDIA_AREAS = {"references", "final"}


class ProjectIn(BaseModel):
    name: str
    brief: str = ""


class ChatIn(BaseModel):
    message: str
    profile: str | None = None


class ClipIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class ClipUpdateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    enabled: bool | None = None


class ClipOrderIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    clip_ids: list[str]


class SelectedTakeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    generation: str | None
    filename: str | None = None


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


class GenerateIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    settings_updated_at: str = Field(min_length=1, max_length=64)


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


def _clips(request: Request) -> ClipStore:
    return request.app.state.clip_store


def _comfy_queue(request: Request) -> ComfyQueueClient:
    return request.app.state.comfy_queue


def _raise_media_review_error(exc: MediaReviewError) -> NoReturn:
    if isinstance(exc, MediaNotFoundError):
        raise HTTPException(404, str(exc))
    if isinstance(exc, UnsupportedMediaError):
        raise HTTPException(415, str(exc))
    raise HTTPException(400, str(exc))


def _raise_clip_store_error(exc: ClipStoreError) -> NoReturn:
    if isinstance(exc, (ClipNotFoundError, TakeNotFoundError)):
        raise HTTPException(404, str(exc))
    raise HTTPException(400, str(exc))


def resolve_project(request: Request, project_id: str) -> Path:
    try:
        return ds.project_path(_settings(request).studio_root, project_id)
    except FileNotFoundError:
        raise HTTPException(404, f"project not found: {project_id}")
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def resolve_clip(request: Request, project: Path, clip_id: str) -> Path:
    try:
        return _clips(request).resolve_clip(project, clip_id)
    except ClipNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ClipStoreError as exc:
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


@router.get("/api/comfyui/queue")
def get_comfy_queue(request: Request):
    return _comfy_queue(request).snapshot()


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
    try:
        manifest = _clips(request).describe(project)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)
    return {
        "id": project.name,
        "title": manifest["title"],
        "brief": ds.read_project_text(project, "brief.md"),
        "clips": manifest["clips"],
    }


@router.get("/api/project/{project_id}/clips")
def get_clips(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    try:
        return _clips(request).describe(project)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)


@router.post("/api/project/{project_id}/clips", status_code=201)
def create_clip(request: Request, project_id: str, body: ClipIn):
    project = resolve_project(request, project_id)
    try:
        return {"clip": _clips(request).create_clip(project, body.title)}
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)


@router.put("/api/project/{project_id}/clips/order")
def reorder_clips(request: Request, project_id: str, body: ClipOrderIn):
    project = resolve_project(request, project_id)
    try:
        return _clips(request).reorder(project, body.clip_ids)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)


@router.get("/api/project/{project_id}/clips/{clip_id}")
def get_clip(request: Request, project_id: str, clip_id: str):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        manifest = _clips(request).describe(project)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)
    entry = next(item for item in manifest["clips"] if item["id"] == clip_id)
    try:
        generation_settings = _generation_settings(request).describe(project, clip)
    except GenerationSettingsError as exc:
        raise HTTPException(400, str(exc))
    return {
        **entry,
        "current_prompt": ds.read_project_text(clip, "current_prompt.txt"),
        "generation_settings": generation_settings,
    }


@router.patch("/api/project/{project_id}/clips/{clip_id}")
def update_clip(request: Request, project_id: str, clip_id: str,
                body: ClipUpdateIn):
    project = resolve_project(request, project_id)
    try:
        return {"clip": _clips(request).update_clip(
            project, clip_id, title=body.title, enabled=body.enabled)}
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)


@router.put("/api/project/{project_id}/clips/{clip_id}/selected-take")
def select_clip_take(request: Request, project_id: str, clip_id: str,
                     body: SelectedTakeIn):
    project = resolve_project(request, project_id)
    try:
        return {"clip": _clips(request).select_take(
            project, clip_id, body.generation, body.filename)}
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)


@router.get("/api/project/{project_id}/clips/{clip_id}/generation-settings")
def get_generation_settings(request: Request, project_id: str, clip_id: str):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        return _generation_settings(request).describe(
            project, clip, include_options=True)
    except GenerationSettingsError as exc:
        raise HTTPException(400, str(exc))


@router.put("/api/project/{project_id}/clips/{clip_id}/generation-settings")
def put_generation_settings(request: Request, project_id: str, clip_id: str,
                            body: GenerationSettingsIn):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        return _generation_settings(request).save(
            project, clip, body.model_dump())
    except GenerationSettingsError as exc:
        raise HTTPException(400, str(exc))


@router.post(
    "/api/project/{project_id}/clips/{clip_id}/generate", status_code=202)
def generate_current_prompt(request: Request, project_id: str, clip_id: str,
                            body: GenerateIn):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        manifest = _clips(request).describe(project)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)
    entry = next(item for item in manifest["clips"] if item["id"] == clip_id)
    if not entry["enabled"]:
        raise HTTPException(409, "enable this clip before generating")
    try:
        _generation_settings(request).validate_generation_request(
            project, clip, body.prompt_sha256, body.settings_updated_at)
    except GenerationSettingsError as exc:
        raise HTTPException(409, str(exc))
    try:
        job = _manager(request).submit_generation(
            project.name,
            clip_id,
            body.prompt_sha256,
            body.settings_updated_at,
        )
    except ActiveJobError as exc:
        raise HTTPException(409, str(exc))
    return job.to_dict()


@router.get("/api/project/{project_id}/chat")
def get_chat(request: Request, project_id: str,
             after: int = Query(0, ge=0)):
    project = resolve_project(request, project_id)
    store = _store(request)
    store.import_chat_if_empty(project.name, project / "chat.jsonl")
    cursor, events = store.chat_events(project.name, after)
    return {"cursor": cursor, "messages": [event.to_dict() for event in events]}


@router.get("/api/project/{project_id}/clips/{clip_id}/generations")
def get_generations(request: Request, project_id: str, clip_id: str):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        return {"generations": _media_reviews(request).list_generations(
            project, clip)}
    except MediaReviewError as exc:
        _raise_media_review_error(exc)


@router.get(
    "/api/project/{project_id}/clips/{clip_id}/generations/{generation_id}")
def get_generation(request: Request, project_id: str, clip_id: str,
                   generation_id: str):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        return _media_reviews(request).describe_generation(
            project, clip, generation_id, include_prompt=True)
    except MediaReviewError as exc:
        _raise_media_review_error(exc)


@router.delete(
    "/api/project/{project_id}/clips/{clip_id}/generations/{generation_id}")
def delete_generation(request: Request, project_id: str, clip_id: str,
                      generation_id: str):
    project = resolve_project(request, project_id)
    if any(job.project == project.name for job in _store(request).active_jobs()):
        raise HTTPException(
            409, "cannot delete a take while this project has an active job")
    try:
        clip = _clips(request).delete_take(project, clip_id, generation_id)
    except ClipStoreError as exc:
        _raise_clip_store_error(exc)
    return {"ok": True, "deleted": generation_id, "clip": clip}


@router.post(
    "/api/project/{project_id}/clips/{clip_id}/generations/"
    "{generation_id}/promote")
def promote_generation_media(request: Request, project_id: str, clip_id: str,
                             generation_id: str, body: MediaActionIn):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        saved = _media_reviews(request).publish(
            project, clip, generation_id, body.filename, "promote")
    except MediaReviewError as exc:
        _raise_media_review_error(exc)
    return {"ok": True, "result": saved.to_dict()}


@router.post(
    "/api/project/{project_id}/clips/{clip_id}/generations/"
    "{generation_id}/use-as-reference")
def use_generation_as_reference(request: Request, project_id: str, clip_id: str,
                                generation_id: str, body: MediaActionIn):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        saved = _media_reviews(request).publish(
            project, clip, generation_id, body.filename, "reference")
    except MediaReviewError as exc:
        _raise_media_review_error(exc)
    return {"ok": True, "result": saved.to_dict()}


@router.get("/api/project/{project_id}/references")
def get_references(request: Request, project_id: str):
    project = resolve_project(request, project_id)
    try:
        return {"references": _references(request).list_references(project)}
    except ReferenceStoreError as exc:
        raise HTTPException(400, str(exc))


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


@router.post(
    "/api/project/{project_id}/clips/{clip_id}/chat", status_code=202)
def chat(request: Request, project_id: str, clip_id: str, body: ChatIn):
    project = resolve_project(request, project_id)
    resolve_clip(request, project, clip_id)
    message = body.message.strip()
    if not message:
        raise HTTPException(400, "empty message")
    profile = body.profile or _settings(request).studio_profile
    if profile not in _settings(request).profiles:
        raise HTTPException(400, f"unknown Studio profile: {profile}")
    try:
        job = _manager(request).submit_chat(
            project.name, clip_id, message, profile)
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


@router.get(
    "/media/projects/{project_id}/clips/{clip_id}/generations/"
    "{generation_id}/{filename}")
def clip_generation_media(request: Request, project_id: str, clip_id: str,
                          generation_id: str, filename: str):
    project = resolve_project(request, project_id)
    clip = resolve_clip(request, project, clip_id)
    try:
        with _media_reviews(request).open_media(
                clip, generation_id, filename) as opened:
            return DescriptorFileResponse(opened)
    except MediaReviewError as exc:
        _raise_media_review_error(exc)


@router.get("/media/projects/{project_id}/{area}/{relative_path:path}")
def project_media(request: Request, project_id: str, area: str,
                  relative_path: str):
    if area not in MEDIA_AREAS:
        raise HTTPException(404, "media area not found")
    project = resolve_project(request, project_id)
    try:
        with _references(request).open_media(
                project, area, relative_path) as opened:
            return DescriptorFileResponse(opened)
    except FileNotFoundError:
        raise HTTPException(404, "media not found")
    except ReferenceStoreError as exc:
        if str(exc) == "invalid media path":
            raise HTTPException(400, str(exc))
        raise HTTPException(404, "media not found")
