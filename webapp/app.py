"""Hermes Studio FastAPI application factory.

Run: .venv/bin/uvicorn webapp.app:app --host 127.0.0.1 --port 8788
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Protocol

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from scripts import design_studio as ds
from webapp.config import Settings
from webapp.job_store import JobStore
from webapp.models import Job
from webapp.reference_store import ReferenceStore
from webapp.routes import router
from webapp.studio_manager import StudioJobManager


class JobManager(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def submit_chat(self, project: str, message: str) -> Job: ...


ManagerFactory = Callable[[Settings, JobStore], JobManager]


def create_app(settings: Settings | None = None,
               manager_factory: ManagerFactory = StudioJobManager) -> FastAPI:
    settings = settings or Settings.from_environment()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        ds.studio_root(str(settings.studio_root))
        store = JobStore(settings.database_path)
        references = ReferenceStore(settings)
        manager = manager_factory(settings, store)
        application.state.job_store = store
        application.state.reference_store = references
        application.state.job_manager = manager
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    application = FastAPI(title="Hermes Studio", lifespan=lifespan)
    application.state.settings = settings
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