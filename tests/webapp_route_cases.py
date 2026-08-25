import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from threading import Thread
from unittest.mock import patch

from fastapi.testclient import TestClient

from webapp.app import create_app
from webapp.config import Settings
from tests.webapp_route_support import (
    RoutePassiveManager as PassiveManager,
    RouteWebAppTestCase as WebAppTestCase,
)
from tests.webapp_test_support import (
    generation_settings_payload as _generation_settings_payload,
)

class LauncherScriptTests(unittest.TestCase):
    def test_user_service_keeps_loopback_launcher_and_external_host_config(self):
        unit = (
            Path(__file__).resolve().parent.parent /
            "webapp" / "hermes-studio.service"
        ).read_text()
        self.assertIn("ExecStart=%h/.local/bin/hermes-studio-web", unit)
        self.assertNotIn("repos/hermes-studio", unit)
        self.assertIn(
            "EnvironmentFile=-%h/.config/hermes-studio/environment", unit)
        self.assertIn("Restart=on-failure", unit)
        self.assertIn("SuccessExitStatus=143", unit)

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

    def test_exact_tailnet_host_allows_same_origin_reads_and_writes(self):
        host = "studio-device.example.ts.net"
        settings = replace(
            self.settings,
            trusted_hosts=("127.0.0.1", "localhost", "testserver", host),
        )
        with TestClient(create_app(settings, PassiveManager)) as client:
            self.assertEqual(client.get(
                "/api/projects", headers={"Host": f"{host}:8788"},
            ).status_code, 200)
            response = client.post(
                "/api/projects",
                headers={
                    "Host": f"{host}:8788",
                    "Origin": f"https://{host}:8788",
                },
                json={"name": "tailnet", "brief": ""},
            )
        self.assertEqual(response.status_code, 200)

    def test_trusted_hosts_extend_from_environment(self):
        with patch.dict(os.environ, {
            "HERMES_STUDIO_TRUSTED_HOSTS": "studio-device.example.ts.net",
        }, clear=False):
            settings = Settings.from_environment()
        self.assertEqual(settings.trusted_hosts, (
            "127.0.0.1", "localhost", "testserver",
            "studio-device.example.ts.net",
        ))

    def test_trusted_hosts_reject_wildcards_and_ports(self):
        for value in ("*.ts.net", "natasha.ts.net:8788"):
            with self.subTest(value=value), patch.dict(os.environ, {
                "HERMES_STUDIO_TRUSTED_HOSTS": value,
            }, clear=False):
                with self.assertRaisesRegex(ValueError, "exact DNS names"):
                    Settings.from_environment()

    def test_default_job_timeout_covers_long_h3_runs(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STUDIO_JOB_TIMEOUT_SECONDS", None)
            settings = Settings.from_environment()
        self.assertEqual(settings.job_timeout_seconds, 10_800)

    def test_project_metadata_update_preserves_id_and_updates_listing(self):
        with TestClient(self.app()) as client:
            project_id = self.create_project(client, "metadata")
            response = client.patch(
                f"/api/project/{project_id}",
                json={"title": "Display title", "brief": "Updated brief"},
            )

            self.assertEqual(response.status_code, 200, response.text)
            project = response.json()["project"]
            self.assertEqual(project["id"], project_id)
            self.assertEqual(project["title"], "Display title")
            self.assertEqual(project["brief"], "Updated brief")
            self.assertEqual(project["clips"][0]["id"], "clip-001")
            project_path = self.settings.studio_root / "projects" / project_id
            self.assertTrue(project_path.is_dir())

            detail = client.get(f"/api/project/{project_id}").json()
            listing = client.get("/api/projects").json()["projects"]
            self.assertEqual(detail, project)
            self.assertEqual(listing[0]["id"], project_id)
            self.assertEqual(listing[0]["title"], "Display title")
            self.assertEqual(listing[0]["brief"], "Updated brief")
            self.assertEqual(
                (project_path / "brief.md").read_text(encoding="utf-8"),
                "Updated brief",
            )

    def test_project_metadata_update_validates_body_and_rejects_active_job(self):
        app = self.app()
        with TestClient(app) as client:
            project_id = self.create_project(client, "metadata-guards")
            self.assertEqual(client.patch(
                f"/api/project/{project_id}",
                json={"id": "replacement", "title": "Title", "brief": "Brief"},
            ).status_code, 422)
            empty = client.patch(
                f"/api/project/{project_id}",
                json={"title": "   ", "brief": "Brief"},
            )
            self.assertEqual(empty.status_code, 400)

            queued = client.post(
                f"/api/project/{project_id}/clips/clip-001/chat",
                json={"message": "Hold this project"},
            )
            self.assertEqual(queued.status_code, 202)
            blocked = client.patch(
                f"/api/project/{project_id}",
                json={"title": "Blocked", "brief": "Blocked"},
            )
            self.assertEqual(blocked.status_code, 409)
            self.assertIn("active job", blocked.json()["detail"])

    def test_comfy_queue_route_sanitizes_workflows_and_preserves_order(self):
        h3_graph = {
            "cond": {"class_type": "MiniMaxH3ReferenceToVideo", "inputs": {
                "prompt": "private prompt", "width": 928, "height": 544,
                "length": 243, "ref_images.ref_image_0": ["ref", 0],
            }},
            "ref": {"class_type": "LoadImage", "inputs": {
                "image": "private-reference.png",
            }},
            "scheduler": {"class_type": "BasicScheduler", "inputs": {"steps": 8}},
            "noise": {"class_type": "RandomNoise", "inputs": {"noise_seed": 42}},
            "fused": {"class_type": "MiniMaxH3FusedModulation", "inputs": {
                "enabled": True,
            }},
            "chunk": {"class_type": "MiniMaxH3ChunkFeedForward", "inputs": {
                "enabled": True,
            }},
            "video": {"class_type": "CreateVideo", "inputs": {"fps": 24}},
        }
        krea_graph = {
            "clip": {"class_type": "CLIPLoader", "inputs": {
                "clip_name": "private-model.safetensors", "type": "krea2",
            }},
            "latent": {"class_type": "EmptyLatentImage", "inputs": {
                "width": 1024, "height": 1024,
            }},
            "sample": {"class_type": "KSampler", "inputs": {
                "seed": 43, "steps": 20,
            }},
        }
        payloads = {
            "http://127.0.0.1:8188/queue": {
                "queue_running": [[
                    7, "running-id", h3_graph, {"create_time": 990_000}, ["9"],
                ]],
                "queue_pending": [[
                    8, "next-id", krea_graph, {"create_time": 985_000}, ["9"],
                ]],
            },
            ("http://127.0.0.1:8188/api/jobs?status=completed&sort_by=created_at"
             "&sort_order=desc&limit=1"): {
                "jobs": [{
                    "id": "completed-id", "status": "completed",
                    "execution_start_time": 100_000,
                    "execution_end_time": 682_274,
                }],
            },
            "http://127.0.0.1:8188/api/jobs/completed-id": {
                "workflow": {"prompt": h3_graph},
            },
        }

        class Response:
            def __init__(self, payload):
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        requested = []

        def fetch(request, **_kwargs):
            requested.append(request.full_url)
            return Response(payloads[request.full_url])

        with patch("webapp.comfy_queue.urlopen", side_effect=fetch), \
                patch("webapp.comfy_queue.time.time", return_value=1000), \
                patch("webapp.comfy_queue.time.monotonic", return_value=100):
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue?include_recent=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": True,
            "running": [{
                "prompt_id": "running-id", "position": 0,
                "recipe": "H3", "mode": "R2V", "kind": "video",
                "width": 928, "height": 544, "frames": 243,
                "media_seconds": 10, "steps": 8, "accel": True,
                "seed": "42", "elapsed_seconds": 0,
            }],
            "pending": [{
                "prompt_id": "next-id", "position": 1,
                "recipe": "Krea 2", "kind": "image",
                "width": 1024, "height": 1024, "steps": 20,
                "seed": "43", "queued_seconds": 15,
            }],
            "recent_completed": {
                "prompt_id": "completed-id", "status": "completed",
                "execution_seconds": 582.274, "completed_at": 682_274,
                "recipe": "H3", "mode": "R2V", "kind": "video",
                "width": 928, "height": 544, "frames": 243,
                "media_seconds": 10, "steps": 8, "accel": True,
                "seed": "42",
            },
        })
        serialized = response.text
        for private_value in (
                "private prompt", "private-reference.png", "private-model.safetensors"):
            self.assertNotIn(private_value, serialized)
        self.assertEqual(set(requested), set(payloads))

    def test_comfy_queue_route_reports_unavailable_without_failing_refresh(self):
        with patch("webapp.comfy_queue.urlopen", side_effect=OSError("offline")):
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": False,
            "running": [],
            "pending": [],
            "recent_completed": None,
            "error": "ComfyUI queue unavailable",
        })

    def test_comfy_queue_stays_available_when_completed_jobs_api_is_unavailable(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "queue_running": [],
                    "queue_pending": [[4, "queued-id", {}, {}, ["9"]]],
                }).encode()

        def fetch(request, **_kwargs):
            if request.full_url.endswith("/queue"):
                return Response()
            raise OSError("jobs endpoint unavailable")

        with patch("webapp.comfy_queue.urlopen", side_effect=fetch):
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue?include_recent=true")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": True,
            "running": [],
            "pending": [{"prompt_id": "queued-id", "position": 1}],
            "recent_completed": None,
        })

    def test_comfy_queue_fetches_completion_history_only_on_demand(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps({
                    "queue_running": [], "queue_pending": [],
                }).encode()

        with patch("webapp.comfy_queue.urlopen", return_value=Response()) as fetch:
            with TestClient(self.app()) as client:
                response = client.get("/api/comfyui/queue")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "available": True, "running": [], "pending": [],
            "recent_completed": None,
        })
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(
            fetch.call_args.args[0].full_url, "http://127.0.0.1:8188/queue")

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
            chat = client.get(
                f"/api/project/{project}/clips/clip-001/chat").json()
            self.assertEqual(
                [(message["role"], message["content"]) for message in chat["messages"]],
                [("user", "hello")],
            )
            self.assertEqual(chat["cursor"], chat["messages"][-1]["id"])
            unchanged = client.get(
                f"/api/project/{project}/clips/clip-001/chat"
                f"?after={chat['cursor']}"
            ).json()
            self.assertEqual(unchanged, {
                "cursor": chat["cursor"], "messages": []})
            activity = client.get(
                f"/api/project/{project}/clips/clip-001/events").json()
            self.assertEqual(activity["events"][0]["event_type"], "job.queued")
            self.assertEqual(activity["events"][0]["profile"], "studio")
            listing = client.get(f"/api/project/{project}/jobs").json()
            self.assertEqual([job["id"] for job in listing["jobs"]],
                             [first.json()["id"]])

    def test_project_and_clip_chat_routes_keep_history_and_activity_separate(self):
        app = self.app()
        with TestClient(app) as client:
            project = self.create_project(client, "scoped-chat")
            project_turn = client.post(
                f"/api/project/{project}/chat",
                json={"message": "project direction"},
            )
            self.assertEqual(project_turn.status_code, 202)
            self.assertEqual(project_turn.json()["clip_id"], "")
            self.assertEqual(project_turn.json()["chat_scope"], "project")
            app.state.job_store.fail(
                project_turn.json()["id"], "test completion")

            clip_turn = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "clip refinement"},
            )
            self.assertEqual(clip_turn.status_code, 202)
            self.assertEqual(clip_turn.json()["chat_scope"], "clip")

            project_chat = client.get(
                f"/api/project/{project}/chat").json()["messages"]
            clip_chat = client.get(
                f"/api/project/{project}/clips/clip-001/chat").json()["messages"]
            self.assertEqual(
                [message["content"] for message in project_chat],
                ["project direction", "Studio job failed: test completion"],
            )
            self.assertEqual(
                [message["content"] for message in clip_chat],
                ["clip refinement"],
            )
            self.assertTrue(all(message["clip_id"] == ""
                                for message in project_chat))
            self.assertTrue(all(message["clip_id"] == "clip-001"
                                for message in clip_chat))

            project_events = client.get(
                f"/api/project/{project}/events").json()["events"]
            clip_events = client.get(
                f"/api/project/{project}/clips/clip-001/events").json()["events"]
            self.assertEqual(
                {event["job_id"] for event in project_events},
                {project_turn.json()["id"]},
            )
            self.assertEqual(
                {event["job_id"] for event in clip_events},
                {clip_turn.json()["id"]},
            )

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

    def test_active_job_blocks_clip_and_generation_contract_mutations(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "active-contract-guard")
            clip_path = (
                self.settings.studio_root / "projects" / project /
                "clips" / "clip-001")
            (clip_path / "current_prompt.txt").write_text(
                "A complete 5-second H3 prompt\n", encoding="utf-8")
            saved = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(),
            )
            self.assertEqual(saved.status_code, 200)
            queued = client.post(
                f"/api/project/{project}/clips/clip-001/chat",
                json={"message": "inspect this clip"},
            )
            self.assertEqual(queued.status_code, 202)

            blocked = [
                client.patch(
                    f"/api/project/{project}/clips/clip-001",
                    json={"title": "Changed", "enabled": False},
                ),
                client.put(
                    f"/api/project/{project}/clips/clip-001/generation-settings",
                    json=_generation_settings_payload(steps=7),
                ),
                client.post(
                    f"/api/project/{project}/clips",
                    json={"title": "Another clip"},
                ),
                client.put(
                    f"/api/project/{project}/clips/order",
                    json={"clip_ids": ["clip-001"]},
                ),
            ]
            for response in blocked:
                self.assertEqual(response.status_code, 409)
                self.assertIn("active job", response.json()["detail"])

            clip = client.get(
                f"/api/project/{project}/clips/clip-001").json()
            self.assertEqual(clip["title"], "Main clip")
            self.assertTrue(clip["enabled"])
            self.assertEqual(
                clip["generation_settings"]["settings"]["steps"], 20)
            self.assertEqual(len(client.get(
                f"/api/project/{project}/clips").json()["clips"]), 1)

    def test_generate_current_prompt_queues_exact_validated_studio_job(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "generate-current")
            root = self.settings.studio_root / "projects" / project
            clip = root / "clips" / "clip-001"
            (clip / "current_prompt.txt").write_text(
                "A complete 5-second H3 generation prompt\n")
            contract = client.put(
                f"/api/project/{project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(seed="42", steps=8, accel=True),
            ).json()
            token = {
                "prompt_sha256": contract["manifest"]["prompt_sha256"],
                "settings_updated_at": contract["manifest"]["updated_at"],
            }

            queued = client.post(
                f"/api/project/{project}/clips/clip-001/generate", json=token)

            self.assertEqual(queued.status_code, 202)
            job = queued.json()
            self.assertEqual(job["kind"], "generate")
            self.assertEqual(job["profile"], "studio")
            self.assertEqual(job["clip_id"], "clip-001")
            self.assertEqual(job["status"], "queued")
            payload = json.loads(job["message"])
            self.assertEqual(payload["action"], "generate-current-prompt")
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["prompt_sha256"], token["prompt_sha256"])
            self.assertEqual(
                payload["settings_updated_at"], token["settings_updated_at"])
            self.assertEqual(
                payload["prompt"], "A complete 5-second H3 generation prompt\n")
            self.assertEqual(payload["expected_generation_id"], "001")
            chat = client.get(
                f"/api/project/{project}/clips/clip-001/chat"
            ).json()["messages"]
            self.assertEqual(
                [(entry["role"], entry["content"]) for entry in chat],
                [("user", "Generate with this prompt")],
            )
            duplicate = client.post(
                f"/api/project/{project}/clips/clip-001/generate", json=token)
            self.assertEqual(duplicate.status_code, 409)

    def test_generate_current_prompt_rejects_unready_disabled_and_stale_contracts(self):
        with TestClient(self.app()) as client:
            unready_project = self.create_project(client, "generate-unready")
            unready = client.post(
                f"/api/project/{unready_project}/clips/clip-001/generate",
                json={"prompt_sha256": "0" * 64, "settings_updated_at": "missing"},
            )
            self.assertEqual(unready.status_code, 409)

            disabled_project = self.create_project(client, "generate-disabled")
            disabled_root = self.settings.studio_root / "projects" / disabled_project
            disabled_clip = disabled_root / "clips" / "clip-001"
            (disabled_clip / "current_prompt.txt").write_text(
                "A complete 5-second H3 generation prompt\n")
            disabled_contract = client.put(
                f"/api/project/{disabled_project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(),
            ).json()
            client.patch(
                f"/api/project/{disabled_project}/clips/clip-001",
                json={"enabled": False},
            )
            disabled = client.post(
                f"/api/project/{disabled_project}/clips/clip-001/generate",
                json={
                    "prompt_sha256": disabled_contract["manifest"]["prompt_sha256"],
                    "settings_updated_at": disabled_contract["manifest"]["updated_at"],
                },
            )
            self.assertEqual(disabled.status_code, 409)

            stale_project = self.create_project(client, "generate-stale")
            stale_root = self.settings.studio_root / "projects" / stale_project
            stale_clip = stale_root / "clips" / "clip-001"
            (stale_clip / "current_prompt.txt").write_text(
                "A complete 5-second H3 generation prompt\n")
            old_contract = client.put(
                f"/api/project/{stale_project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(),
            ).json()
            client.put(
                f"/api/project/{stale_project}/clips/clip-001/generation-settings",
                json=_generation_settings_payload(steps=8),
            )
            stale = client.post(
                f"/api/project/{stale_project}/clips/clip-001/generate",
                json={
                    "prompt_sha256": old_contract["manifest"]["prompt_sha256"],
                    "settings_updated_at": old_contract["manifest"]["updated_at"],
                },
            )
            self.assertEqual(stale.status_code, 409)
            self.assertEqual(
                client.get(f"/api/project/{stale_project}/jobs").json()["jobs"], [])

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

    def test_generation_can_snapshot_previous_selected_take_last_frame(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "previous-take-input")
            root = self.settings.studio_root / "projects" / project
            created = client.post(
                f"/api/project/{project}/clips",
                json={"title": "Continuation"},
            )
            self.assertEqual(created.status_code, 201)
            self.assertEqual(created.json()["clip"]["id"], "clip-002")

            generation = root / "clips" / "clip-001" / "generations" / "001"
            generation.mkdir()
            source = generation / "take.mp4"
            subprocess.run([
                "ffmpeg", "-v", "error", "-nostdin", "-f", "lavfi",
                "-i", "color=c=blue:s=64x64:r=4:d=0.5", "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
            ], check=True, capture_output=True)
            selected = client.put(
                f"/api/project/{project}/clips/clip-001/selected-take",
                json={"generation": "001", "filename": "take.mp4"},
            )
            self.assertEqual(selected.status_code, 200)

            before_prompt = client.get(
                f"/api/project/{project}/clips/clip-002").json()
            self.assertEqual(
                before_prompt["generation_settings"]
                ["previous_selected_take_input"],
                {
                    "eligible": True,
                    "source_clip_id": "clip-001",
                    "source_generation_id": "001",
                    "source_filename": "take.mp4",
                    "picture_number": 1,
                },
            )

            (root / "references" / "identity.png").write_bytes(b"identity")
            clip = root / "clips" / "clip-002"
            (clip / "current_prompt.txt").write_text(
                "A 5-second continuation with <Picture 1> (identity.png) and "
                "<Picture 2> as the exact opening continuity anchor\n")
            contract = client.put(
                f"/api/project/{project}/clips/clip-002/generation-settings",
                json=_generation_settings_payload(mode="r2v"),
            ).json()
            self.assertEqual(contract["previous_selected_take_input"], {
                "eligible": True,
                "source_clip_id": "clip-001",
                "source_generation_id": "001",
                "source_filename": "take.mp4",
                "picture_number": 2,
            })

            queued = client.post(
                f"/api/project/{project}/clips/clip-002/generate",
                json={
                    "prompt_sha256": contract["manifest"]["prompt_sha256"],
                    "settings_updated_at": contract["manifest"]["updated_at"],
                    "use_previous_take_last_frame": True,
                },
            )
            self.assertEqual(queued.status_code, 202)
            payload = json.loads(queued.json()["message"])
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(
                [item["type"] for item in payload["execution"]["inputs"]],
                ["project_reference", "previous_selected_take_last_frame"])
            previous = payload["execution"]["inputs"][1]
            self.assertEqual(previous["slot"], 2)
            derived = (
                clip / "generation-inputs" / previous["derived_filename"])
            self.assertTrue(derived.is_file())
            self.assertGreater(derived.stat().st_size, 0)
