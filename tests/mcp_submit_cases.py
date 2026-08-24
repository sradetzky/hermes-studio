import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import submit_h3_graph_mcp as submit_mcp


class H3McpSubmissionTests(unittest.TestCase):
    def test_exact_graph_is_uploaded_serially_and_submitted_without_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prompt = root / "current_prompt.txt"
            settings = root / "current_generation.json"
            graph_path = root / "graph.json"
            image = root / "ref.jpg"
            result_path = root / "result.json"
            prompt.write_text("exact prompt\n", encoding="utf-8")
            prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
            settings.write_text(json.dumps({
                "prompt_sha256": prompt_sha256,
                "updated_at": "timestamp",
            }), encoding="utf-8")
            image.write_bytes(b"image")
            graph_path.write_text(json.dumps({
                "prompt_stage1_h3": {
                    "cond": {
                        "class_type": "Conditioner",
                        "inputs": {"prompt": "exact prompt"},
                    },
                    "ref": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "ref.jpg"},
                    },
                },
            }), encoding="utf-8")
            responses = [
                subprocess.CompletedProcess(
                    [], 0, json.dumps({
                        "content": [{
                            "type": "text",
                            "text": "Uploaded via HTTP.\n\nFilename: server/ref.jpg\n",
                        }],
                    }), ""),
                subprocess.CompletedProcess(
                    [], 0, json.dumps({
                        "batch_id": "batch-id",
                        "count": 1,
                        "prompt_ids": ["prompt-id"],
                    }), ""),
            ]

            with (
                patch.object(submit_mcp.subprocess, "run", side_effect=responses) as run,
                redirect_stdout(StringIO()),
            ):
                code = submit_mcp.main([
                        "--graph-json", str(graph_path),
                        "--prompt-file", str(prompt),
                        "--settings-file", str(settings),
                        "--prompt-sha256", prompt_sha256,
                        "--settings-updated-at", "timestamp",
                        "--image", str(image),
                        "--comfyui-path", str(root),
                        "--comfyui-python", sys.executable,
                        "--result-json", str(result_path),
                    ])

            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 2)
            upload_command = run.call_args_list[0].args[0]
            batch_command = run.call_args_list[1].args[0]
            self.assertIn("mcporter@0.13.7", upload_command)
            self.assertTrue(any(
                "comfyui-mcp@0.52.61" in argument
                for argument in upload_command))
            upload_args = json.loads(upload_command[upload_command.index("--args") + 1])
            self.assertEqual(upload_args, {
                "action": "image",
                "source_path": str(image),
            })
            upload_environment = run.call_args_list[0].kwargs["env"]
            self.assertEqual(upload_environment["COMFYUI_PATH"], str(root))
            self.assertEqual(upload_environment["COMFYUI_PYTHON"], sys.executable)
            self.assertEqual(
                upload_environment["COMFYUI_URL"], "http://127.0.0.1:8188")
            batch_args = json.loads(batch_command[batch_command.index("--args") + 1])
            self.assertEqual(batch_args["action"], "submit")
            self.assertTrue(batch_args["disable_random_seed"])
            submitted_graph = batch_args["workflows"][0]
            self.assertEqual(submitted_graph["cond"]["inputs"]["prompt"], "exact prompt")
            self.assertEqual(
                submitted_graph["ref"]["inputs"]["image"], "server/ref.jpg")
            self.assertEqual(json.loads(result_path.read_text()), {
                "status": "submitted",
                "batch_id": "batch-id",
                "prompt_id": "prompt-id",
                "images": {"ref.jpg": "server/ref.jpg"},
            })

    def test_result_reservation_prevents_duplicate_submission(self):
        with tempfile.TemporaryDirectory() as directory:
            result_path = Path(directory) / "result.json"
            result_path.write_text('{"status":"submitting"}\n')
            with self.assertRaisesRegex(ValueError, "duplicate submission"):
                submit_mcp.main([
                    "--graph-json", "graph.json",
                    "--prompt-file", "prompt.txt",
                    "--settings-file", "settings.json",
                    "--prompt-sha256", "hash",
                    "--settings-updated-at", "timestamp",
                    "--comfyui-path", str(Path(directory)),
                    "--comfyui-python", sys.executable,
                    "--result-json", str(result_path),
                ])


if __name__ == "__main__":
    unittest.main()
