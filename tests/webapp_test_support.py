import tempfile
import unittest
from pathlib import Path
from typing import Any

from studio_core.job_store import ActiveJobError, JobStore
from webapp.app import create_app
from webapp.config import Settings


class PassiveManager:
    def __init__(self, _settings, store):
        self.store = store

    def start(self):
        self.store.initialize()

    def stop(self):
        pass

    def submit_project_chat(self, project, message, profile=None):
        return self.store.create_project_chat_job(
            project, message, profile or "studio")

    def submit_chat(self, project, clip_id, message, profile=None):
        return self.store.create_chat_job(
            project, message, profile or "studio", clip_id=clip_id)


def create_job_in_process(database, barrier, results):
    store = JobStore(Path(database))
    barrier.wait()
    try:
        results.put(store.create_chat_job(
            "project", "message", clip_id="clip-001").id)
    except ActiveJobError:
        results.put("rejected")


def generation_settings_payload(**overrides):
    payload = {
        "mode": "t2va",
        "aspect": "16:9",
        "mp": 0.4,
        "width": None,
        "height": None,
        "seed": None,
        "steps": 20,
        "accel": False,
    }
    payload.update(overrides)
    return payload


class WebAppTestCase(unittest.TestCase):
    manager_factory: Any = PassiveManager

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            repo=Path(__file__).resolve().parent.parent,
            studio_root=root / "studio",
            comfy_output=root / "comfy-output",
            runtime_root=root / "runtime",
            job_timeout_seconds=5,
            max_reference_bytes=1024,
        )
        self.settings.comfy_output.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def app(self):
        return create_app(self.settings, self.manager_factory)

    def create_project(self, client, name="web-job"):
        response = client.post(
            "/api/projects", json={"name": name, "brief": "test"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]
