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
            job = manager.submit_chat(project.name, "clip-001", "question")
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

    def test_specialist_command_uses_minimal_toolset(self):
        store = JobStore(self.settings.database_path)
        store.initialize()
        manager = StudioJobManager(self.settings, store)
        job = store.create_chat_job(
            "project", "plan", profile="studio-storyboarder",
            clip_id="clip-002")
        command = manager._default_command(job, None)
        self.assertEqual(
            command[command.index("-t") + 1], "file,terminal,skills")
        self.assertNotIn("all", command)
        query = command[command.index("-q") + 1]
        self.assertIn("Project ID: project", query)
        self.assertIn("Active clip ID: clip-002", query)
        self.assertIn("/projects/project/clips/clip-002", query)
        environment = manager._job_environment(job)
        self.assertEqual(environment["HERMES_STUDIO_CLIP"], "clip-002")
        self.assertTrue(environment["HERMES_STUDIO_CLIP_PATH"].endswith(
            "/projects/project/clips/clip-002"))

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
        job = manager.submit_chat(project.name, "clip-001", "question")
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
        job = first.submit_chat(project.name, "clip-001", "question")
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
            job = manager.submit_chat(project.name, "clip-001", "question")
            with self.assertLogs("webapp.studio_manager", level="ERROR"):
                failed = self.wait_for_terminal(store, job.id)
        finally:
            manager.stop()
        self.assertEqual(failed.status, JobStatus.FAILED)
        cursor, events = store.chat_events(project.name)
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [event.role for event in events], ["user", "system"])
        self.assertIn("failed", events[1].content.lower())

    def test_scheduler_survives_transient_store_error(self):
        project = ds.create_project(self.settings.studio_root, "scheduler-retry")
        store = JobStore(self.settings.database_path)
        manager = StudioJobManager(
            self.settings, store,
            command_builder=lambda *_: [
                sys.executable, "-c", "print('recovered')"],
            cleanup_callback=lambda: None,
        )
        store.initialize()
        store.register_worker(manager.owner_id)
        job = manager.submit_chat(project.name, "clip-001", "question")
        real_heartbeat = store.heartbeat_worker
        calls = 0

        def flaky_heartbeat(owner_id):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("temporary database failure")
            real_heartbeat(owner_id)

        with (
            patch.object(store, "heartbeat_worker", side_effect=flaky_heartbeat),
            self.assertLogs("webapp.studio_manager", level="ERROR"),
        ):
            scheduler = Thread(target=manager._scheduler_loop)
            manager._scheduler = scheduler
            scheduler.start()
            try:
                completed = self.wait_for_terminal(store, job.id)
            finally:
                manager.stop()
        self.assertEqual(completed.status, JobStatus.COMPLETED)
        _, chat = store.chat_events(project.name)
        self.assertEqual(chat[-1].content, "recovered")

    def test_specialist_failure_does_not_interrupt_comfyui(self):
        project = ds.create_project(self.settings.studio_root, "specialist-failure")
        store = JobStore(self.settings.database_path)
        cleanup_calls = []

        manager = StudioJobManager(
            self.settings,
            store,
            command_builder=lambda *_: [
                sys.executable, "-c", "raise SystemExit(7)"],
            cleanup_callback=lambda: cleanup_calls.append(True),
        )
        manager.start()
        try:
            job = manager.submit_chat(
                project.name, "clip-001", "plan", "studio-storyboarder")
            with self.assertLogs("webapp.studio_manager", level="ERROR"):
                failed = self.wait_for_terminal(store, job.id)
        finally:
            manager.stop()
        self.assertEqual(failed.status, JobStatus.FAILED)
        self.assertEqual(cleanup_calls, [])

    def test_startup_recovers_job_without_live_worker_lease(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(self.settings.studio_root, "recovery")
        store = JobStore(self.settings.database_path)
        store.initialize()
        job = store.create_chat_job(
            project.name, "question", clip_id="clip-001")
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

    def test_startup_recovers_specialist_without_interrupting_comfyui(self):
        ds.studio_root(str(self.settings.studio_root))
        project = ds.create_project(
            self.settings.studio_root, "specialist-recovery")
        store = JobStore(self.settings.database_path)
        store.initialize()
        job = store.create_chat_job(
            project.name, "plan", "studio-storyboarder",
            clip_id="clip-001")
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
        self.assertEqual(cleanup_calls, [])

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
        first_job = first.submit_chat(
            first_project.name, "clip-001", "fail")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if first_store.get_job(first_job.id).status is JobStatus.RUNNING:
                break
            time.sleep(0.05)
        second.start()
        second_job = second.submit_chat(
            second_project.name, "clip-001", "wait")
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

    def test_orphan_reaper_refuses_unowned_process_with_hermes_in_path(self):
        store = JobStore(self.settings.database_path)
        store.initialize()
        job = store.create_chat_job(
            "project", "orphaned", clip_id="clip-001")
        running = store.claim_next("dead-owner")
        assert running is not None
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
        )
        try:
            store.set_process(
                job.id, "dead-owner", child.pid,
                process_start_time(child.pid))
            with self.assertLogs("webapp.studio_manager", level="ERROR"):
                StudioJobManager._terminate_orphan_process(store.get_job(job.id))
            self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_orphan_reaper_refuses_reused_pid_identity(self):
        store = JobStore(self.settings.database_path)
        store.initialize()
        job = store.create_chat_job(
            "project", "orphaned", clip_id="clip-001")
        running = store.claim_next("dead-owner")
        assert running is not None
        environment = {
            **os.environ,
            "HERMES_STUDIO_JOB_ID": running.id,
            "HERMES_STUDIO_PROJECT": running.project,
            "HERMES_STUDIO_CLIP": running.clip_id,
            "HERMES_STUDIO_PROFILE": running.profile,
        }
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            env=environment,
        )
        try:
            store.set_process(
                job.id, "dead-owner", child.pid,
                process_start_time(child.pid) + 1)
            with self.assertLogs("webapp.studio_manager", level="ERROR"):
                StudioJobManager._terminate_orphan_process(store.get_job(job.id))
            self.assertIsNone(child.poll())
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()

    def test_surviving_manager_reaps_peer_after_lease_expires(self):
        settings = replace(
            self.settings, worker_lease_timeout_seconds=1)
        ds.studio_root(str(settings.studio_root))
        first_project = ds.create_project(settings.studio_root, "orphan")
        second_project = ds.create_project(settings.studio_root, "next")
        store = JobStore(settings.database_path)
        store.initialize()
        store.register_worker("dead-owner")
        first_job = store.create_chat_job(
            first_project.name, "orphaned", clip_id="clip-001")
        running = store.claim_next("dead-owner")
        assert running is not None
        environment = {
            **os.environ,
            "HERMES_STUDIO_JOB_ID": running.id,
            "HERMES_STUDIO_PROJECT": running.project,
            "HERMES_STUDIO_CLIP": running.clip_id,
            "HERMES_STUDIO_PROFILE": running.profile,
        }
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            start_new_session=True,
            env=environment,
        )
        store.set_process(
            first_job.id, "dead-owner", child.pid,
            process_start_time(child.pid))

        def command(_job, _session):
            return [sys.executable, "-c", "print('next')"]

        survivor = StudioJobManager(
            settings, JobStore(settings.database_path),
            command_builder=command, cleanup_callback=lambda: None,
        )
        survivor.start()
        second_job = survivor.submit_chat(
            second_project.name, "clip-001", "continue")
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
