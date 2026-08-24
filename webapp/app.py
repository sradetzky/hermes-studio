"""Hermes Studio FastAPI application factory.

Run: .venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from scripts import design_studio as ds
from webapp.clip_store import ClipStore
from webapp.comfy_queue import ComfyQueueClient
from webapp.config import Settings
from webapp.generation_settings_store import GenerationSettingsStore
from webapp.job_store import JobStore
from webapp.media_review_store import MediaReviewStore
from webapp.models import Job
from webapp.movie_store import MovieStore
from webapp.reference_store import ReferenceStore
from webapp.routes import router
from webapp.studio_manager import StudioJobManager


class JobManager(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def submit_project_chat(self, project: str, message: str,
                            profile: str | None = None) -> Job: ...
    def submit_chat(self, project: str, clip_id: str, message: str,
                    profile: str | None = None) -> Job: ...


ManagerFactory = Callable[[Settings, JobStore], JobManager]


def create_app(settings: Settings | None = None,
               manager_factory: ManagerFactory = StudioJobManager) -> FastAPI:
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ds.studio_root(str(settings.studio_root))
        store = JobStore(settings.database_path)
        references = ReferenceStore(settings)
        media_reviews = MediaReviewStore()
        generation_settings = GenerationSettingsStore(settings)
        clips = ClipStore()
        movies = MovieStore(clips, media_reviews)
        comfy_queue = ComfyQueueClient(settings.comfy_url)
        manager = manager_factory(settings, store)
        application.state.job_store = store
        application.state.reference_store = references
        application.state.media_review_store = media_reviews
        application.state.generation_settings_store = generation_settings
        application.state.clip_store = clips
        application.state.movie_store = movies
        application.state.comfy_queue = comfy_queue
        application.state.job_manager = manager
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    application = FastAPI(title="Hermes Studio", lifespan=lifespan)
    application.state.settings = settings
    application.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=list(settings.trusted_hosts),
    )

    @application.middleware("http")
    async def protect_local_writes(request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            origin_host = urlsplit(origin).hostname if origin else None
            if origin_host and origin_host.lower() not in settings.trusted_hosts:
                return JSONResponse(
                    {"detail": "cross-origin writes are not allowed"},
                    status_code=403,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    application.include_router(router)
    application.mount(
        "/static",
        StaticFiles(directory=settings.web_root / "static"),
        name="static",
    )

    @application.get("/", include_in_schema=False)
    def index():
        return FileResponse(settings.web_root / "static" / "index.html")

    return application


app = create_app()