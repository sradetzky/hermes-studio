import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from studio_core.comfyui_mcp import cleanup_comfyui
from studio_core.generation_contracts import (
    GenerationContract,
    parse_generation_contract,
)
from webapp.generation_runner import GenerationJobRunner, GenerationRuntime


class GenerationJobRunnerTests(unittest.TestCase):
    def _contract(self, reference: str = "ref.jpg") -> GenerationContract:
        prompt = "exact prompt\n"
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        return parse_generation_contract({
            "schema_version": 1,
            "action": "generate-current-prompt",
            "prompt": prompt,
            "prompt_sha256": digest,
            "settings_updated_at": "2026-08-25T00:00:00+00:00",
            "settings_manifest": {
                "schema_version": 2,
                "prompt_sha256": digest,
                "updated_at": "2026-08-25T00:00:00+00:00",
                "mode": "r2v",
                "aspect": "16:9",
                "mp": 0.5,
                "width": 832,
                "height": 480,
                "seed": 42,
                "steps": 8,
                "accel": True,
            },
            "execution": {
                "resolution": {
                    "mode": "explicit", "width": 832, "height": 480,
                    "megapixels": 0.399,
                },
                "timing": {
                    "requested_seconds": 5.0, "fps": 24, "frames": 124,
                    "actual_seconds": 5.167,
                },
                "references": [reference],
            },
            "expected_generation_id": "001",
        })

    def _fixture(self, root: Path) -> tuple[GenerationRuntime, Path, Path]:
        studio_root = root / "studio-root"
        project = studio_root / "projects" / "project"
        clip = project / "clips" / "clip-001"
        references = project / "references"
        profile = root / "profile"
        comfy = root / "ComfyUI"
        comfy_target = root / "comfy-target"
        clip.mkdir(parents=True)
        references.mkdir()
        (clip / "current_prompt.txt").write_text("exact prompt\n")
        (clip / "current_generation.json").write_text(json.dumps({
            "prompt_sha256": self._contract().prompt_sha256,
            "updated_at": "2026-08-25T00:00:00+00:00",
        }))
        (references / "ref.jpg").write_bytes(b"reference")
        (profile / "skills/minimax-h3-run/scripts").mkdir(parents=True)
        (comfy_target / "output").mkdir(parents=True)
        comfy.symlink_to(comfy_target, target_is_directory=True)
        runtime = GenerationRuntime(
            studio_root=studio_root,
            runtime_root=root / "runtime",
            profile_home=profile,
            real_home=root,
            comfy_root=comfy,
            comfy_url="http://127.0.0.1:8188",
            comfy_python=comfy / ".venv/bin/python",
            timeout_seconds=10800,
        )
        return runtime, project, clip

    def test_direct_runner_builds_submits_waits_archives_and_cleans_without_hermes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, _project, clip = self._fixture(root)
            calls = []
            events = []
            archived = []

            def run_command(command, **kwargs):
                calls.append(("command", command, kwargs))
                output_path = Path(command[command.index("--output-json") + 1])
                output_path.write_text(json.dumps({
                    "prompt_stage1_h3": {
                        "condition": {
                            "class_type": "MiniMaxH3ReferenceToVideo",
                            "inputs": {"prompt": "exact prompt"},
                        },
                        "reference": {
                            "class_type": "LoadImage",
                            "inputs": {"image": "ref.jpg"},
                        },
                        "save": {
                            "class_type": "SaveVideo",
                            "inputs": {"video": ["condition", 0]},
                        },
                    },
                }))
                return subprocess.CompletedProcess(command, 0, "", "")

            responses = [
                {"filename": "ref.jpg"},
                {"batch_id": "batch-id", "prompt_ids": ["prompt-id"]},
                {"result": json.dumps({
                    "batch_id": "batch-id",
                    "jobs": [{"prompt_id": "prompt-id", "state": "done"}],
                    "all_terminal": True,
                    "timed_out": False,
                })},
                {
                    "batch_id": "batch-id",
                    "completed": 1,
                    "outputs": [{
                        "prompt_id": "prompt-id",
                        "state": "done",
                        "outputs": {
                            "save": {"videos": [{
                                "filename": "render.mp4",
                                "subfolder": "h3",
                                "type": "output",
                            }]},
                        },
                    }],
                },
                "VRAM cleared successfully",
            ]

            def mcp_call(tool, arguments, environment, timeout=180):
                calls.append((tool, arguments, timeout))
                return responses.pop(0)

            def archive(*args, **kwargs):
                archived.append((args, kwargs))
                return clip / "generations/001"

            runner = GenerationJobRunner(
                runtime,
                command_runner=run_command,
                mcp_call=mcp_call,
                archiver=archive,
                event_callback=lambda *args, **kwargs: events.append((args, kwargs)),
            )
            result = runner.run(
                "job-id", "project", "clip-001", self._contract())

            self.assertEqual(result, {
                "generation_id": "001",
                "prompt_id": "prompt-id",
                "outputs": ["h3/render.mp4"],
            })
            graph_command = calls[0][1]
            self.assertNotIn("hermes", graph_command)
            self.assertIn("--dry-run", graph_command)
            self.assertEqual(
                [entry[0] for entry in calls[1:]],
                ["upload_image", "batch", "batch", "batch", "clear_vram"],
            )
            self.assertEqual(calls[2][1]["action"], "submit")
            self.assertEqual(calls[3][1], {
                "action": "wait", "batch_id": "batch-id", "timeout_s": 600,
            })
            self.assertEqual(archived[0][0][3], ["h3/render.mp4"])
            self.assertEqual(archived[0][0][4], {"prompt_id": "prompt-id"})
            self.assertEqual(
                archived[0][1]["source_root"],
                runtime.comfy_root.resolve() / "output",
            )
            self.assertIn("generation.archive", [item[0][0] for item in events])
            self.assertEqual(responses, [])

    def test_failure_after_submission_cancels_queue_verifies_it_and_clears_vram(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime, _project, _clip = self._fixture(root)
            calls = []

            def run_command(command, **_kwargs):
                output_path = Path(command[command.index("--output-json") + 1])
                output_path.write_text(json.dumps({
                    "prompt_stage1_h3": {
                        "condition": {
                            "class_type": "MiniMaxH3ReferenceToVideo",
                            "inputs": {"prompt": "exact prompt"},
                        },
                        "reference": {
                            "class_type": "LoadImage",
                            "inputs": {"image": "ref.jpg"},
                        },
                        "save": {
                            "class_type": "SaveVideo",
                            "inputs": {"video": ["condition", 0]},
                        },
                    },
                }))
                return subprocess.CompletedProcess(command, 0, "", "")

            responses = [
                {"filename": "ref.jpg"},
                {"batch_id": "batch-id", "prompt_ids": ["prompt-id"]},
                {
                    "batch_id": "batch-id",
                    "jobs": [{
                        "prompt_id": "prompt-id", "state": "error",
                        "error_message": "render failed",
                    }],
                    "all_terminal": True,
                    "timed_out": False,
                },
                "cancelled",
                {"running": 0, "pending": 0, "running_jobs": [], "pending_jobs": []},
                "VRAM cleared successfully",
            ]

            def mcp_call(tool, arguments, environment, timeout=180):
                calls.append((tool, arguments, timeout))
                return responses.pop(0)

            runner = GenerationJobRunner(
                runtime, command_runner=run_command, mcp_call=mcp_call,
                archiver=lambda *_args, **_kwargs: self.fail("must not archive"),
            )
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                runner.run("job-id", "project", "clip-001", self._contract())

            self.assertEqual(calls[-3][0:2], (
                "queue", {
                    "action": "cancel", "prompt_id": "prompt-id",
                    "clear_pending": True,
                }))
            self.assertEqual(calls[-2][0:2], ("queue", {"action": "list"}))
            self.assertEqual(calls[-1][0], "clear_vram")
            self.assertEqual(responses, [])

    def test_cleanup_accepts_cancel_race_only_after_queue_verifies_empty(self):
        calls = []

        def mcp_call(tool, arguments, environment, timeout=180):
            calls.append((tool, arguments))
            if len(calls) == 1:
                raise RuntimeError("prompt already completed")
            if len(calls) == 2:
                return {"running": 0, "pending": 0}
            return "VRAM cleared successfully"

        cleanup_comfyui(
            {}, prompt_id="prompt-id", cancel=True, mcp_call=mcp_call)

        self.assertEqual([item[0] for item in calls], [
            "queue", "queue", "clear_vram"])


if __name__ == "__main__":
    unittest.main()
