import json

from tests.webapp_test_support import WebAppTestCase
from webapp.studio_manager import StudioJobManager


class RoutePassiveManager:
    def __init__(self, settings, store):
        self.store = store
        self.manager = StudioJobManager(
            settings, store, cleanup_callback=lambda: None)

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

    def submit_generation(self, project, clip_id, prompt_sha256,
                          settings_updated_at,
                          use_previous_take_last_frame=False):
        return self.manager.submit_generation(
            project, clip_id, prompt_sha256, settings_updated_at,
            use_previous_take_last_frame)

    def submit_movie_export(self, project):
        return self.store.create_movie_export_job(project, json.dumps({
            "schema_version": 1,
            "action": "export-selected-takes",
            "sources": [{
                "clip_id": "clip-001",
                "clip_title": "Main clip",
                "generation": "001",
                "filename": "take.mp4",
                "size": 1,
                "sha256": "0" * 64,
                "probe": {
                    "duration_seconds": 1.0,
                    "video": {
                        "codec_name": "h264",
                        "width": 160,
                        "height": 96,
                        "pix_fmt": "yuv420p",
                        "r_frame_rate": "10/1",
                        "time_base": "1/10240",
                    },
                    "audio": None,
                },
            }],
            "assembly": {
                "mode": "stream-copy",
                "hard_cuts": True,
                "target": {
                    "width": 160,
                    "height": 96,
                    "fps": "10/1",
                    "sample_rate": 48000,
                    "channels": 2,
                },
            },
            "output": {
                "id": "movie-001",
                "filename": "movie.mp4",
                "provenance": "provenance.json",
            },
        }))


class RouteWebAppTestCase(WebAppTestCase):
    manager_factory = RoutePassiveManager
