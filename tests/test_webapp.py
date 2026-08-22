import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
