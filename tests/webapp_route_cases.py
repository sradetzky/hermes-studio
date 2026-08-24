import json
import multiprocessing
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event, Thread
from unittest.mock import patch

from fastapi.testclient import TestClient

from scripts import design_studio as ds
from webapp.app import create_app
from webapp.config import Settings
from webapp.hermes_events import HermesSessionEventBridge
from webapp.job_store import ActiveJobError, JobStore, JobStoreError
from webapp.models import JobStatus
from webapp.runtime_schema import CURRENT_SCHEMA_VERSION, LEGACY_CLIP_ERROR
from webapp import safe_files
from webapp.studio_manager import StudioJobManager, process_start_time


def _create_job_in_process(database, barrier, results):
    store = JobStore(Path(database))
    barrier.wait()
    try:
        results.put(store.create_chat_job(
            "project", "message", clip_id="clip-001").id)
    except ActiveJobError:
        results.put("rejected")
def _generation_settings_payload(**overrides):
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
class PassiveManager:
    def __init__(self, _settings, store):
        self.store = store

    def start(self):
        self.store.initialize()

    def stop(self):
        pass

    def submit_chat(self, project, clip_id, message, profile=None):
        return self.store.create_chat_job(
            project, message, profile or "studio", clip_id=clip_id)
class WebAppTestCase(unittest.TestCase):
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
        return create_app(self.settings, PassiveManager)

    def create_project(self, client, name="web-job"):
        response = client.post(
            "/api/projects", json={"name": name, "brief": "test"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

class LauncherScriptTests(unittest.TestCase):
    def test_profile_sync_allows_missing_optional_grok_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            hermes_home = Path(directory)
            for profile in (
                "studio", "studio-storyboarder", "studio-prompt-engineer",
                "studio-reviewer", "studio-illustrator",
            ):
                config = hermes_home / "profiles" / profile / "config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text("model: {}\n")
            script = (
                Path(__file__).resolve().parent.parent /
                "scripts" / "sync-profiles.sh")
            result = subprocess.run(
                [script], capture_output=True, text=True, check=False,
                env={**os.environ, "HERMES_HOME": str(hermes_home)},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("skipped optional studio-grok", result.stdout)

    def test_run_rejects_uvicorn_overrides(self):
        script = Path(__file__).resolve().parent.parent / "webapp" / "run.sh"
        result = subprocess.run(
            [script, "--host", "0.0.0.0"],
            capture_output=True, text=True, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage", result.stderr)

    def test_stop_refuses_active_jobs_without_force(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            webapp = root / "webapp"
            runtime = root / ".runtime"
            python_dir = root / ".venv" / "bin"
            webapp.mkdir()
            runtime.mkdir()
            python_dir.mkdir(parents=True)
            shutil.copy2(
                Path(__file__).resolve().parent.parent / "webapp" / "stop.sh",
                webapp / "stop.sh",
            )
            (webapp / "stop.sh").chmod(0o755)
            (python_dir / "python").symlink_to(sys.executable)
            database = runtime / "studio.db"
            with closing(sqlite3.connect(database)) as connection, connection:
                connection.execute(
                    "CREATE TABLE jobs (id TEXT, project TEXT, profile TEXT, "
                    "status TEXT, created_at TEXT)")
                connection.execute(
                    "INSERT INTO jobs VALUES "
                    "('job-1', 'project-1', 'studio', 'running', 'now')")

            process = subprocess.Popen([
                "bash", "-c",
                f"exec -a {webapp / 'run.sh'} sleep 30",
            ])
            try:
                (runtime / "webapp.pid").write_text(str(process.pid))
                refused = subprocess.run(
                    [webapp / "stop.sh"], capture_output=True, text=True,
                    check=False,
                )
                self.assertEqual(refused.returncode, 1)
                self.assertIn("jobs are active", refused.stderr)
                self.assertIsNone(process.poll())

                with closing(sqlite3.connect(database)) as connection, connection:
                    connection.execute("DELETE FROM jobs")
                reaper = Thread(target=process.wait)
                reaper.start()
                stopped = subprocess.run(
                    [webapp / "stop.sh"], capture_output=True, text=True,
                    timeout=10, check=False,
                    env={**os.environ, "HERMES_STUDIO_STOP_TIMEOUT_SECONDS": "5"},
                )
                reaper.join(timeout=5)
                self.assertEqual(stopped.returncode, 0, stopped.stderr)
                self.assertIsNotNone(process.poll())
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=5)

class AppFactoryTests(WebAppTestCase):
    def test_rejects_untrusted_hosts_and_cross_origin_writes(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.get(
                "/api/projects", headers={"Host": "attacker.example"}
            ).status_code, 400)
            self.assertEqual(client.post(
                "/api/projects",
                headers={"Origin": "https://attacker.example"},
                json={"name": "blocked", "brief": ""},
            ).status_code, 403)

    def test_default_job_timeout_covers_long_h3_runs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STUDIO_JOB_TIMEOUT_SECONDS", None)
            settings = Settings.from_environment()
        self.assertEqual(settings.job_timeout_seconds, 10_800)

    def test_comfy_queue_route_sanitizes_workflows_and_preserves_order(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "queue_running": [[
                        7, "running-id", {"secret": "large workflow"}, {}, ["9"],
                    ]],
                    "queue_pending": [
                        [8, "next-id", {"secret": "next workflow"}, {}, ["9"]],
                        [9, "later-id", {}, {}, ["9"]],
                    ],
                }).encode()

        with patch("webapp.comfy_queue.urlopen", return_value=Response()) as fetch:
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": True,
            "running": [{"prompt_id": "running-id", "position": 0}],
            "pending": [
                {"prompt_id": "next-id", "position": 1},
                {"prompt_id": "later-id", "position": 2},
            ],
        })
        request = fetch.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8188/queue")

    def test_comfy_queue_route_reports_unavailable_without_failing_refresh(self):
        with patch("webapp.comfy_queue.urlopen", side_effect=OSError("offline")):
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": False,
            "running": [],
            "pending": [],
            "error": "ComfyUI queue unavailable",
        })

    def test_app_creation_has_no_runtime_side_effects(self):
        create_app(self.settings, PassiveManager)
        self.assertFalse(self.settings.runtime_root.exists())
        with TestClient(self.app()):
            self.assertTrue(self.settings.database_path.is_file())

    def test_chat_route_returns_202_and_rejects_second_active_job(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            first = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "hello"})
            second = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "again"})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["status"], "queued")
            self.assertEqual(first.json()["profile"], "studio")
            self.assertEqual(first.json()["clip_id"], "clip-001")
            self.assertEqual(second.status_code, 409)
            chat = client.get(f"/api/project/{project}/chat").json()
            self.assertEqual(
                [(message["role"], message["content"]) for message in chat["messages"]],
                [("user", "hello")],
            )
            self.assertEqual(chat["cursor"], chat["messages"][-1]["id"])
            unchanged = client.get(
                f"/api/project/{project}/chat?after={chat['cursor']}"
            ).json()
            self.assertEqual(unchanged, {
                "cursor": chat["cursor"], "messages": []})
            activity = client.get(
                f"/api/project/{project}/events").json()
            self.assertEqual(activity["events"][0]["event_type"], "job.queued")
            self.assertEqual(activity["events"][0]["profile"], "studio")
            listing = client.get(f"/api/project/{project}/jobs").json()
            self.assertEqual([job["id"] for job in listing["jobs"]],
                             [first.json()["id"]])

    def test_chat_route_requires_an_exact_clip(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "missing-chat-clip")
            response = client.post(
                f"/api/project/{project}/clips/clip-999/chat",
                json={"message": "hello"},
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(
                client.get(f"/api/project/{project}/jobs").json()["jobs"], [])

    def test_profile_listing_and_specialist_dispatch_validation(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "profiles")
            profiles = client.get("/api/profiles").json()["profiles"]
            self.assertIn(
                "studio-storyboarder", [profile["id"] for profile in profiles])
            accepted = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "Plan the shots", "profile": "studio-storyboarder"},
            )
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["profile"], "studio-storyboarder")

            other = self.create_project(client, "unknown-profile")
            rejected = client.post(
                f"/api/project/{other}/clips/clip-001/chat",
                json={"message": "hello", "profile": "not-a-profile"},
            )
            self.assertEqual(rejected.status_code, 400)

    def test_chat_defaults_to_configured_studio_profile(self):
        settings = replace(self.settings, studio_profile="custom-studio")
        with TestClient(create_app(settings, PassiveManager)) as client:
            project = self.create_project(client, "custom-profile")
            accepted = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "hello"})
            self.assertEqual(accepted.status_code, 202)
            self.assertEqual(accepted.json()["profile"], "custom-studio")

    def test_generation_settings_manifest_and_prompt_readiness(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "generation-settings")
            root = self.settings.studio_root / "projects" / project
            clip = root / "clips" / "clip-001"
            (clip / "current_prompt.txt").write_text(
                "A 5-second compiled H3 prompt\n")

            initial = client.get(
                f"/api/project/{project}/clips/clip-001/generation-settings"
            ).json()
            self.assertFalse(initial["exists"])
            self.assertEqual(initial["readiness"]["status"], "not-configured")
            self.assertEqual(initial["options"]["modes"],
                             ["fl2va", "i2va", "r2v", "t2va"])
            self.assertEqual(
                sorted(initial["options"]), ["aspects", "max_seed", "modes"])
            self.assertEqual(initial["options"]["max_seed"], "9007199254740991")

            large_seed = "9007199254740991"
            saved = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(seed=large_seed),
            )
            self.assertEqual(saved.status_code, 200)
            body = saved.json()
            self.assertTrue(body["exists"])
            self.assertTrue(body["readiness"]["ready"])
            self.assertEqual(body["settings"]["seed"], large_seed)
            self.assertEqual(
                body["readiness"]["resolution"], {
                    "mode": "mp", "width": 832, "height": 480,
                    "megapixels": 0.399,
                })
            self.assertEqual(body["readiness"]["timing"]["frames"], 124)
            manifest = json.loads(
                (clip / "current_generation.json").read_text())
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["steps"], 20)
            self.assertEqual(manifest["seed"], int(large_seed))
            self.assertNotIn("duration", manifest)
            self.assertNotIn("references", manifest)
            self.assertNotIn("upscale", manifest)

            (clip / "current_prompt.txt").write_text("changed prompt\n")
            stale = client.get(
                f"/api/project/{project}/clips/clip-001").json()
            self.assertFalse(
                stale["generation_settings"]["readiness"]["ready"])
            self.assertEqual(
                stale["generation_settings"]["readiness"]["status"], "stale")

    def test_generation_settings_mode_references_and_options(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "generation-references")
            root = self.settings.studio_root / "projects" / project
            clip = root / "clips" / "clip-001"
            (clip / "current_prompt.txt").write_text(
                "A 10-second reference prompt\n")
            (root / "references" / "character.png").write_bytes(b"image")

            blocked = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(mode="r2v"),
            ).json()
            self.assertFalse(blocked["readiness"]["ready"])
            self.assertIn(
                "R2V prompt requires", blocked["readiness"]["reasons"][0])

            (clip / "current_prompt.txt").write_text(
                "A 10-second reference prompt using "
                "<Picture 1> (character.png)\n")

            ready = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(
                    mode="r2v", mp=0.9, steps=8, accel=True,
                ),
            ).json()
            self.assertTrue(ready["readiness"]["ready"])
            self.assertEqual(ready["readiness"]["references"], ["character.png"])
            self.assertEqual(ready["readiness"]["timing"]["requested_seconds"], 10)
            self.assertNotIn("references", ready["settings"])

            (root / "references" / "character.png").unlink()
            missing = client.get(
                f"/api/project/{project}/clips/clip-001/generation-settings"
            ).json()
            self.assertIn(
                "Missing reference", missing["readiness"]["reasons"][0])

            (clip / "current_prompt.txt").write_text(
                "A 10-second reference prompt using "
                "<Picture 1> (../character.png)\n")
            unsafe = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(mode="r2v"),
            ).json()
            self.assertFalse(unsafe["readiness"]["ready"])
            self.assertTrue(any(
                "invalid prompt reference" in reason
                for reason in unsafe["readiness"]["reasons"]
            ))

    def test_generation_settings_reject_unsafe_values(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "generation-validation")
            root = self.settings.studio_root / "projects" / project
            clip = root / "clips" / "clip-001"
            (clip / "current_prompt.txt").write_text("prompt\n")
            cases = [
                _generation_settings_payload(mp=1.2),
                _generation_settings_payload(width=1344, height=None),
                _generation_settings_payload(width=1536, height=768),
                _generation_settings_payload(steps=0),
                _generation_settings_payload(seed="9007199254740992"),
            ]
            for payload in cases:
                with self.subTest(payload=payload):
                    response = client.put(
                        f"/api/project/{project}/clips/clip-001/generation-settings",
                        json=payload,
                    )
                    self.assertEqual(response.status_code, 400)
            nan_payload = _generation_settings_payload(mp=float("nan"))
            response = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                content=json.dumps(nan_payload),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse((clip / "current_generation.json").exists())

    def test_clip_api_isolates_prompt_settings_takes_and_selection(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "clip-api")
            root = self.settings.studio_root / "projects" / project
            created = client.post(
                f"/api/project/{project}/clips", json={"title": "Closing"})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["clip"]["id"], "clip-002")

            first = root / "clips" / "clip-001"
            second = root / "clips" / "clip-002"
            (first / "current_prompt.txt").write_text("First 5-second prompt\n")
            (second / "current_prompt.txt").write_text("Second 6-second prompt\n")
            for clip_id, seed in (("clip-001", "11"), ("clip-002", "22")):
                response = client.put(
                    f"/api/project/{project}/clips/{clip_id}/generation-settings",
                    json=_generation_settings_payload(seed=seed),
                )
                self.assertEqual(response.status_code, 200)

            for clip, content in ((first, b"first"), (second, b"second")):
                generation = clip / "generations" / "001"
                generation.mkdir()
                (generation / "take.mp4").write_bytes(content)

            first_detail = client.get(
                f"/api/project/{project}/clips/clip-001").json()
            second_detail = client.get(
                f"/api/project/{project}/clips/clip-002").json()
            self.assertEqual(first_detail["current_prompt"], "First 5-second prompt\n")
            self.assertEqual(second_detail["current_prompt"], "Second 6-second prompt\n")
            self.assertEqual(first_detail["generation_settings"]["settings"]["seed"],
                             "11")
            self.assertEqual(second_detail["generation_settings"]["settings"]["seed"],
                             "22")

            first_take = client.get(
                f"/api/project/{project}/clips/clip-001/generations"
            ).json()["generations"][0]
            second_take = client.get(
                f"/api/project/{project}/clips/clip-002/generations"
            ).json()["generations"][0]
            self.assertIn("/clips/clip-001/", first_take["media"][0]["url"])
            self.assertIn("/clips/clip-002/", second_take["media"][0]["url"])
            self.assertEqual(client.get(first_take["media"][0]["url"]).content, b"first")
            self.assertEqual(client.get(second_take["media"][0]["url"]).content, b"second")

            selected = client.put(
                f"/api/project/{project}/clips/clip-002/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(selected.status_code, 200)
            self.assertEqual(selected.json()["clip"]["selected_take"], {
                "generation": "001", "filename": "take.mp4"})

            updated = client.patch(
                f"/api/project/{project}/clips/clip-002",
                json={"title": "Finale", "enabled": False},
            )
            self.assertEqual(updated.status_code, 200)
            blocked_selection = client.put(
                f"/api/project/{project}/clips/clip-002/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(blocked_selection.status_code, 400)
            reordered = client.put(
                f"/api/project/{project}/clips/order",
                json={"clip_ids": ["clip-002", "clip-001"]},
            )
            self.assertEqual(
                [entry["id"] for entry in reordered.json()["clips"]],
                ["clip-002", "clip-001"],
            )
            project_state = client.get(f"/api/project/{project}").json()
            self.assertEqual(project_state["clips"][0]["title"], "Finale")
            self.assertFalse(project_state["clips"][0]["enabled"])
            self.assertIsNone(project_state["clips"][1]["selected_take"])
            self.assertEqual(
                client.get(f"/api/project/{project}/generation-settings").status_code,
                404,
            )
            self.assertEqual(
                client.get(f"/api/project/{project}/generations").status_code, 404)

    def test_project_metadata_symlinks_are_not_read(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "metadata-symlink")
            root = self.settings.studio_root / "projects" / project
            outside = Path(self.temp.name) / "outside.txt"
            outside.write_text("outside secret")
            (root / "brief.md").unlink()
            (root / "brief.md").symlink_to(outside)
            listing = client.get("/api/projects").json()["projects"]
            self.assertEqual(listing[0]["brief"], "")
            self.assertEqual(
                client.get(f"/api/project/{project}").json()["brief"], "")

    def test_project_metadata_parent_swap_is_not_read(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "metadata-parent-swap")
            root = self.settings.studio_root / "projects" / project
            displaced = Path(self.temp.name) / "displaced-project"
            outside = Path(self.temp.name) / "outside-project"
            outside.mkdir()
            (outside / "brief.md").write_text("outside secret")
            real_open = os.open
            swapped = False

            def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
                nonlocal swapped
                if not swapped and Path(path).name == "brief.md":
                    swapped = True
                    root.rename(displaced)
                    root.symlink_to(outside, target_is_directory=True)
                return real_open(path, flags, mode, dir_fd=dir_fd)

            with patch("webapp.safe_files.os.open",
                       side_effect=swap_parent_at_final_open):
                listing = client.get("/api/projects").json()["projects"]

            self.assertTrue(swapped)
            self.assertEqual(listing[0]["brief"], "")
            self.assertEqual((outside / "brief.md").read_text(), "outside secret")

    def test_media_route_exposes_only_media_areas(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            root = self.settings.studio_root / "projects" / project
            (root / "references" / "reference.png").write_bytes(b"image")
            self.assertEqual(client.get(
                f"/media/projects/{project}/references/reference.png"
            ).status_code, 200)
            self.assertEqual(client.get(
                f"/media/projects/{project}/brief.md/x"
            ).status_code, 404)
            self.assertEqual(client.get(
                f"/media/projects/{project}/research/note.md"
            ).status_code, 404)

    def test_media_response_remains_bound_to_validated_file_identity(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "media-identity")
            root = self.settings.studio_root / "projects" / project
            source = root / "references" / "reference.png"
            displaced = root / "references" / "validated.png"
            outside = Path(self.temp.name) / "outside-secret.png"
            source.write_bytes(b"validated")
            outside.write_bytes(b"secret")
            real_pread = os.pread
            swapped = False

            def swap_before_read(descriptor, amount, offset):
                nonlocal swapped
                if not swapped:
                    swapped = True
                    source.rename(displaced)
                    source.symlink_to(outside)
                return real_pread(descriptor, amount, offset)

            with patch("webapp.safe_response.os.pread",
                       side_effect=swap_before_read):
                response = client.get(
                    f"/media/projects/{project}/references/reference.png")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"validated")
            self.assertTrue(swapped)
            self.assertEqual(source.read_bytes(), b"secret")

    def test_media_response_supports_single_byte_ranges(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "media-range")
            root = self.settings.studio_root / "projects" / project
            (root / "references" / "reference.png").write_bytes(b"012345")
            url = f"/media/projects/{project}/references/reference.png"

            partial = client.get(url, headers={"Range": "bytes=1-3"})
            self.assertEqual(partial.status_code, 206)
            self.assertEqual(partial.content, b"123")
            self.assertEqual(partial.headers["content-range"], "bytes 1-3/6")
            self.assertEqual(partial.headers["accept-ranges"], "bytes")

            unsatisfied = client.get(url, headers={"Range": "bytes=9-10"})
            self.assertEqual(unsatisfied.status_code, 416)
            self.assertEqual(unsatisfied.headers["content-range"], "bytes */6")

            (root / "references" / "empty.png").write_bytes(b"")
            empty = client.get(
                f"/media/projects/{project}/references/empty.png")
            self.assertEqual(empty.status_code, 200)
            self.assertEqual(empty.content, b"")
            self.assertEqual(empty.headers["content-length"], "0")

    def test_generation_detail_and_review_actions(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            (generation / "still.png").write_bytes(b"image")
            (generation / "prompt.txt").write_text("structured prompt\n")
            (generation / "meta.json").write_text(
                json.dumps({"seed": 42, "recipe": "r2v"}))

            listing = client.get(
                f"/api/project/{project}/clips/clip-001/generations"
            ).json()["generations"]
            self.assertEqual(listing[0]["files"], ["still.png", "video.mp4"])
            self.assertEqual(
                {item["kind"] for item in listing[0]["media"]},
                {"image", "video"},
            )
            detail = client.get(
                f"/api/project/{project}/clips/clip-001/generations/001"
            ).json()
            self.assertEqual(detail["prompt"], "structured prompt\n")
            self.assertEqual(detail["meta"]["seed"], 42)

            promoted = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(promoted.status_code, 200)
            promoted_name = promoted.json()["result"]["target"]
            self.assertEqual(
                (root / "final" / promoted_name).read_bytes(), b"video")
            self.assertEqual((generation / "video.mp4").read_bytes(), b"video")

            repeated = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(
                repeated.json()["result"]["target"], promoted_name)

            referenced = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/use-as-reference",
                json={"filename": "still.png"},
            )
            self.assertEqual(referenced.status_code, 200)
            reference_name = referenced.json()["result"]["target"]
            self.assertEqual(
                (root / "references" / reference_name).read_bytes(), b"image")
            refreshed = client.get(
                f"/api/project/{project}/clips/clip-001/generations/001"
            ).json()
            media = {item["name"]: item for item in refreshed["media"]}
            self.assertTrue(media["video.mp4"]["promoted"])
            self.assertTrue(media["still.png"]["reference"])

    def test_generation_review_actions_never_overwrite_and_reject_bad_sources(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-safety")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"new")
            (generation / "prompt.txt").write_text("not media")
            (root / "final" / "clip-001_001_video.mp4").write_bytes(b"existing")

            promoted = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "video.mp4"},
            )
            self.assertEqual(promoted.status_code, 200)
            self.assertEqual(
                promoted.json()["result"]["target"],
                "clip-001_001_video_2.mp4")
            self.assertEqual(
                (root / "final" / "clip-001_001_video.mp4").read_bytes(),
                b"existing")
            self.assertEqual(
                (root / "final" / "clip-001_001_video_2.mp4").read_bytes(),
                b"new")

            traversal = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "../video.mp4"},
            )
            unsupported = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/promote",
                json={"filename": "prompt.txt"},
            )
            missing = client.get(
                f"/api/project/{project}/clips/clip-001/generations/999")
            self.assertEqual(traversal.status_code, 400)
            self.assertEqual(unsupported.status_code, 415)
            self.assertEqual(missing.status_code, 404)

    def test_review_rollback_preserves_replacement_at_published_name(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-rollback-identity")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")
            target = root / "final" / "clip-001_001_video.mp4"
            displaced = root / "final" / ".published-original"

            def replace_target_then_fail(*_args, **_kwargs):
                target.rename(displaced)
                target.write_bytes(b"replacement")
                raise safe_files.SafeFilesystemError("injected review failure")

            with patch(
                    "webapp.media_review_store.atomic_write_bytes_at",
                    side_effect=replace_target_then_fail):
                response = client.post(
                    f"/api/project/{project}/clips/clip-001/"
                    "generations/001/promote",
                    json={"filename": "video.mp4"},
                )

            self.assertEqual(response.status_code, 400)
            self.assertEqual(target.read_bytes(), b"replacement")
            self.assertEqual(displaced.read_bytes(), b"video")

    def test_concurrent_promote_is_idempotent(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "review-concurrent")
            root = self.settings.studio_root / "projects" / project
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "video.mp4").write_bytes(b"video")

            def promote():
                return client.post(
                    f"/api/project/{project}/clips/clip-001/generations/001/promote",
                    json={"filename": "video.mp4"},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: promote(), range(2)))
            self.assertTrue(all(response.status_code == 200
                                for response in responses))
            self.assertEqual(
                {response.json()["result"]["target"] for response in responses},
                {"clip-001_001_video.mp4"},
            )
            self.assertEqual([
                item.name for item in (root / "final").iterdir()
                if not item.name.startswith(".")
            ], ["clip-001_001_video.mp4"])

    def test_media_and_upload_reject_symlinked_reference_directory(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "symlink")
            root = self.settings.studio_root / "projects" / project
            outside = Path(self.temp.name) / "outside"
            outside.mkdir()
            (root / "references").rmdir()
            (root / "references").symlink_to(outside, target_is_directory=True)
            (outside / "secret.png").write_bytes(b"secret")
            self.assertEqual(client.get(
                f"/media/projects/{project}/references/secret.png"
            ).status_code, 404)
            response = client.post(
                f"/api/project/{project}/references",
                files={"files": ("image.png", b"image", "image/png")},
            )
            self.assertEqual(response.status_code, 400)
            self.assertFalse((outside / "image.png").exists())
            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            (generation / "still.png").write_bytes(b"image")
            reviewed = client.post(
                f"/api/project/{project}/clips/clip-001/generations/001/use-as-reference",
                json={"filename": "still.png"},
            )
            self.assertEqual(reviewed.status_code, 400)
            self.assertFalse(
                (outside / "clip-001_001_still.png").exists())

    def test_concurrent_same_name_uploads_never_overwrite(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            barrier = Barrier(2)

            def upload(content):
                barrier.wait()
                return client.post(
                    f"/api/project/{project}/references",
                    files={"files": ("same.png", content, "image/png")},
                )

            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(upload, (b"first", b"second")))
            self.assertTrue(all(response.status_code == 201
                                for response in responses))
            names = sorted(
                response.json()["references"][0]["name"]
                for response in responses
            )
            self.assertEqual(names, ["same.png", "same_2.png"])
            directory = (
                self.settings.studio_root / "projects" / project / "references")
            contents = {
                path.read_bytes() for path in directory.iterdir()
                if not path.name.startswith(".")
            }
            self.assertEqual(contents, {b"first", b"second"})

    def test_failed_upload_batch_rolls_back(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            response = client.post(
                f"/api/project/{project}/references",
                files=[
                    ("files", ("small.png", b"ok", "image/png")),
                    ("files", ("large.png", b"x" * 2048, "image/png")),
                ],
            )
            self.assertEqual(response.status_code, 413)
            directory = (
                self.settings.studio_root / "projects" / project / "references")
            self.assertEqual([
                path for path in directory.iterdir()
                if not path.name.startswith(".")
            ], [])
