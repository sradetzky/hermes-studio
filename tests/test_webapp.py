import json
import multiprocessing
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event

from fastapi.testclient import TestClient

from scripts import design_studio as ds
from webapp.app import create_app
from webapp.config import Settings
from webapp.job_store import ActiveJobError, JobStore
from webapp.models import JobStatus
from webapp.studio_manager import StudioJobManager


def _create_job_in_process(database, barrier, results):
    store = JobStore(Path(database))
    barrier.wait()
    try:
        results.put(store.create_chat_job("project", "message").id)
    except ActiveJobError:
        results.put("rejected")


class PassiveManager:
    def __init__(self, _settings, store):
        self.store = store

    def start(self):
        self.store.initialize()

    def stop(self):
        pass

    def submit_chat(self, project, message):
        return self.store.create_chat_job(project, message)


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


class AppFactoryTests(WebAppTestCase):
    def test_app_creation_has_no_runtime_side_effects(self):
        create_app(self.settings, PassiveManager)
        self.assertFalse(self.settings.runtime_root.exists())
        with TestClient(self.app()):
            self.assertTrue(self.settings.database_path.is_file())

    def test_chat_route_returns_202_and_rejects_second_active_job(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client)
            first = client.post(
                f"/api/chat?pid={project}", json={"message": "hello"})
            second = client.post(
                f"/api/chat?pid={project}", json={"message": "again"})
            self.assertEqual(first.status_code, 202)
            self.assertEqual(first.json()["status"], "queued")
            self.assertEqual(second.status_code, 409)
            listing = client.get(f"/api/project/{project}/jobs").json()
            self.assertEqual([job["id"] for job in listing["jobs"]],
                             [first.json()["id"]])

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


class JobStoreTests(WebAppTestCase):
    def store(self):
        store = JobStore(self.settings.database_path)
        store.initialize()
        return store

    def test_cross_connection_active_job_claim_is_atomic(self):
        first_store = self.store()
        second_store = JobStore(self.settings.database_path)
        barrier = Barrier(2)

        def create(store):
            barrier.wait()
            try:
                return store.create_chat_job("project", "message").id
            except ActiveJobError:
                return "rejected"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(create, (first_store, second_store)))
        self.assertEqual(results.count("rejected"), 1)
        self.assertEqual(len(first_store.list_jobs("project")), 1)

    def test_cross_process_active_job_claim_is_atomic(self):
        store = self.store()
        context = multiprocessing.get_context("fork")
        barrier = context.Barrier(2)
        results = context.Queue()
        processes = [
            context.Process(
                target=_create_job_in_process,
                args=(str(self.settings.database_path), barrier, results),
            )
            for _ in range(2)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        values = [results.get(timeout=1) for _ in range(2)]
        self.assertEqual(values.count("rejected"), 1)
        self.assertEqual(len(store.list_jobs("project")), 1)

    def test_only_one_job_runs_globally(self):
        store = self.store()
        first = store.create_chat_job("one", "first")
        second = store.create_chat_job("two", "second")
        claimed = store.claim_next("worker-a")
        self.assertIsNotNone(claimed)
        assert claimed is not None
        self.assertEqual(claimed.id, first.id)
        self.assertIsNone(store.claim_next("worker-b"))
        store.complete(first.id, "worker-a", "done", "session-a")
        claimed_second = store.claim_next("worker-b")
        self.assertIsNotNone(claimed_second)
        assert claimed_second is not None
        self.assertEqual(claimed_second.id, second.id)

    def test_chat_session_and_completion_commit_together(self):
        store = self.store()
        job = store.create_chat_job("project", "question")
        store.claim_next("worker")
        store.complete(job.id, "worker", "answer", "session-1")
        total, events = store.chat_events("project")
        self.assertEqual(total, 2)
        self.assertEqual(
            [(event.role, event.content) for event in events],
            [("user", "question"), ("assistant", "answer")],
        )
        self.assertEqual(store.get_session("project"), "session-1")
        self.assertEqual(store.get_job(job.id).reply, "answer")

    def test_existing_chat_jsonl_imports_once_and_logs_corruption(self):
        store = self.store()
        chat = Path(self.temp.name) / "chat.jsonl"
        chat.write_text(
            json.dumps({"role": "user", "content": "one"}) +
            "\n{broken\n" +
            json.dumps({"role": "assistant", "content": "two"}) + "\n",
            encoding="utf-8",
        )
        with self.assertLogs("webapp.job_store", level="WARNING"):
            store.import_chat_if_empty("project", chat)
        store.import_chat_if_empty("project", chat)
        total, events = store.chat_events("project")
        self.assertEqual(total, 2)
        self.assertEqual([event.content for event in events], ["one", "two"])


class StudioManagerTests(WebAppTestCase):
    def wait_for_terminal(self, store, job_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = store.get_job(job_id)
            if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return job
            time.sleep(0.05)
        self.fail("job did not reach a terminal state")

    def test_real_subprocess_completes_and_exports_chat(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "manager")
        store = JobStore(self.settings.database_path)

        def command(_job, _session):
            return [
                sys.executable, "-c",
                "import sys; print('reply'); "
                "print('session_id: session-1', file=sys.stderr)",
            ]

        manager = StudioJobManager(
            self.settings, store, command_builder=command,
            cleanup_callback=lambda: None,
        )
        manager.start()
        try:
            job = manager.submit_chat(project.name, "question")
            completed = self.wait_for_terminal(store, job.id)
        finally:
            manager.stop()
        self.assertEqual(completed.status, JobStatus.COMPLETED)
        rows = [json.loads(line) for line in
                (project / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(row["role"], row["content"]) for row in rows],
            [("user", "question"), ("assistant", "reply")],
        )

    def test_shutdown_terminates_tracked_child(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "shutdown")
        store = JobStore(self.settings.database_path)

        def command(_job, _session):
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        manager = StudioJobManager(
            self.settings, store, command_builder=command,
            cleanup_callback=lambda: None,
        )
        manager.start()
        job = manager.submit_chat(project.name, "question")
        deadline = time.monotonic() + 5
        running = None
        while time.monotonic() < deadline:
            running = store.get_job(job.id)
            if running.pid:
                break
            time.sleep(0.05)
        self.assertIsNotNone(running)
        assert running is not None
        self.assertIsNotNone(running.pid)
        child_pid = running.pid
        self.assertIsNotNone(child_pid)
        assert child_pid is not None
        manager.stop()
        failed = store.get_job(job.id)
        self.assertEqual(failed.status, JobStatus.FAILED)
        with self.assertRaises(ProcessLookupError):
            os.kill(child_pid, 0)

    def test_second_manager_does_not_recover_live_owner_job(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "leases")
        first_store = JobStore(self.settings.database_path)
        second_store = JobStore(self.settings.database_path)

        def command(_job, _session):
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        first = StudioJobManager(
            self.settings, first_store, command_builder=command,
            cleanup_callback=lambda: None,
        )
        second = StudioJobManager(
            self.settings, second_store, command_builder=command,
            cleanup_callback=lambda: None,
        )
        first.start()
        job = first.submit_chat(project.name, "question")
        deadline = time.monotonic() + 5
        running = first_store.get_job(job.id)
        while time.monotonic() < deadline:
            running = first_store.get_job(job.id)
            if running.pid:
                break
            time.sleep(0.05)
        self.assertEqual(running.status, JobStatus.RUNNING)
        self.assertIsNotNone(running.pid)
        second.start()
        try:
            still_running = first_store.get_job(job.id)
            self.assertEqual(still_running.status, JobStatus.RUNNING)
            assert still_running.pid is not None
            os.kill(still_running.pid, 0)
        finally:
            second.stop()
            first.stop()

    def test_failed_process_commits_user_and_system_event_atomically(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "failure")
        store = JobStore(self.settings.database_path)

        def command(_job, _session):
            return [sys.executable, "-c", "raise SystemExit(7)"]

        manager = StudioJobManager(
            self.settings, store, command_builder=command,
            cleanup_callback=lambda: None,
        )
        manager.start()
        try:
            job = manager.submit_chat(project.name, "question")
            with self.assertLogs("webapp.studio_manager", level="ERROR"):
                failed = self.wait_for_terminal(store, job.id)
        finally:
            manager.stop()
        self.assertEqual(failed.status, JobStatus.FAILED)
        total, events = store.chat_events(project.name)
        self.assertEqual(total, 2)
        self.assertEqual(
            [event.role for event in events], ["user", "system"])
        self.assertIn("failed", events[1].content.lower())

    def test_startup_recovers_job_without_live_worker_lease(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "recovery")
        store = JobStore(self.settings.database_path)
        store.initialize()
        job = store.create_chat_job(project.name, "question")
        store.claim_next("dead-worker")
        cleanup_calls = []
        manager = StudioJobManager(
            self.settings, store,
            command_builder=lambda *_: [sys.executable, "-c", "pass"],
            cleanup_callback=lambda: cleanup_calls.append(True),
        )
        manager.start()
        try:
            recovered = store.get_job(job.id)
        finally:
            manager.stop()
        self.assertEqual(recovered.status, JobStatus.FAILED)
        self.assertEqual(cleanup_calls, [True])

    def test_cleanup_finishes_before_next_global_job_starts(self):
        ds.studio_root(str(self.settings.studio_root))
        first_project = ds.create_project(self.settings.studio_root, "first")
        second_project = ds.create_project(self.settings.studio_root, "second")
        first_store = JobStore(self.settings.database_path)
        second_store = JobStore(self.settings.database_path)
        cleanup_started = Event()
        release_cleanup = Event()

        def command(job, _session):
            if job.project == first_project.name:
                return [
                    sys.executable, "-c",
                    "import time; time.sleep(.2); raise SystemExit(7)",
                ]
            return [sys.executable, "-c", "print('ok')"]

        def cleanup():
            cleanup_started.set()
            release_cleanup.wait(timeout=5)

        first = StudioJobManager(
            self.settings, first_store, command_builder=command,
            cleanup_callback=cleanup,
        )
        second = StudioJobManager(
            self.settings, second_store,
            command_builder=command,
            cleanup_callback=lambda: None,
        )
        first.start()
        first_job = first.submit_chat(first_project.name, "fail")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if first_store.get_job(first_job.id).status is JobStatus.RUNNING:
                break
            time.sleep(0.05)
        second.start()
        second_job = second.submit_chat(second_project.name, "wait")
        self.assertTrue(cleanup_started.wait(timeout=5))
        self.assertEqual(
            first_store.get_job(first_job.id).status, JobStatus.RUNNING)
        self.assertEqual(
            second_store.get_job(second_job.id).status, JobStatus.QUEUED)
        release_cleanup.set()
        try:
            first_done = self.wait_for_terminal(first_store, first_job.id)
            second_done = self.wait_for_terminal(second_store, second_job.id)
        finally:
            second.stop()
            first.stop()
        self.assertEqual(first_done.status, JobStatus.FAILED)
        self.assertEqual(second_done.status, JobStatus.COMPLETED)

    def test_surviving_manager_reaps_peer_after_lease_expires(self):
        settings = replace(
            self.settings, worker_lease_timeout_seconds=1)
        ds.studio_root(str(settings.studio_root))
        first_project = ds.create_project(settings.studio_root, "orphan")
        second_project = ds.create_project(settings.studio_root, "next")
        store = JobStore(settings.database_path)
        store.initialize()
        store.register_worker("dead-owner")
        first_job = store.create_chat_job(first_project.name, "orphaned")
        store.claim_next("dead-owner")
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        store.set_pid(first_job.id, "dead-owner", child.pid)

        def command(_job, _session):
            return [sys.executable, "-c", "print('next')"]

        survivor = StudioJobManager(
            settings, JobStore(settings.database_path),
            command_builder=command, cleanup_callback=lambda: None,
        )
        survivor.start()
        second_job = survivor.submit_chat(second_project.name, "continue")
        connection = sqlite3.connect(settings.database_path)
        with connection:
            connection.execute(
                "UPDATE workers SET heartbeat = 0 WHERE owner_id = 'dead-owner'")
        connection.close()
        try:
            first_done = self.wait_for_terminal(store, first_job.id, timeout=8)
            second_done = self.wait_for_terminal(store, second_job.id, timeout=8)
        finally:
            survivor.stop()
            if child.poll() is None:
                child.kill()
                child.wait()
        self.assertEqual(first_done.status, JobStatus.FAILED)
        self.assertEqual(second_done.status, JobStatus.COMPLETED)
        with self.assertRaises(ProcessLookupError):
            os.kill(child.pid, 0)


if __name__ == "__main__":
    unittest.main()