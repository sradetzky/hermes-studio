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
from studio_core.paths import StudioPaths
from webapp.app import create_app
from webapp.config import Settings
from webapp.hermes_events import HermesSessionEventBridge
from webapp.job_store import ActiveJobError, JobStore, JobStoreError
from webapp.models import JobStatus
from webapp.runtime_schema import CURRENT_SCHEMA_VERSION, LEGACY_CLIP_ERROR
from webapp import runtime_schema, safe_files
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

    def submit_project_chat(self, project, message, profile=None):
        return self.store.create_project_chat_job(
            project, message, profile or "studio")

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

class JobStoreTests(WebAppTestCase):
    def store(self):
        store = JobStore(self.settings.database_path)
        store.initialize()
        return store

    def test_runtime_database_permissions_are_private(self):
        self.store()
        self.assertEqual(
            self.settings.runtime_root.stat().st_mode & 0o777, 0o700)
        self.assertEqual(
            self.settings.database_path.stat().st_mode & 0o777, 0o600)

    def test_historical_unbound_active_job_fails_closed_during_migration(self):
        store = self.store()
        historical = store.create_chat_job(
            "project", "message", clip_id="clip-001")
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.execute("DROP TRIGGER jobs_validate_contract_on_insert")
            connection.execute("DROP TRIGGER jobs_validate_contract_on_update")
            connection.execute("ALTER TABLE jobs DROP COLUMN clip_id")
            connection.execute("ALTER TABLE jobs DROP COLUMN chat_scope")
            connection.execute("PRAGMA user_version = 0")
            connection.commit()

        store.initialize()
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(jobs)").fetchall()
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("clip_id", columns)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        migrated = store.get_job(historical.id)
        self.assertEqual(migrated.clip_id, "")
        self.assertEqual(migrated.status, JobStatus.FAILED)
        self.assertEqual(migrated.error, LEGACY_CLIP_ERROR)
        self.assertEqual(migrated.chat_scope, "project")
        self.assertEqual(store.active_jobs(), [])

    def test_scope_migration_preserves_legacy_chat_and_session_as_project_history(self):
        self.settings.runtime_root.mkdir()
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.executescript("""
                CREATE TABLE jobs (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    clip_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'studio',
                    status TEXT NOT NULL,
                    message TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    owner_id TEXT NOT NULL DEFAULT '',
                    pid INTEGER,
                    pid_start_time INTEGER
                );
                CREATE TABLE profile_sessions (
                    project TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(project, profile)
                );
                CREATE TABLE chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    job_id TEXT,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, role)
                );
                CREATE TABLE job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    job_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );
                INSERT INTO profile_sessions VALUES (
                    'project', 'studio', 'legacy-session', '2026-08-24T00:00:00Z');
                INSERT INTO jobs VALUES (
                    'legacy-job', 'project', 'clip-001', 'chat', 'studio',
                    'completed', 'legacy history', '',
                    '2026-08-24T00:00:00Z', '2026-08-24T00:00:01Z',
                    '2026-08-24T00:00:02Z', '', NULL, NULL);
                INSERT INTO chat_events (
                    project, job_id, role, content, created_at
                ) VALUES (
                    'project', 'legacy-job', 'user', 'legacy history',
                    '2026-08-24T00:00:00Z'
                );
                INSERT INTO job_events (
                    project, job_id, profile, event_type, status, summary,
                    detail, created_at
                ) VALUES (
                    'project', 'legacy-job', 'studio', 'job.completed',
                    'completed', 'legacy complete', '{}',
                    '2026-08-24T00:00:02Z'
                );
                PRAGMA user_version = 3;
            """)

        store = JobStore(self.settings.database_path)
        store.initialize()

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            session_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(profile_sessions)").fetchall()
            }
            chat_columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(chat_events)").fetchall()
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertIn("clip_id", session_columns)
        self.assertIn("clip_id", chat_columns)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(store.get_session("project", "studio", clip_id=""),
                         "legacy-session")
        self.assertIsNone(
            store.get_session("project", "studio", clip_id="clip-001"))
        _, project_chat = store.chat_events("project", clip_id="")
        _, clip_chat = store.chat_events("project", clip_id="clip-001")
        self.assertEqual([event.content for event in project_chat], ["legacy history"])
        self.assertEqual(clip_chat, [])
        migrated_job = store.get_job("legacy-job")
        self.assertEqual(migrated_job.clip_id, "clip-001")
        self.assertEqual(migrated_job.chat_scope, "project")
        self.assertEqual(
            [job.id for job in store.list_jobs("project", clip_id="")],
            ["legacy-job"],
        )
        self.assertEqual(store.list_jobs("project", clip_id="clip-001"), [])
        _, project_activity = store.job_events("project", clip_id="")
        _, clip_activity = store.job_events("project", clip_id="clip-001")
        self.assertEqual(
            [event.summary for event in project_activity], ["legacy complete"])
        self.assertEqual(clip_activity, [])

    def test_active_jobs_require_a_valid_exact_clip(self):
        store = self.store()
        for clip_id in ("", "clip-1", "../clip-001", "clip-001/other"):
            with self.subTest(clip_id=clip_id), self.assertRaises(JobStoreError):
                store.create_chat_job(
                    "project", "message", clip_id=clip_id)

        job = store.create_chat_job(
            "project", "message", clip_id="clip-001")
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET status = 'failed' WHERE id = ?", (job.id,))
            with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "invalid typed contract"):
                connection.execute(
                    "UPDATE jobs SET status = 'queued', clip_id = '', "
                    "kind = 'generate' WHERE id = ?",
                    (job.id,),
                )

    def test_runtime_schema_rejects_newer_database_versions(self):
        self.settings.runtime_root.mkdir()
        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            connection.execute(
                f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION + 1}")
        with self.assertRaisesRegex(JobStoreError, "newer than supported"):
            JobStore(self.settings.database_path).initialize()

    def test_runtime_schema_migration_rolls_back_failed_version(self):
        def fail_migration(connection):
            connection.execute("CREATE TABLE migration_must_rollback (id INTEGER)")
            raise RuntimeError("injected migration failure")

        migrations = (*runtime_schema.MIGRATIONS[:2], fail_migration)
        with patch.object(runtime_schema, "MIGRATIONS", migrations), \
                self.assertRaisesRegex(RuntimeError, "injected migration failure"):
            JobStore(self.settings.database_path).initialize()

        with closing(sqlite3.connect(self.settings.database_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            partial = connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'migration_must_rollback'"
            ).fetchone()
        self.assertEqual(version, 2)
        self.assertIsNone(partial)

    def test_runtime_database_rejects_symlinked_paths(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        self.settings.runtime_root.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(JobStoreError, "runtime directory"):
            self.store()

        self.settings.runtime_root.unlink()
        self.settings.runtime_root.mkdir()
        outside_database = outside / "outside.db"
        outside_database.touch()
        self.settings.database_path.symlink_to(outside_database)
        with self.assertRaisesRegex(JobStoreError, "database files"):
            self.store()

    def test_profile_sessions_are_isolated_per_project(self):
        store = self.store()
        first = store.create_chat_job(
            "project", "plan", profile="studio-storyboarder",
            clip_id="clip-001")
        store.claim(first.id, "worker")
        store.complete(first.id, "worker", "planned", "story-session")
        second = store.create_chat_job(
            "project", "prompt", profile="studio-prompt-engineer",
            clip_id="clip-001")
        store.claim(second.id, "worker")
        store.complete(second.id, "worker", "prompted", "prompt-session")

        self.assertEqual(
            store.get_session(
                "project", "studio-storyboarder", clip_id="clip-001"),
            "story-session",
        )
        self.assertEqual(
            store.get_session(
                "project", "studio-prompt-engineer", clip_id="clip-001"),
            "prompt-session",
        )

    def test_project_and_clip_chat_sessions_are_isolated(self):
        store = self.store()
        project_job = store.create_project_chat_job("project", "plan", "studio")
        self.assertEqual(project_job.chat_scope, "project")
        store.claim(project_job.id, "worker")
        store.complete(
            project_job.id, "worker", "project reply", "project-session")
        clip_job = store.create_chat_job(
            "project", "refine", "studio", clip_id="clip-001")
        self.assertEqual(clip_job.chat_scope, "clip")
        store.claim(clip_job.id, "worker")
        store.complete(clip_job.id, "worker", "clip reply", "clip-session")

        self.assertEqual(
            store.get_session("project", "studio", clip_id=""),
            "project-session")
        self.assertEqual(
            store.get_session("project", "studio", clip_id="clip-001"),
            "clip-session")
        _, project_chat = store.chat_events("project", clip_id="")
        _, clip_chat = store.chat_events("project", clip_id="clip-001")
        self.assertEqual(
            [event.content for event in project_chat], ["plan", "project reply"])
        self.assertEqual(
            [event.content for event in clip_chat], ["refine", "clip reply"])

    def test_external_user_append_dedupes_active_web_turn(self):
        store = self.store()
        store.create_chat_job(
            "project", "same message", clip_id="clip-001")
        store.append_external_event("project", "user", "same message")
        cursor, events = store.chat_events("project")
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].content, "same message")

    def test_job_event_cursor_only_advances_through_returned_events(self):
        store = self.store()
        job = store.create_chat_job(
            "project", "message", clip_id="clip-001")
        cursor, initial = store.job_events("project")
        self.assertEqual(len(initial), 1)
        store.append_job_event(
            job.id, "studio", "commentary", "Still working", phase="running")
        next_cursor, added = store.job_events("project", cursor)
        self.assertEqual([event.summary for event in added], ["Still working"])
        self.assertGreater(next_cursor, cursor)

    def test_chat_cursor_uses_delivered_row_ids_not_project_counts(self):
        store = self.store()
        store.append_external_event("project", "user", "first")
        store.append_external_event("other", "user", "unrelated")
        store.append_external_event("project", "assistant", "second")

        cursor, events = store.chat_events("project")
        self.assertEqual([event.id for event in events], [1, 3])
        self.assertEqual(cursor, 3)

        unchanged, no_events = store.chat_events("project", cursor)
        self.assertEqual(unchanged, cursor)
        self.assertEqual(no_events, [])

        store.append_external_event("project", "system", "third")
        next_cursor, added = store.chat_events("project", cursor)
        self.assertEqual([event.content for event in added], ["third"])
        self.assertEqual(next_cursor, added[-1].id)

    def test_hermes_session_bridge_projects_reasoning_and_tools(self):
        store = self.store()
        job = store.create_chat_job(
            "project", "inspect", clip_id="clip-001")
        home = Path(self.temp.name) / "hermes"
        state = home / "profiles" / "studio" / "state.db"
        state.parent.mkdir(parents=True)
        started_at = time.time()
        with closing(sqlite3.connect(state)) as connection:
            connection.executescript(
                """
                CREATE TABLE sessions (
                    id TEXT PRIMARY KEY, source TEXT, started_at REAL
                );
                CREATE TABLE messages (
                    id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                    content TEXT, tool_call_id TEXT, tool_name TEXT, tool_calls TEXT,
                    reasoning TEXT, reasoning_content TEXT, timestamp REAL
                );
                """
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("session-1", f"studio-web:{job.id}", started_at),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    1, "session-1", "assistant", "", None, None,
                    json.dumps([
                        {"id": "call-a", "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/a"}),
                        }},
                        {"id": "call-b", "function": {
                            "name": "read_file",
                            "arguments": json.dumps({"path": "/tmp/b"}),
                        }},
                    ]),
                    "Inspecting the project", None, started_at + 1,
                ),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    2, "session-1", "tool", json.dumps({"success": True}),
                    "call-b", "read_file", None, None, None, started_at + 2,
                ),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    3, "session-1", "tool", json.dumps({"success": True}),
                    "call-a", "read_file", None, None, None, started_at + 3,
                ),
            )
            connection.commit()

        with patch.dict(os.environ, {"HERMES_HOME": str(home)}):
            settings = replace(
                self.settings, runtime_paths=StudioPaths.from_environment())
            bridge = HermesSessionEventBridge(
                store, settings, job, f"studio-web:{job.id}", started_at)
            bridge.poll()

        _, events = store.job_events("project")
        event_types = [event.event_type for event in events]
        self.assertIn("profile.connected", event_types)
        self.assertIn("reasoning", event_types)
        self.assertIn("tool.started", event_types)
        self.assertIn("tool.completed", event_types)
        completed = [
            event for event in events if event.event_type == "tool.completed"]
        self.assertEqual(completed[0].detail["duration"], 1.0)
        self.assertEqual(
            [event.detail["arguments"]["path"] for event in completed],
            ["/tmp/b", "/tmp/a"],
        )

    def test_hermes_session_bridge_requires_exact_job_source(self):
        store = self.store()
        job = store.create_chat_job(
            "project", "inspect", clip_id="clip-001")
        state = Path(self.temp.name) / "exact-source-state.db"
        state.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state)) as connection:
            connection.executescript(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
                "started_at REAL);"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                "role TEXT, content TEXT, tool_call_id TEXT, tool_name TEXT, "
                "tool_calls TEXT, reasoning TEXT, reasoning_content TEXT, "
                "timestamp REAL);"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("previous", "studio-web", time.time()),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "previous", "assistant", "", None, None, None,
                 "old reasoning", None, time.time()),
            )
            connection.commit()
        bridge = HermesSessionEventBridge(
            store, self.settings, job, f"studio-web:{job.id}", time.time())
        bridge.database_path = state
        bridge.poll()
        self.assertIsNone(bridge.session_id)
        _, events = store.job_events("project")
        self.assertNotIn("reasoning", [event.event_type for event in events])

    def test_hermes_session_bridge_retries_baseline_without_replaying_history(self):
        store = self.store()
        job = store.create_chat_job(
            "project", "inspect", clip_id="clip-001")
        state = Path(self.temp.name) / "baseline-state.db"
        state.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(state)) as connection:
            connection.executescript(
                "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, "
                "started_at REAL);"
                "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
                "role TEXT, content TEXT, tool_call_id TEXT, tool_name TEXT, "
                "tool_calls TEXT, reasoning TEXT, reasoning_content TEXT, "
                "timestamp REAL);"
            )
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                ("resumed", "old-source", time.time()),
            )
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (1, "resumed", "assistant", "", None, None, None,
                 "old reasoning", None, time.time()),
            )
            connection.commit()
        bridge = HermesSessionEventBridge(
            store, self.settings, job, f"studio-web:{job.id}", time.time(),
            session_id="resumed")
        bridge.database_path = state
        real_connection = bridge._connection
        calls = 0

        def flaky_connection():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("temporarily locked")
            return real_connection()

        bridge._connection = flaky_connection
        self.assertFalse(bridge.prepare())
        bridge.poll()
        with closing(sqlite3.connect(state)) as connection:
            connection.execute(
                "INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (2, "resumed", "assistant", "", None, None, None,
                 "new reasoning", None, time.time()),
            )
            connection.commit()
        bridge.poll()
        _, events = store.job_events("project")
        reasoning = [
            event.detail["text"] for event in events
            if event.event_type == "reasoning"]
        self.assertEqual(reasoning, ["new reasoning"])

    def test_cross_connection_active_job_claim_is_atomic(self):
        first_store = self.store()
        second_store = JobStore(self.settings.database_path)
        barrier = Barrier(2)

        def create(store):
            barrier.wait()
            try:
                return store.create_chat_job(
                    "project", "message", clip_id="clip-001").id
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
        first = store.create_chat_job(
            "one", "first", clip_id="clip-001")
        second = store.create_chat_job(
            "two", "second", clip_id="clip-001")
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
        job = store.create_chat_job(
            "project", "question", clip_id="clip-001")
        store.claim_next("worker")
        store.complete(job.id, "worker", "answer", "session-1")
        cursor, events = store.chat_events("project", clip_id="clip-001")
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 2)
        self.assertEqual(
            [(event.role, event.content) for event in events],
            [("user", "question"), ("assistant", "answer")],
        )
        self.assertEqual(
            store.get_session("project", clip_id="clip-001"), "session-1")
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
        with self.assertLogs("studio_core.job_store", level="WARNING"):
            store.import_chat_if_empty("project", chat)
        store.import_chat_if_empty("project", chat)
        cursor, events = store.chat_events("project")
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 2)
        self.assertEqual([event.content for event in events], ["one", "two"])
