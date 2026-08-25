import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from typing import cast

from studio_core.job_contracts import (
    ChatJobPayload,
    ChatScope,
    GenerationJobPayload,
    LegacyGenerationJobPayload,
    JobKind,
    JobEventType,
    JobPhase,
    MovieExportJobPayload,
)
from studio_core.job_store import JobStore, JobStoreError


def generation_request() -> str:
    prompt = "A complete H3 prompt\n"
    prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
    return json.dumps({
        "schema_version": 1,
        "action": "generate-current-prompt",
        "prompt": prompt,
        "prompt_sha256": prompt_sha256,
        "settings_updated_at": "2026-08-25T00:00:00+00:00",
        "settings_manifest": {
            "schema_version": 2,
            "prompt_sha256": prompt_sha256,
            "updated_at": "2026-08-25T00:00:00+00:00",
            "mode": "t2va",
            "aspect": "16:9",
            "mp": 0.4,
            "width": None,
            "height": None,
            "seed": None,
            "steps": 20,
            "accel": False,
        },
        "execution": {
            "resolution": {"width": 1344, "height": 768},
            "timing": {"frames": 121, "fps": 24},
            "references": [],
        },
        "expected_generation_id": "001",
    }, sort_keys=True, separators=(",", ":"))


def movie_contract() -> str:
    return json.dumps({
        "schema_version": 1,
        "action": "export-selected-takes",
        "sources": [{
            "clip_id": "clip-001",
            "clip_title": "Opening",
            "generation": "001",
            "filename": "take.mp4",
            "size": 1,
            "sha256": "0" * 64,
            "probe": {},
        }],
        "assembly": {
            "mode": "stream-copy",
            "hard_cuts": True,
            "target": {},
        },
        "output": {
            "id": "movie-001",
            "filename": "movie.mp4",
            "provenance": "provenance.json",
        },
    }, sort_keys=True, separators=(",", ":"))


class JobContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.temp.name) / "studio.db")
        self.store.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_store_decodes_each_job_kind_to_its_domain_payload(self):
        chat = self.store.create_project_chat_job("project-a", "plan")
        self.assertIs(chat.kind, JobKind.CHAT)
        self.assertIs(chat.chat_scope, ChatScope.PROJECT)
        self.assertIsInstance(chat.payload, ChatJobPayload)
        self.store.fail(chat.id, "done")

        generation = self.store.create_generation_job(
            "project-b", generation_request(), clip_id="clip-001")
        self.assertIs(generation.kind, JobKind.GENERATE)
        self.assertIs(generation.chat_scope, ChatScope.CLIP)
        self.assertIsInstance(generation.payload, GenerationJobPayload)
        self.store.fail(generation.id, "done")

        movie = self.store.create_movie_export_job(
            "project-c", movie_contract())
        self.assertIs(movie.kind, JobKind.EXPORT_MOVIE)
        self.assertIs(movie.chat_scope, ChatScope.PROJECT)
        self.assertIsInstance(movie.payload, MovieExportJobPayload)
        self.assertNotIn("payload", movie.to_dict())

    def test_invalid_kind_scope_and_payloads_fail_at_enqueue(self):
        with self.assertRaisesRegex(JobStoreError, "generation request payload"):
            self.store.create_generation_job(
                "project", "{}", clip_id="clip-001")
        with self.assertRaisesRegex(JobStoreError, "movie export payload"):
            self.store.create_movie_export_job("project", "[]")
        with self.assertRaisesRegex(JobStoreError, "generation jobs require clip"):
            self.store._create_job(
                "project",
                generation_request(),
                "studio",
                clip_id="",
                chat_scope=ChatScope.PROJECT,
                kind=JobKind.GENERATE,
                chat_content="generate",
            )

    def test_invalid_persisted_kind_fails_at_read_boundary(self):
        job = self.store.create_project_chat_job("project", "plan")
        self.store.fail(job.id, "done")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            connection.execute("DROP TRIGGER jobs_validate_contract_on_update")
            connection.execute(
                "UPDATE jobs SET kind = 'unknown' WHERE id = ?", (job.id,))
            connection.commit()
        with self.assertRaisesRegex(JobStoreError, "persisted job.*invalid job kind"):
            self.store.get_job(job.id)

    def test_invalid_persisted_payload_fails_at_read_boundary(self):
        job = self.store.create_generation_job(
            "project", generation_request(), clip_id="clip-001")
        self.store.fail(job.id, "done")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET message = '{}' WHERE id = ?", (job.id,))
            connection.commit()
        with self.assertRaisesRegex(
                JobStoreError, "persisted job.*generation request payload"):
            self.store.get_job(job.id)

    def test_exact_legacy_generation_payload_is_terminal_only(self):
        legacy_payload = json.dumps({
            "action": "generate-current-prompt",
            "prompt_sha256": "a" * 64,
            "settings_updated_at": "2026-08-24T00:00:00+00:00",
        })
        terminal = self.store.create_generation_job(
            "project", generation_request(), clip_id="clip-001")
        self.store.fail(terminal.id, "legacy completed elsewhere")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET message = ? WHERE id = ?",
                (legacy_payload, terminal.id),
            )
            connection.commit()

        loaded = self.store.get_job(terminal.id)
        self.assertIsInstance(loaded.payload, LegacyGenerationJobPayload)
        self.assertEqual(loaded.message, legacy_payload)
        self.assertEqual(self.store.list_jobs("project")[0].id, terminal.id)

        active = self.store.create_generation_job(
            "other", generation_request(), clip_id="clip-001")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            connection.execute(
                "UPDATE jobs SET message = ? WHERE id = ?",
                (legacy_payload, active.id),
            )
            connection.commit()
        with self.assertRaisesRegex(
                JobStoreError, "persisted job.*generation request payload"):
            self.store.get_job(active.id)

    def test_schema_migration_rejects_invalid_contract_updates(self):
        job = self.store.create_project_chat_job("project", "plan")
        self.store.fail(job.id, "done")
        with closing(sqlite3.connect(self.store.database_path)) as connection:
            with self.assertRaisesRegex(
                    sqlite3.IntegrityError, "invalid typed contract"):
                connection.execute(
                    "UPDATE jobs SET kind = 'unknown' WHERE id = ?", (job.id,))

    def test_event_types_and_phases_are_validated_at_the_adapter(self):
        job = self.store.create_project_chat_job("project", "plan")
        self.store.append_job_event(
            job.id,
            "studio",
            JobEventType.COMMENTARY,
            "Working",
            phase=JobPhase.RUNNING,
        )
        _, events = self.store.job_events("project")
        self.assertIs(events[-1].event_type, JobEventType.COMMENTARY)
        self.assertIs(events[-1].phase, JobPhase.RUNNING)
        with self.assertRaisesRegex(JobStoreError, "event type or phase"):
            self.store.append_job_event(
                job.id, "studio", cast(JobEventType, "unknown"), "bad",
                phase=JobPhase.RUNNING)
        with self.assertRaisesRegex(JobStoreError, "event type or phase"):
            self.store.append_job_event(
                job.id, "studio", JobEventType.COMMENTARY, "bad",
                phase=cast(JobPhase, "bad"))


if __name__ == "__main__":
    unittest.main()
