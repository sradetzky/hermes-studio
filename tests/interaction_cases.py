import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from studio_core.interaction_store import (
    InteractionConflictError,
    InteractionNotFoundError,
    InteractionStore,
    InteractionStoreError,
)
from studio_core.interactions import (
    InteractionContractError,
    gateway_answer,
    parse_gateway_clarify,
    validate_interaction_answers,
)
from studio_core.job_contracts import ChatScope
from studio_core.job_store import JobStore
from tests.job_store_cases import WebAppTestCase


class InteractionContractTests(unittest.TestCase):
    def test_single_multi_free_text_and_batch_contracts(self):
        request_id, single = parse_gateway_clarify({
            "request_id": "request-1",
            "question": "Which cut?",
            "choices": ["A", "B"],
            "multi_select": False,
        })
        self.assertEqual(request_id, "request-1")
        self.assertFalse(single.batch)
        self.assertEqual(
            validate_interaction_answers(single, {"q0": "A"}),
            {"q0": "A"},
        )

        _, multi = parse_gateway_clarify({
            "request_id": "request-2",
            "question": "Which traits?",
            "choices": ["Warm", "Fast"],
            "multi_select": True,
        })
        self.assertEqual(
            validate_interaction_answers(multi, {"q0": ["Warm", "Custom"]}),
            {"q0": ["Warm", "Custom"]},
        )
        with self.assertRaises(InteractionContractError):
            validate_interaction_answers(multi, {"q0": ["Custom", "Another"]})
        self.assertEqual(gateway_answer(["Warm", "Custom"]), '["Warm","Custom"]')

        _, free = parse_gateway_clarify({
            "request_id": "request-3",
            "question": "Describe the ending",
            "choices": [],
            "multi_select": False,
        })
        self.assertEqual(
            validate_interaction_answers(free, {"q0": "Hard cut to black"}),
            {"q0": "Hard cut to black"},
        )

        _, batch = parse_gateway_clarify({
            "request_id": "request-4",
            "questions": [
                {
                    "qid": "tone",
                    "question": "Tone?",
                    "choices": ["Cold", "Warm"],
                    "multi_select": False,
                },
                {
                    "qid": "notes",
                    "question": "Any notes?",
                    "choices": [],
                    "multi_select": False,
                },
            ],
        })
        self.assertTrue(batch.batch)
        self.assertEqual(
            validate_interaction_answers(
                batch, {"tone": "Cold", "notes": "No music"}),
            {"tone": "Cold", "notes": "No music"},
        )
        with self.assertRaises(InteractionContractError):
            validate_interaction_answers(batch, {"tone": "Cold"})

    def test_malformed_gateway_contracts_fail_closed(self):
        malformed = [
            {},
            {"request_id": "bad id", "question": "Question?"},
            {"request_id": "ok", "question": "", "choices": []},
            {
                "request_id": "ok",
                "questions": [{"qid": "q", "question": "Question?",
                               "choices": "not-a-list"}],
            },
        ]
        for payload in malformed:
            with self.subTest(payload=payload), self.assertRaises(
                    InteractionContractError):
                parse_gateway_clarify(payload)


class InteractionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "runtime.sqlite3"
        self.jobs = JobStore(self.database)
        self.jobs.initialize()
        self.interactions = InteractionStore(self.database)
        self.job = self.jobs.create_chat_job(
            "project", "Need direction", clip_id="clip-001")
        self.jobs.claim(self.job.id, "worker")

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def payload(request_id="request-1"):
        return {
            "request_id": request_id,
            "question": "Which direction?",
            "choices": ["Left", "Right"],
            "multi_select": False,
        }

    def test_request_is_durable_scoped_and_replay_safe(self):
        request = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        replay = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        self.assertEqual(request, replay)
        self.assertEqual(
            self.interactions.open_for_scope(
                "project", ChatScope.CLIP, "clip-001"), request)
        self.assertIsNone(self.interactions.open_for_scope(
            "project", ChatScope.PROJECT))
        events = self.jobs.job_events("project", clip_id="clip-001")[1]
        self.assertEqual(events[-1].event_type, "interaction.requested")
        self.assertEqual(events[-1].status, "waiting_for_user")

        with self.assertRaises(InteractionConflictError):
            self.interactions.create(
                self.job.id, "other-session", self.payload())
        with self.assertRaises(InteractionConflictError):
            self.interactions.create(
                self.job.id, "hermes-session", self.payload("request-2"))

    def test_answer_is_atomic_exact_scope_and_exact_revision(self):
        request = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        with self.assertRaises(InteractionNotFoundError):
            self.interactions.answer(
                request.id, request.revision, "project",
                ChatScope.PROJECT, "", {"q0": "Left"})
        with self.assertRaises(InteractionStoreError):
            self.interactions.answer(
                request.id, request.revision, "project",
                ChatScope.CLIP, "clip-001", {})

        def answer():
            try:
                return self.interactions.answer(
                    request.id, request.revision, "project",
                    ChatScope.CLIP, "clip-001", {"q0": "Left"}).status.value
            except InteractionConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = sorted(pool.map(lambda _index: answer(), range(2)))
        self.assertEqual(outcomes, ["answered", "conflict"])
        answered = self.interactions.answered(request.id, self.job.id)
        self.assertIsNotNone(answered)
        assert answered is not None
        self.assertEqual(answered.answers, {"q0": "Left"})
        with self.assertRaises(InteractionConflictError):
            self.interactions.answer(
                request.id, request.revision, "project",
                ChatScope.CLIP, "clip-001", {"q0": "Right"})
        resolved = self.interactions.resolve(request.id, self.job.id)
        self.assertEqual(resolved.status.value, "resolved")
        self.assertIsNone(self.interactions.open_for_scope(
            "project", ChatScope.CLIP, "clip-001"))

    def test_terminal_job_closes_pending_interaction(self):
        request = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        self.jobs.fail(self.job.id, "cancelled")
        self.assertEqual(self.interactions.get(request.id).status.value, "closed")
        with self.assertRaises(InteractionConflictError):
            self.interactions.answer(
                request.id, request.revision, "project",
                ChatScope.CLIP, "clip-001", {"q0": "Left"})

    def test_gateway_expiry_closes_pending_or_accepted_answer(self):
        pending = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        expired = self.interactions.expire(
            pending.id, self.job.id, pending.hermes_request_id)
        self.assertEqual(expired.status.value, "closed")
        self.assertIsNone(self.interactions.open_for_scope(
            "project", ChatScope.CLIP, "clip-001"))
        with self.assertRaises(InteractionConflictError):
            self.interactions.answer(
                pending.id, pending.revision, "project",
                ChatScope.CLIP, "clip-001", {"q0": "Left"})

        self.jobs.fail(self.job.id, "expired")
        second_job = self.jobs.create_chat_job(
            "project", "Need another direction", clip_id="clip-001")
        self.jobs.claim(second_job.id, "worker")
        answered = self.interactions.create(
            second_job.id, "hermes-session-2", self.payload("request-2"))
        answered = self.interactions.answer(
            answered.id, answered.revision, "project",
            ChatScope.CLIP, "clip-001", {"q0": "Right"})
        closed = self.interactions.expire(
            answered.id, second_job.id, answered.hermes_request_id)
        self.assertEqual(closed.status.value, "closed")
        self.assertEqual(closed.answers, {"q0": "Right"})
        events = self.jobs.job_events("project", clip_id="clip-001")[1]
        self.assertEqual(events[-1].event_type, "interaction.expired")
        self.assertEqual(events[-1].status, "failed")

    def test_schema_7_migrates_and_malformed_payload_fails_closed(self):
        request = self.interactions.create(
            self.job.id, "hermes-session", self.payload())
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE interaction_requests SET payload = '{}' WHERE id = ?",
                (request.id,),
            )
        with self.assertRaisesRegex(
                InteractionStoreError, "persisted interaction"):
            self.interactions.get(request.id)

        migrated_database = Path(self.temp.name) / "migration" / "runtime.sqlite3"
        migrated_jobs = JobStore(migrated_database)
        migrated_jobs.initialize()
        with sqlite3.connect(migrated_database) as connection:
            connection.execute("DROP TABLE interaction_requests")
            connection.execute("PRAGMA user_version = 6")
        migrated_jobs.initialize()
        with sqlite3.connect(migrated_database) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(interaction_requests)")
            }
        self.assertEqual(version, 7)
        self.assertIn("hermes_request_id", columns)


class InteractionRouteTests(WebAppTestCase):
    def test_pending_interaction_survives_reload_and_answers_exactly_once(self):
        with TestClient(self.app()) as client:
            project = self.create_project(client, "clarify-route")
            clip = client.post(
                f"/api/project/{project}/clips", json={"title": "First"})
            self.assertEqual(clip.status_code, 201)
            clip_id = clip.json()["clip"]["id"]
            response = client.post(
                f"/api/project/{project}/clips/{clip_id}/chat",
                json={"message": "Plan this", "profile": "studio"},
            )
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["id"]
            store = JobStore(self.settings.database_path)
            store.claim(job_id, "worker")
            interactions = InteractionStore(self.settings.database_path)
            request = interactions.create(job_id, "session-1", {
                "request_id": "route-request",
                "questions": [
                    {"qid": "pick", "question": "Pick one",
                     "choices": ["One", "Two"], "multi_select": False},
                    {"qid": "notes", "question": "Notes?",
                     "choices": [], "multi_select": False},
                ],
            })

            path = f"/api/project/{project}/clips/{clip_id}/interaction"
            first = client.get(path)
            second = client.get(path)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.json(), second.json())
            self.assertEqual(first.json()["interaction"]["id"], request.id)
            self.assertEqual(
                client.get(f"/api/project/{project}/interaction").json(),
                {"interaction": None},
            )

            answer = client.post(
                f"{path}/{request.id}",
                json={
                    "revision": request.revision,
                    "answers": {"pick": "One", "notes": "No music"},
                },
            )
            self.assertEqual(answer.status_code, 200)
            self.assertEqual(answer.json()["interaction"]["status"], "answered")
            duplicate = client.post(
                f"{path}/{request.id}",
                json={
                    "revision": request.revision,
                    "answers": {"pick": "Two", "notes": "Changed"},
                },
            )
            self.assertEqual(duplicate.status_code, 409)
            wrong_scope = client.post(
                f"/api/project/{project}/interaction/{request.id}",
                json={
                    "revision": request.revision,
                    "answers": {"pick": "One", "notes": "No music"},
                },
            )
            self.assertEqual(wrong_scope.status_code, 404)
