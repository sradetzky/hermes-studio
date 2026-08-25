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
from studio_core import comfyui_mcp


class H3McpSubmissionTests(unittest.TestCase):
    def _make_contract(self, root: Path) -> tuple[list[str], Path]:
        prompt = root / "current_prompt.txt"
        settings = root / "current_generation.json"
        graph_path = root / "graph.json"
        result_path = root / "result.json"
        prompt.write_text("exact prompt\n", encoding="utf-8")
        prompt_sha256 = hashlib.sha256(prompt.read_bytes()).hexdigest()
        settings.write_text(json.dumps({
            "prompt_sha256": prompt_sha256,
            "updated_at": "timestamp",
        }), encoding="utf-8")
        graph_path.write_text(json.dumps({
            "prompt_stage1_h3": {
                "cond": {
                    "class_type": "Conditioner",
                    "inputs": {"prompt": "exact prompt"},
                },
            },
        }), encoding="utf-8")
        return [
            "--graph-json", str(graph_path),
            "--prompt-file", str(prompt),
            "--settings-file", str(settings),
            "--prompt-sha256", prompt_sha256,
            "--settings-updated-at", "timestamp",
            "--comfyui-path", str(root),
            "--comfyui-python", sys.executable,
            "--result-json", str(result_path),
        ], result_path

    def test_exact_graph_is_uploaded_serially_and_submitted_without_transcription(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments, result_path = self._make_contract(root)
            graph_path = root / "graph.json"
            first_image = root / "first.jpg"
            second_image = root / "second.jpg"
            first_image.write_bytes(b"first image")
            second_image.write_bytes(b"second image")
            graph_path.write_text(json.dumps({
                "prompt_stage1_h3": {
                    "cond": {
                        "class_type": "Conditioner",
                        "inputs": {"prompt": "exact prompt"},
                    },
                    "first_ref": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "first.jpg"},
                    },
                    "second_ref": {
                        "class_type": "LoadImage",
                        "inputs": {"image": "second.jpg"},
                    },
                },
            }), encoding="utf-8")
            responses = [
                subprocess.CompletedProcess(
                    [], 0, json.dumps({
                        "content": [{
                            "type": "text",
                            "text": "Uploaded via HTTP.\n\nFilename: server/first.jpg\n",
                        }],
                    }), ""),
                subprocess.CompletedProcess(
                    [], 0, json.dumps({
                        "content": [{
                            "type": "text",
                            "text": "Uploaded via HTTP.\n\nFilename: server/second.jpg\n",
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
                patch.object(comfyui_mcp.subprocess, "run", side_effect=responses) as run,
                redirect_stdout(StringIO()),
            ):
                code = submit_mcp.main([
                    *arguments,
                    "--image", str(first_image),
                    "--image", str(second_image),
                ])

            self.assertEqual(code, 0)
            self.assertEqual(run.call_count, 3)
            first_upload = run.call_args_list[0].args[0]
            second_upload = run.call_args_list[1].args[0]
            batch_command = run.call_args_list[2].args[0]
            self.assertIn("mcporter@0.13.7", first_upload)
            self.assertTrue(any("comfyui-mcp@0.52.61" in argument
                                for argument in first_upload))
            self.assertEqual(
                first_upload[first_upload.index("--timeout") + 1], "180000")
            self.assertEqual(run.call_args_list[0].kwargs["timeout"], 195)
            first_upload_args = json.loads(
                first_upload[first_upload.index("--args") + 1])
            second_upload_args = json.loads(
                second_upload[second_upload.index("--args") + 1])
            self.assertEqual(first_upload_args, {
                "action": "image",
                "source_path": str(first_image),
            })
            self.assertEqual(second_upload_args, {
                "action": "image",
                "source_path": str(second_image),
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
                submitted_graph["first_ref"]["inputs"]["image"], "server/first.jpg")
            self.assertEqual(
                submitted_graph["second_ref"]["inputs"]["image"], "server/second.jpg")
            self.assertEqual(json.loads(result_path.read_text()), {
                "status": "submitted",
                "batch_id": "batch-id",
                "prompt_id": "prompt-id",
                "images": {
                    "first.jpg": "server/first.jpg",
                    "second.jpg": "server/second.jpg",
                },
            })

    def test_prompt_hash_mismatch_fails_before_mcp_calls(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, result_path = self._make_contract(Path(directory))
            hash_index = arguments.index("--prompt-sha256") + 1
            arguments[hash_index] = "0" * 64

            with (
                patch.object(comfyui_mcp.subprocess, "run") as run,
                self.assertRaisesRegex(ValueError, "prompt SHA-256"),
            ):
                submit_mcp.main(arguments)

            run.assert_not_called()
            result = json.loads(result_path.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("prompt SHA-256", result["error"])

    def test_settings_revision_mismatch_fails_before_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments, result_path = self._make_contract(root)
            image = root / "ref.jpg"
            image.write_bytes(b"image")
            arguments.extend(["--image", str(image)])
            revision_index = arguments.index("--settings-updated-at") + 1
            arguments[revision_index] = "stale-timestamp"

            with (
                patch.object(comfyui_mcp.subprocess, "run") as run,
                self.assertRaisesRegex(ValueError, "settings timestamp"),
            ):
                submit_mcp.main(arguments)

            run.assert_not_called()
            result = json.loads(result_path.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("settings timestamp", result["error"])

    def test_failed_upload_stops_before_batch_and_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            arguments, result_path = self._make_contract(root)
            image = root / "ref.jpg"
            image.write_bytes(b"image")
            arguments.extend(["--image", str(image)])
            response = subprocess.CompletedProcess([], 9, "", "upload failed")

            with (
                patch.object(comfyui_mcp.subprocess, "run", return_value=response) as run,
                self.assertRaisesRegex(RuntimeError, "upload_image failed"),
            ):
                submit_mcp.main(arguments)

            self.assertEqual(run.call_count, 1)
            result = json.loads(result_path.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("upload_image failed", result["error"])

    def test_failed_batch_result_records_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            arguments, result_path = self._make_contract(Path(directory))
            response = subprocess.CompletedProcess(
                [], 0, json.dumps({"batch_id": "batch-id", "prompt_ids": []}), "")

            with (
                patch.object(comfyui_mcp.subprocess, "run", return_value=response) as run,
                self.assertRaisesRegex(RuntimeError, "one prompt_id"),
            ):
                submit_mcp.main(arguments)

            self.assertEqual(run.call_count, 1)
            result = json.loads(result_path.read_text())
            self.assertEqual(result["status"], "failed")
            self.assertIn("one prompt_id", result["error"])

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
