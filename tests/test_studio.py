import json
import subprocess
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from scripts import design_studio as ds
from scripts import krea2_image
from scripts.krea2_image import parse_loras


class ProjectPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = ds.studio_root(self.temp.name)
        with redirect_stdout(StringIO()):
            self.project = ds.create_project(self.root, "same-name", "test")

    def tearDown(self):
        self.temp.cleanup()

    def test_requires_exact_project_id(self):
        self.assertEqual(ds.project_path(self.root, self.project.name), self.project)
        with self.assertRaises(FileNotFoundError):
            ds.project_path(self.root, "same-name")

    def test_rejects_path_traversal(self):
        for value in ("../outside", "foo/bar", ".", ".."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ds.project_path(self.root, value)

    def test_atomic_concurrent_chat_appends(self):
        count = 100
        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(
                lambda i: ds.append_chat(
                    self.root, self.project.name, "user", f"message-{i}"),
                range(count),
            ))
        records = [json.loads(line) for line in
                   (self.project / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(len(records), count)
        self.assertEqual({r["content"] for r in records},
                         {f"message-{i}" for i in range(count)})

    def test_archives_mcp_outputs_with_protected_metadata(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        source = comfy_output / "result.png"
        source.write_bytes(b"image")
        ds.write_prompt(self.root, self.project.name, "the prompt")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            generation = ds.archive_outputs(
                self.root,
                self.project.name,
                ["result.png"],
                {"prompt_id": "mcp-123", "transport": "forged"},
            )
        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(meta["prompt_id"], "mcp-123")
        self.assertEqual(meta["transport"], "comfyui-mcp")
        self.assertEqual(meta["files"], ["result.png"])
        self.assertEqual((generation / "result.png").read_bytes(), b"image")
        self.assertEqual((generation / "prompt.txt").read_text(), "the prompt\n")

    def test_archive_rejects_output_outside_comfy_directory(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            with self.assertRaises(ValueError):
                ds.archive_outputs(
                    self.root, self.project.name, [str(outside)])


class LoraParserTests(unittest.TestCase):
    def test_parses_name_and_strength(self):
        self.assertEqual(parse_loras(["foo.safetensors:0.75"]),
                         [("foo.safetensors", 0.75)])

    def test_rejects_invalid_spec(self):
        with self.assertRaises(ValueError):
            parse_loras(["foo.safetensors"])
        with self.assertRaises(ValueError):
            parse_loras(["foo.safetensors:not-a-number"])


class LegacyCleanupTests(unittest.TestCase):
    @patch.object(krea2_image, "free_vram", return_value={"ok": True})
    @patch.object(krea2_image, "wait", return_value={"done": True, "files": []})
    @patch.object(krea2_image, "queue", return_value="prompt-1")
    def test_image_fallback_cleans_after_success(self, queue, wait, free_vram):
        with redirect_stdout(StringIO()):
            result = krea2_image.main([
                "--recipe", "t2i", "--prompt", "test", "--seed", "1"])
        self.assertEqual(result, 0)
        queue.assert_called_once()
        wait.assert_called_once_with("prompt-1", timeout=900)
        free_vram.assert_called_once()

    @patch.object(krea2_image, "free_vram", return_value={"ok": True})
    @patch.object(krea2_image, "interrupt", return_value={"ok": True})
    @patch.object(krea2_image, "wait",
                  return_value={"done": False, "error": "timeout"})
    @patch.object(krea2_image, "queue", return_value="prompt-2")
    def test_image_timeout_interrupts_before_cleanup(
            self, queue, wait, interrupt, free_vram):
        with redirect_stdout(StringIO()):
            result = krea2_image.main([
                "--recipe", "t2i", "--prompt", "test", "--seed", "1"])
        self.assertEqual(result, 1)
        interrupt.assert_called_once()
        free_vram.assert_called_once()

    def test_h3_timeout_interrupts_before_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = ds.studio_root(directory)
            project = ds.create_project(root, "timeout-test")
            with (
                patch.object(ds.subprocess, "run",
                             side_effect=subprocess.TimeoutExpired("h3", 1)),
                patch.object(ds, "interrupt_comfy",
                             return_value={"ok": True}) as interrupt,
                patch.object(ds, "free_comfy_vram",
                             return_value={"ok": True}) as free_vram,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                ds.run_generation(root, project.name, timeout=1)
            interrupt.assert_called_once()
            free_vram.assert_called_once()


if __name__ == "__main__":
    unittest.main()
