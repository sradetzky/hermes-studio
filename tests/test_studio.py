import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import design_studio as ds
from scripts import krea2_image
from scripts.krea2_image import parse_loras
from webapp.clip_store import ClipStore, ClipStoreError
from webapp.job_store import JobStore


class ClipStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.project.mkdir()
        self.store = ClipStore()

    def tearDown(self):
        self.temp.cleanup()

    def test_initializes_project_with_one_safe_clip(self):
        manifest = self.store.initialize(self.project, "Test project")
        self.assertEqual(manifest, {
            "schema_version": 1,
            "title": "Test project",
            "clips": [{
                "id": "clip-001",
                "title": "Main clip",
                "enabled": True,
                "selected_take": None,
            }],
        })
        clip = self.store.resolve_clip(self.project, "clip-001")
        self.assertTrue((clip / "current_prompt.txt").is_file())
        self.assertTrue((clip / "generations").is_dir())
        self.assertEqual(
            json.loads((self.project / "project.json").read_text()), manifest)

    def test_creates_updates_and_reorders_clips(self):
        self.store.initialize(self.project, "Test project")
        second = self.store.create_clip(self.project, "Second scene")
        third = self.store.create_clip(self.project, "Third scene")
        self.assertEqual((second["id"], third["id"]),
                         ("clip-002", "clip-003"))

        updated = self.store.update_clip(
            self.project, "clip-002", title="Reveal", enabled=False)
        self.assertEqual(updated["title"], "Reveal")
        self.assertFalse(updated["enabled"])
        reordered = self.store.reorder(
            self.project, ["clip-003", "clip-001", "clip-002"])
        self.assertEqual(
            [clip["id"] for clip in reordered["clips"]],
            ["clip-003", "clip-001", "clip-002"],
        )

    def test_concurrent_clip_creation_serializes_ids_and_manifest(self):
        self.store.initialize(self.project, "Test project")
        with ThreadPoolExecutor(max_workers=8) as pool:
            created = list(pool.map(
                lambda index: self.store.create_clip(
                    self.project, f"Clip {index}")["id"],
                range(8),
            ))
        self.assertEqual(len(set(created)), 8)
        manifest = self.store.describe(self.project)
        self.assertEqual(len(manifest["clips"]), 9)
        self.assertEqual(
            {clip["id"] for clip in manifest["clips"]},
            {f"clip-{index:03d}" for index in range(1, 10)},
        )

    def test_selects_and_clears_one_existing_video_take(self):
        self.store.initialize(self.project, "Test project")
        generation = (
            self.project / "clips" / "clip-001" / "generations" / "001")
        generation.mkdir()
        (generation / "video.mp4").write_bytes(b"video")
        selected = self.store.select_take(
            self.project, "clip-001", "001", "video.mp4")
        self.assertEqual(selected["selected_take"], {
            "generation": "001", "filename": "video.mp4"})
        cleared = self.store.select_take(
            self.project, "clip-001", None)
        self.assertIsNone(cleared["selected_take"])

    def test_rejects_invalid_order_selection_and_symlinked_clip(self):
        self.store.initialize(self.project, "Test project")
        self.store.create_clip(self.project, "Second")
        with self.assertRaisesRegex(ClipStoreError, "every clip"):
            self.store.reorder(self.project, ["clip-001"])
        with self.assertRaisesRegex(ClipStoreError, "must be a video"):
            generation = (
                self.project / "clips" / "clip-001" / "generations" / "001")
            generation.mkdir()
            (generation / "still.png").write_bytes(b"image")
            self.store.select_take(
                self.project, "clip-001", "001", "still.png")

        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        clip = self.project / "clips" / "clip-002"
        for item in clip.iterdir():
            if item.is_dir():
                item.rmdir()
            else:
                item.unlink()
        clip.rmdir()
        clip.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ClipStoreError, "clip not found"):
            self.store.describe(self.project)


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
        self.assertTrue((self.project / "research").is_dir())
        manifest = json.loads((self.project / "project.json").read_text())
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual([clip["id"] for clip in manifest["clips"]], ["clip-001"])
        clip = self.project / "clips" / "clip-001"
        self.assertTrue((clip / "current_prompt.txt").is_file())
        self.assertTrue((clip / "generations").is_dir())
        self.assertFalse((self.project / "current_prompt.txt").exists())
        self.assertFalse((self.project / "generations").exists())
        with self.assertRaises(FileNotFoundError):
            ds.project_path(self.root, "same-name")

    def test_clip_resolution_requires_exact_manifest_id(self):
        clip = ds.clip_path(self.root, self.project.name, "clip-001")
        self.assertEqual(clip, self.project / "clips" / "clip-001")
        (self.project / "clips" / "clip-999").mkdir()
        for value in ("Main clip", "clip-999", "../clip-001", "clip-1"):
            with self.subTest(value=value), self.assertRaises(ClipStoreError):
                ds.clip_path(self.root, self.project.name, value)

    def test_rejects_path_traversal(self):
        for value in ("../outside", "foo/bar", ".", ".."):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ds.project_path(self.root, value)

    def test_rejects_project_and_prompt_symlinks(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        alias = self.root / "projects" / "alias"
        alias.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ds.project_path(self.root, alias.name)

        target = Path(self.temp.name) / "outside-prompt.txt"
        target.write_text("secret")
        prompt = self.project / "clips" / "clip-001" / "current_prompt.txt"
        prompt.unlink()
        prompt.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "regular clip file"):
            ds.write_prompt(
                self.root, self.project.name, "clip-001", "replacement")
        self.assertEqual(target.read_text(), "secret")

    def test_prompt_writes_are_atomic_and_clip_scoped(self):
        second = ClipStore().create_clip(self.project, "Second")
        written = ds.write_prompt(
            self.root, self.project.name, second["id"], "second prompt")
        self.assertEqual(
            written, self.project / "clips" / "clip-002" / "current_prompt.txt")
        self.assertEqual(written.read_text(), "second prompt\n")
        self.assertEqual(
            (self.project / "clips" / "clip-001" / "current_prompt.txt").read_text(),
            "",
        )

    def test_clip_management_cli_and_exact_clip_requirement(self):
        def invoke(*arguments):
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(
                    ds.main(["--root", str(self.root), *arguments]), 0)
            return output.getvalue().strip()

        created = json.loads(invoke(
            "create-clip", self.project.name, "Second scene"))
        self.assertEqual(created["id"], "clip-002")
        updated = json.loads(invoke(
            "update-clip", self.project.name, "clip-002",
            "--title", "Reveal", "--disable"))
        self.assertEqual((updated["title"], updated["enabled"]),
                         ("Reveal", False))
        reordered = json.loads(invoke(
            "reorder-clips", self.project.name, "clip-002", "clip-001"))
        self.assertEqual(
            [clip["id"] for clip in reordered["clips"]],
            ["clip-002", "clip-001"],
        )
        listed = json.loads(invoke("list-clips", self.project.name))
        self.assertEqual(listed, reordered)

        generation = (
            self.project / "clips" / "clip-001" / "generations" / "001")
        generation.mkdir()
        (generation / "take.mp4").write_bytes(b"video")
        selected = json.loads(invoke(
            "select-take", self.project.name, "clip-001", "001", "take.mp4"))
        self.assertEqual(selected["selected_take"], {
            "generation": "001", "filename": "take.mp4"})
        cleared = json.loads(invoke(
            "select-take", self.project.name, "clip-001", "--clear"))
        self.assertIsNone(cleared["selected_take"])

        grok_cache = Path(self.temp.name) / "grok-cli-cache"
        grok_cache.mkdir()
        (grok_cache / "grok.png").write_bytes(b"image")
        with patch.object(ds, "GROK_IMAGE_OUTPUT", grok_cache):
            archived = Path(invoke(
                "archive-grok", self.project.name, "clip-002", "grok.png"))
        self.assertEqual(
            archived.parent,
            self.project / "clips" / "clip-002" / "generations",
        )
        self.assertEqual(
            json.loads((archived / "meta.json").read_text())["transport"],
            "xai-imagine",
        )

        with self.assertRaises(SystemExit):
            ds.main([
                "--root", str(self.root), "write-prompt",
                self.project.name, "prompt-without-clip",
            ])

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

    def test_configured_cli_chat_append_uses_transactional_store(self):
        runtime = Path(self.temp.name) / "runtime"
        with patch.dict(os.environ, {
            "DESIGN_STUDIO_ROOT": str(self.root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(runtime),
        }):
            ds.append_chat(self.root, self.project.name, "user", "transactional")
        store = JobStore(runtime / "studio.db")
        total, events = store.chat_events(self.project.name)
        self.assertEqual(total, 1)
        self.assertEqual(events[0].content, "transactional")
        exported = [json.loads(line) for line in
                    (self.project / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(exported[0]["content"], "transactional")

    def test_direct_script_entrypoint_reaches_transactional_store(self):
        runtime = Path(self.temp.name) / "direct-runtime"
        environment = {
            **os.environ,
            "DESIGN_STUDIO_ROOT": str(self.root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(runtime),
        }
        script = ds.REPO_ROOT / "scripts" / "design_studio.py"
        subprocess.run(
            [sys.executable, str(script), "append-chat", self.project.name,
             "system", "direct-entrypoint"],
            cwd=ds.REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        store = JobStore(runtime / "studio.db")
        total, events = store.chat_events(self.project.name)
        self.assertEqual(total, 1)
        self.assertEqual(events[0].content, "direct-entrypoint")

    def test_cli_append_after_import_survives_web_job_export(self):
        runtime = Path(self.temp.name) / "reconcile-runtime"
        with patch.dict(os.environ, {
            "DESIGN_STUDIO_ROOT": str(self.root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(runtime),
        }):
            store = JobStore(runtime / "studio.db")
            store.initialize()
            store.import_chat_if_empty(
                self.project.name, self.project / "chat.jsonl")
            ds.append_chat(
                self.root, self.project.name, "system", "external-after-import")
            job = store.create_chat_job(self.project.name, "question")
            store.claim_next("worker")
            store.complete(job.id, "worker", "answer", "session")
            store.export_chat(self.project.name, self.project / "chat.jsonl")
        rows = [json.loads(line) for line in
                (self.project / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(
            [(row["role"], row["content"]) for row in rows],
            [
                ("system", "external-after-import"),
                ("user", "question"),
                ("assistant", "answer"),
            ],
        )

    def test_archives_mcp_outputs_with_protected_metadata(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        source = comfy_output / "result.png"
        source.write_bytes(b"image")
        settings = {"schema_version": 2, "seed": 42, "steps": 20}
        clip = self.project / "clips" / "clip-001"
        (clip / "current_generation.json").write_text(
            json.dumps(settings) + "\n", encoding="utf-8")
        ds.write_prompt(self.root, self.project.name, "clip-001", "the prompt")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            generation = ds.archive_outputs(
                self.root,
                self.project.name,
                "clip-001",
                ["result.png"],
                {"prompt_id": "mcp-123", "transport": "forged"},
            )
        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(meta["prompt_id"], "mcp-123")
        self.assertEqual(meta["transport"], "comfyui-mcp")
        self.assertEqual(meta["files"], ["result.png"])
        self.assertEqual((generation / "result.png").read_bytes(), b"image")
        self.assertEqual((generation / "prompt.txt").read_text(), "the prompt\n")
        self.assertEqual(
            json.loads((generation / "settings.json").read_text()), settings)

    def test_archive_without_current_settings_records_exact_null_snapshot(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        generation = ds.archive_outputs(
            self.root, self.project.name, "clip-001", ["result.png"],
            source_root=comfy_output)
        self.assertEqual((generation / "prompt.txt").read_text(), "")
        self.assertEqual((generation / "settings.json").read_text(), "null\n")

    def test_archive_rejects_output_outside_comfy_directory(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        with patch.object(ds, "COMFY_OUTPUT", comfy_output):
            with self.assertRaises(ValueError):
                ds.archive_outputs(
                    self.root, self.project.name, "clip-001", [str(outside)])

    def test_archive_rejects_symlinked_output(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside.png"
        outside.write_bytes(b"image")
        (comfy_output / "linked.png").symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["linked.png"],
                source_root=comfy_output)

    def test_archive_rejects_symlinked_generation_directory(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        outside = Path(self.temp.name) / "outside-generations"
        outside.mkdir()
        generations = self.project / "clips" / "clip-001" / "generations"
        generations.rmdir()
        generations.symlink_to(
            outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "generations directory"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_is_hidden_until_atomic_publication(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        source = comfy_output / "result.png"
        source.write_bytes(b"image")
        visible_during_copy = []
        real_copy = ds.shutil.copy2

        def inspect_copy(source_path, target_path):
            visible_during_copy.append([
                item.name for item in (
                    self.project / "clips" / "clip-001" / "generations").iterdir()
                if item.name.isdigit()
            ])
            return real_copy(source_path, target_path)

        with patch.object(ds.shutil, "copy2", side_effect=inspect_copy):
            generation = ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(visible_during_copy, [[]])
        self.assertEqual(generation.name, "001")
        self.assertTrue((generation / "meta.json").is_file())

    def test_archives_grok_cache_with_xai_transport(self):
        grok_cache = Path(self.temp.name) / "grok-cache"
        grok_cache.mkdir()
        image = grok_cache / "imagine.png"
        image.write_bytes(b"image")
        generation = ds.archive_outputs(
            self.root, self.project.name, "clip-001", [str(image)],
            {"prompt": "test"}, source_root=grok_cache,
            transport="xai-imagine")
        meta = json.loads((generation / "meta.json").read_text())
        self.assertEqual(meta["transport"], "xai-imagine")
        self.assertTrue((generation / "imagine.png").is_file())

    def test_concurrent_take_allocation_is_unique_and_complete(self):
        comfy_output = Path(self.temp.name) / "comfy-output"
        comfy_output.mkdir()
        (comfy_output / "result.png").write_bytes(b"image")
        with ThreadPoolExecutor(max_workers=8) as pool:
            generations = list(pool.map(
                lambda _: ds.archive_outputs(
                    self.root, self.project.name, "clip-001", ["result.png"],
                    source_root=comfy_output),
                range(8),
            ))
        self.assertEqual(
            {generation.name for generation in generations},
            {f"{index:03d}" for index in range(1, 9)},
        )
        for generation in generations:
            self.assertEqual(
                {path.name for path in generation.iterdir()},
                {"result.png", "prompt.txt", "settings.json", "meta.json"},
            )

    def test_generation_runners_archive_under_the_requested_clip(self):
        ClipStore().create_clip(self.project, "Second")
        comfy_output = Path(self.temp.name) / "runner-output"
        comfy_output.mkdir()
        video = comfy_output / "video.mp4"
        image = comfy_output / "image.png"
        video.write_bytes(b"video")
        image.write_bytes(b"image")

        video_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"video": str(video)}),
            stderr="",
        )
        image_result = SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "seed": 7, "prompt_id": "image-1", "files": ["image.png"]}),
            stderr="",
        )
        with (
            patch.object(ds, "COMFY_OUTPUT", comfy_output),
            patch.object(ds, "free_comfy_vram", return_value={"ok": True}),
            patch.object(ds.subprocess, "run", return_value=video_result),
        ):
            generated_video = ds.run_generation(
                self.root, self.project.name, "clip-002")
        with (
            patch.object(ds, "COMFY_OUTPUT", comfy_output),
            patch.object(ds.subprocess, "run", return_value=image_result),
        ):
            generated_image = ds.run_image_generation(
                self.root, self.project.name, "clip-001", "t2i", "still prompt")

        self.assertEqual(
            Path(generated_video["generation"]).parent,
            self.project / "clips" / "clip-002" / "generations",
        )
        self.assertEqual(
            Path(generated_image["generation"]).parent,
            self.project / "clips" / "clip-001" / "generations",
        )
        self.assertEqual(
            (Path(generated_image["generation"]) / "prompt.txt").read_text(),
            "still prompt\n",
        )

    def test_generation_runners_reject_unknown_clip_before_execution(self):
        with patch.object(ds.subprocess, "run") as run:
            with self.assertRaisesRegex(ClipStoreError, "clip not found"):
                ds.run_generation(
                    self.root, self.project.name, "clip-999", dry_run=True)
            with self.assertRaisesRegex(ClipStoreError, "clip not found"):
                ds.run_image_generation(
                    self.root, self.project.name, "clip-999", "t2i", "prompt")
        run.assert_not_called()

    def test_archive_rejects_reserved_media_name_and_unsafe_settings(self):
        comfy_output = Path(self.temp.name) / "unsafe-output"
        comfy_output.mkdir()
        (comfy_output / "meta.json").write_text("media")
        with self.assertRaisesRegex(ValueError, "reserved"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["meta.json"],
                source_root=comfy_output)

        (comfy_output / "result.png").write_bytes(b"image")
        outside = Path(self.temp.name) / "settings.json"
        outside.write_text("{}")
        settings = (
            self.project / "clips" / "clip-001" / "current_generation.json")
        settings.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "regular clip file"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )

    @patch.object(ds.subprocess, "run")
    def test_grok_dispatch_persists_project_session(self, run):
        run.return_value = SimpleNamespace(
            returncode=0, stdout="research result\n",
            stderr="session_id: grok-session-1\n")
        reply = ds.dispatch_grok(self.root, self.project.name, "research this")
        self.assertEqual(reply, "research result")
        first_command = run.call_args.args[0]
        self.assertNotIn("-r", first_command)
        self.assertIn("web,x_search,image_gen,vision,file,terminal", first_command)

        run.return_value = SimpleNamespace(
            returncode=0, stdout="continued\n",
            stderr="session_id: grok-session-1\n")
        ds.dispatch_grok(self.root, self.project.name, "continue")
        second_command = run.call_args.args[0]
        self.assertIn("-r", second_command)
        self.assertIn("grok-session-1", second_command)

    @patch.object(ds.subprocess, "Popen")
    def test_local_profile_dispatch_persists_project_session(self, popen):
        process = MagicMock()
        process.returncode = 0
        process.communicate.return_value = (
            "storyboard ready\n", "session_id: storyboard-session\n")
        popen.return_value = process

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERMES_STUDIO_JOB_ID", None)
            reply = ds.dispatch_profile(
                self.root, self.project.name,
                "studio-storyboarder", "Plan three shots")
            self.assertEqual(reply, "storyboard ready")
            first_command = popen.call_args.args[0]
            self.assertNotIn("-r", first_command)
            self.assertIn("--source", first_command)
            self.assertIn("studio-handoff", first_command)
            self.assertEqual(
                first_command[first_command.index("-t") + 1],
                "file,terminal,skills",
            )
            self.assertNotIn("all", first_command)

            ds.dispatch_profile(
                self.root, self.project.name,
                "studio-storyboarder", "Revise the plan")
            second_command = popen.call_args.args[0]
            self.assertIn("-r", second_command)
            self.assertIn("storyboard-session", second_command)

    def test_local_profile_dispatch_rejects_unknown_profile(self):
        with self.assertRaisesRegex(ValueError, "unsupported"):
            ds.dispatch_profile(
                self.root, self.project.name, "studio", "Do everything")


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
                ds.run_generation(root, project.name, "clip-001", timeout=1)
            interrupt.assert_called_once()
            free_vram.assert_called_once()


if __name__ == "__main__":
    unittest.main()
