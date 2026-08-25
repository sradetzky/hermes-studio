import os
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace

from scripts import design_studio as ds
from studio_core.job_store import JobStore
from studio_core.models import JobStatus
from webapp.process_runner import SupervisedProcessRunner, process_start_time
from webapp.studio_manager import StudioJobManager
from tests.webapp_test_support import WebAppTestCase


class StudioRecoveryTests(WebAppTestCase):
    def wait_for_terminal(self, store, job_id, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = store.get_job(job_id)
            if job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                return job
            time.sleep(0.05)
        self.fail("job did not reach a terminal state")

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
            with self.assertLogs("webapp.process_runner", level="ERROR"):
                SupervisedProcessRunner.terminate_orphan_process(
                    store.get_job(job.id))
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
            with self.assertLogs("webapp.process_runner", level="ERROR"):
                SupervisedProcessRunner.terminate_orphan_process(
                    store.get_job(job.id))
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
