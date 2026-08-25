import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from studio_core.interaction_store import InteractionStore
from studio_core.job_contracts import ChatScope
from studio_core.job_store import JobStore


FAKE_GATEWAY = r'''
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


send({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}})
responses = []
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "session.create":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "session_id": "live-session", "stored_session_id": "stored-session"}})
    elif method == "session.resume":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "session_id": "live-session", "session_key": "stored-session",
            "resumed": "stored-session"}})
    elif method == "prompt.submit":
        send({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "clarify.request", "session_id": "live-session", "payload": {
                "request_id": "clarify-1", "questions": [
                    {"qid": "pick", "question": "Pick one", "choices": ["One", "Two"],
                     "multi_select": False},
                    {"qid": "traits", "question": "Pick traits", "choices": ["Warm", "Fast"],
                     "multi_select": True},
                    {"qid": "notes", "question": "Notes?", "choices": [],
                     "multi_select": False}
                ]}}})
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}})
    elif method == "clarify.respond":
        responses.append(request["params"])
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "status": "ok", "remaining": 3 - len(responses)}})
        if len(responses) == 3:
            expected = [
                {"request_id": "clarify-1", "question_id": "pick", "answer": "One"},
                {"request_id": "clarify-1", "question_id": "traits",
                 "answer": "[\"Warm\",\"Custom\"]"},
                {"request_id": "clarify-1", "question_id": "notes", "answer": "No music"},
            ]
            if responses != expected:
                send({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": "error", "payload": {"message": "wrong clarify answers"}}})
            else:
                send({"jsonrpc": "2.0", "method": "event", "params": {
                    "type": "message.complete", "session_id": "live-session",
                    "payload": {"text": "Continued the same run."}}})
    elif method == "session.close":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"closed": True}})
'''


EXPIRING_GATEWAY = r'''
import json
import sys


def send(value):
    print(json.dumps(value), flush=True)


send({"jsonrpc": "2.0", "method": "event", "params": {"type": "gateway.ready"}})
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "session.create":
        send({"jsonrpc": "2.0", "id": request["id"], "result": {
            "session_id": "live-session", "stored_session_id": "stored-session"}})
    elif method == "prompt.submit":
        send({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "clarify.request", "session_id": "live-session", "payload": {
                "request_id": "clarify-expired", "question": "Too late?",
                "choices": ["Yes", "No"]}}})
        send({"jsonrpc": "2.0", "id": request["id"], "result": {"accepted": True}})
        send({"jsonrpc": "2.0", "method": "event", "params": {
            "type": "clarify.expire", "session_id": "live-session",
            "payload": {"request_id": "clarify-expired"}}})
'''


class HermesStudioAgentTests(unittest.TestCase):
    def test_gateway_clarify_waits_for_durable_answer_and_resumes_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "tui_gateway"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "entry.py").write_text(FAKE_GATEWAY)
            profile_home = root / "profile"
            profile_home.mkdir()
            (profile_home / "config.yaml").write_text(
                "agent:\n  clarify_timeout: 0\n")
            database = root / "runtime.sqlite3"
            jobs = JobStore(database)
            jobs.initialize()
            job = jobs.create_chat_job(
                "project", "ask me", profile="studio", clip_id="clip-001")
            jobs.claim(job.id, "worker")
            interactions = InteractionStore(database)
            command = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent /
                    "scripts" / "hermes_studio_agent.py"),
                "--gateway-python", sys.executable,
                "--database", str(database),
                "--job-id", job.id,
                "--profile", "studio",
                "--profile-home", str(profile_home),
                "--project", "project",
                "--clip-id", "clip-001",
                "--chat-scope", "clip",
                "--session-id", "stored-session",
                "--source", f"studio-web:{job.id}",
                "--cwd", str(root),
                "--toolsets", "file,terminal,skills,clarify",
                "--prompt", "Ask and continue",
            ]
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            request = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                request = interactions.open_for_scope(
                    "project", ChatScope.CLIP, "clip-001")
                if request:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            if request is None and process.poll() is not None:
                stdout, stderr = process.communicate()
                self.fail(
                    f"gateway worker exited early ({process.returncode}): "
                    f"stdout={stdout!r} stderr={stderr!r}")
            self.assertIsNotNone(request)
            assert request is not None
            self.assertEqual(request.hermes_session_id, "stored-session")
            self.assertEqual([question.id for question in request.payload.questions],
                             ["pick", "traits", "notes"])
            interactions.answer(
                request.id, request.revision, "project", ChatScope.CLIP,
                "clip-001", {
                    "pick": "One",
                    "traits": ["Warm", "Custom"],
                    "notes": "No music",
                })
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), "Continued the same run.")
            self.assertIn("session_id: stored-session", stderr)
            resolved = interactions.get(request.id)
            self.assertEqual(resolved.status.value, "resolved")
            events = jobs.job_events("project", clip_id="clip-001")[1]
            self.assertEqual(
                [event.event_type for event in events[-3:]],
                [
                    "interaction.requested",
                    "interaction.answered",
                    "interaction.resumed",
                ],
            )
            self.assertEqual(events[-3].status, "waiting_for_user")
            self.assertEqual(events[-1].status, "running")

    def test_gateway_expiry_closes_interaction_and_fails_the_same_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "tui_gateway"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "entry.py").write_text(EXPIRING_GATEWAY)
            profile_home = root / "profile"
            profile_home.mkdir()
            (profile_home / "config.yaml").write_text(
                "agent:\n  clarify_timeout: 0\n")
            database = root / "runtime.sqlite3"
            jobs = JobStore(database)
            jobs.initialize()
            job = jobs.create_chat_job(
                "project", "ask me", profile="studio", clip_id="clip-001")
            jobs.claim(job.id, "worker")
            interactions = InteractionStore(database)
            command = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent /
                    "scripts" / "hermes_studio_agent.py"),
                "--gateway-python", sys.executable,
                "--database", str(database),
                "--job-id", job.id,
                "--profile", "studio",
                "--profile-home", str(profile_home),
                "--project", "project",
                "--clip-id", "clip-001",
                "--chat-scope", "clip",
                "--source", f"studio-web:{job.id}",
                "--cwd", str(root),
                "--toolsets", "file,terminal,skills,clarify",
                "--prompt", "Ask and expire",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 1, process.stderr)
            self.assertIn("clarification expired", process.stderr.lower())
            self.assertIsNone(interactions.open_for_scope(
                "project", ChatScope.CLIP, "clip-001"))
            with interactions._connection() as connection:
                row = connection.execute(
                    "SELECT status FROM interaction_requests WHERE job_id = ?",
                    (job.id,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "closed")

    def test_late_gateway_response_is_closed_instead_of_resumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            database = root / "studio.db"
            jobs = JobStore(database)
            jobs.initialize()
            job = jobs.create_chat_job(
                "project", "Ask and continue", profile="studio", clip_id="clip-001")
            jobs.claim(job.id, "worker")
            gateway_source = (
                FAKE_GATEWAY.replace(
                    '"status": "ok", "remaining": 3 - len(responses)',
                    '"status": "expired", "remaining": 3 - len(responses)',
                )
            )
            profile = root / "profile"
            profile.mkdir()
            (profile / "config.yaml").write_text(
                "agent:\n  clarify_timeout: 0\n")
            package = root / "tui_gateway"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "entry.py").write_text(gateway_source)
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent.parent /
                        "scripts" / "hermes_studio_agent.py"),
                    "--gateway-python", sys.executable,
                    "--database", str(database),
                    "--job-id", job.id,
                    "--profile", "studio",
                    "--profile-home", str(profile),
                    "--project", "project",
                    "--clip-id", "clip-001",
                    "--chat-scope", "clip",
                    "--source", f"studio-web:{job.id}",
                    "--cwd", str(root),
                    "--toolsets", "file,terminal,skills,clarify",
                    "--prompt", "Ask and continue",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            store = InteractionStore(database)
            interaction = None
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                interaction = store.open_for_scope(
                    "project", ChatScope.CLIP, "clip-001")
                if interaction:
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.05)
            self.assertIsNotNone(interaction)
            assert interaction is not None
            store.answer(
                interaction.id,
                interaction.revision,
                "project",
                ChatScope.CLIP,
                "clip-001",
                {
                    "pick": "One",
                    "traits": ["Warm", "Custom"],
                    "notes": "No music",
                },
            )
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(
                "Hermes was no longer waiting for this clarification", stderr)
            closed = store.get(interaction.id)
            self.assertIsNotNone(closed)
            self.assertEqual(closed.status.value, "closed")

    def test_gateway_worker_requires_unlimited_profile_clarify_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "tui_gateway"
            package.mkdir()
            (package / "__init__.py").write_text("")
            (package / "entry.py").write_text(FAKE_GATEWAY)
            profile_home = root / "profile"
            profile_home.mkdir()
            (profile_home / "config.yaml").write_text(
                "agent:\n  clarify_timeout: 600\n")
            database = root / "runtime.sqlite3"
            jobs = JobStore(database)
            jobs.initialize()
            job = jobs.create_chat_job(
                "project", "ask me", profile="studio", clip_id="clip-001")
            jobs.claim(job.id, "worker")
            command = [
                sys.executable,
                str(Path(__file__).resolve().parent.parent /
                    "scripts" / "hermes_studio_agent.py"),
                "--gateway-python", sys.executable,
                "--database", str(database),
                "--job-id", job.id,
                "--profile", "studio",
                "--profile-home", str(profile_home),
                "--project", "project",
                "--clip-id", "clip-001",
                "--chat-scope", "clip",
                "--source", f"studio-web:{job.id}",
                "--cwd", str(root),
                "--toolsets", "file,terminal,skills,clarify",
                "--prompt", "Reject bounded clarify",
            ]
            process = subprocess.run(
                command, capture_output=True, text=True, timeout=10)
            self.assertEqual(process.returncode, 1)
            self.assertIn("clarify_timeout must be unlimited", process.stderr)
