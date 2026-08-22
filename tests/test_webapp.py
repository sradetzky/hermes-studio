import asyncio
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from webapp import app as webapp


class JsonlReaderTests(unittest.TestCase):
    def test_corrupt_record_is_logged_not_silent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chat.jsonl"
            path.write_text(
                json.dumps({"role": "user", "content": "one"}) +
                "\n{broken\n" +
                json.dumps({"role": "assistant", "content": "two"}) + "\n",
                encoding="utf-8",
            )
            with self.assertLogs(webapp.log, level="WARNING") as captured:
                records = webapp.read_jsonl(path)
            self.assertEqual([r["content"] for r in records], ["one", "two"])
            self.assertIn(f"{path}:2", "\n".join(captured.output))


class AsyncJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = webapp.ds.studio_root(self.temp.name)
        self.project = webapp.ds.create_project(self.root, "web-job")
        self.jobs = Path(self.temp.name) / "jobs"
        self.sessions = Path(self.temp.name) / "sessions"
        self.jobs.mkdir()
        self.sessions.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_worker_persists_completed_job(self):
        job = {
            "id": "a" * 32, "project": self.project.name, "kind": "chat",
            "status": "queued", "message": "hello", "reply": "",
            "error": "", "created_at": webapp.utc_now(),
            "started_at": "", "finished_at": "",
        }
        with (
            patch.object(webapp, "STUDIO_ROOT", self.root),
            patch.object(webapp, "JOBS", self.jobs),
            patch.object(webapp, "SESSIONS", self.sessions),
            patch.object(webapp, "execute_chat", return_value="reply"),
        ):
            webapp.write_job(job)
            webapp.run_chat_job(job["id"])
            saved = webapp.read_job(job["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["reply"], "reply")
        self.assertTrue(saved["started_at"])
        self.assertTrue(saved["finished_at"])

    def test_one_active_job_per_project(self):
        with (
            patch.object(webapp, "JOBS", self.jobs),
            patch.object(webapp.threading, "Thread") as thread,
        ):
            first = webapp.create_chat_job(self.project, "one")
            with self.assertRaises(HTTPException) as captured:
                webapp.create_chat_job(self.project, "two")
        self.assertEqual(captured.exception.status_code, 409)
        self.assertEqual(first["status"], "queued")
        thread.return_value.start.assert_called_once()

    def test_reference_upload_is_safe_and_non_overwriting(self):
        async def upload(filename, content):
            item = UploadFile(filename=filename, file=BytesIO(content))
            return await webapp.upload_references(self.project.name, [item])

        with patch.object(webapp, "STUDIO_ROOT", self.root):
            first = asyncio.run(upload("reference.png", b"one"))
            second = asyncio.run(upload("reference.png", b"two"))
            with self.assertRaises(HTTPException) as captured:
                asyncio.run(upload("../escape.png", b"bad"))
            with self.assertRaises(HTTPException):
                asyncio.run(upload("..\\escape.png", b"bad"))
        self.assertEqual(first["references"][0]["name"], "reference.png")
        self.assertEqual(second["references"][0]["name"], "reference_2.png")
        self.assertEqual(captured.exception.status_code, 400)
        self.assertFalse((self.project.parent / "escape.png").exists())

    def test_failed_upload_batch_rolls_back_saved_files(self):
        items = [
            UploadFile(filename="small.png", file=BytesIO(b"ok")),
            UploadFile(filename="large.png", file=BytesIO(b"too-large")),
        ]
        with (
            patch.object(webapp, "STUDIO_ROOT", self.root),
            patch.object(webapp, "MAX_REFERENCE_BYTES", 3),
            self.assertRaises(HTTPException) as captured,
        ):
            asyncio.run(webapp.upload_references(self.project.name, items))
        self.assertEqual(captured.exception.status_code, 413)
        self.assertEqual(list((self.project / "references").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
