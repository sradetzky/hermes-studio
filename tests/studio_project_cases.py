import json
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from scripts import design_studio as ds
from scripts import krea2_image
from scripts.krea2_image import parse_loras
from studio_core import projects as clip_store, safe_files
from studio_core.projects import ClipStore, ClipStoreError
from studio_core.job_store import JobStore


class ProjectPathTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = ds.studio_root(self.temp.name)
        with redirect_stdout(StringIO()):
            self.project = ds.create_project(self.root, "same-name", "test")

    def tearDown(self):
        self.temp.cleanup()

    def test_runtime_paths_ignore_profile_isolated_home(self):
        root = Path(self.temp.name)
        real_home = root / "real-home"
        profile = root / ".hermes" / "profiles" / "studio"
        environment = {
            **os.environ,
            "HOME": str(profile / "home"),
            "HERMES_REAL_HOME": str(real_home),
            "HERMES_HOME": str(profile),
        }
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json; from scripts import design_studio as ds; "
                "print(json.dumps({"
                "'runner': str(ds.RUN_H3), "
                "'comfy': str(ds.COMFY_ROOT), "
                "'grok': str(ds.GROK_IMAGE_OUTPUT)}))",
            ],
            cwd=ds.REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        paths = json.loads(result.stdout)
        self.assertEqual(
            paths["runner"],
            str(profile / "skills/minimax-h3-run/scripts/run_h3.py"),
        )
        self.assertEqual(paths["comfy"], str(real_home / "ComfyUI"))
        self.assertEqual(
            paths["grok"],
            str(root / ".hermes/profiles/studio-grok/cache/images"),
        )

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

    def test_select_take_serializes_media_validation_with_deletion(self):
        store = ClipStore()
        generation = (
            self.project / "clips" / "clip-001" / "generations" / "001")
        generation.mkdir()
        (generation / "take.mp4").write_bytes(b"video")
        validation_started = threading.Event()
        continue_validation = threading.Event()
        deletion_finished = threading.Event()
        errors = []
        real_open = clip_store.open_regular_file_at

        def pause_validation(*args, **kwargs):
            validation_started.set()
            if not continue_validation.wait(timeout=2):
                raise RuntimeError("selection validation was not released")
            return real_open(*args, **kwargs)

        def select():
            try:
                store.select_take(
                    self.project, "clip-001", "001", "take.mp4")
            except Exception as exc:
                errors.append(exc)

        def delete():
            try:
                store.delete_take(self.project, "clip-001", "001")
            except Exception as exc:
                errors.append(exc)
            finally:
                deletion_finished.set()

        with patch.object(
                clip_store, "open_regular_file_at",
                side_effect=pause_validation):
            selector = threading.Thread(target=select)
            selector.start()
            self.assertTrue(validation_started.wait(timeout=1))
            deleter = threading.Thread(target=delete)
            deleter.start()
            self.assertFalse(deletion_finished.wait(timeout=0.1))
            continue_validation.set()
            selector.join(timeout=2)
            deleter.join(timeout=2)

        self.assertFalse(selector.is_alive())
        self.assertFalse(deleter.is_alive())
        self.assertEqual(errors, [])
        self.assertIsNone(
            store.describe(self.project)["clips"][0]["selected_take"])
        self.assertFalse(generation.exists())

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

    def test_optional_project_metadata_ignores_missing_and_unsafe_entries(self):
        self.assertEqual(ds.read_project_text(self.project, "missing.md"), "")

        outside = Path(self.temp.name) / "outside-metadata.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.project / "linked.md").symlink_to(outside)
        (self.project / "metadata-dir").mkdir()
        for filename in ("linked.md", "metadata-dir"):
            with self.subTest(filename=filename):
                self.assertEqual(
                    ds.read_project_text(self.project, filename), "")
                with self.assertRaisesRegex(ValueError, "missing|regular file"):
                    ds.read_project_text(
                        self.project, filename, required=True)

        with self.assertRaisesRegex(ValueError, "missing"):
            ds.read_project_text(self.project, "missing.md", required=True)

    def test_optional_project_metadata_parent_swap_does_not_read_outside(self):
        displaced = Path(self.temp.name) / "displaced-project"
        outside = Path(self.temp.name) / "outside-project"
        outside.mkdir()
        (outside / "brief.md").write_text("outside secret", encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "brief.md":
                swapped = True
                self.project.rename(displaced)
                self.project.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with patch("studio_core.safe_files.os.open",
                   side_effect=swap_parent_at_final_open):
            value = ds.read_project_text(self.project, "brief.md")

        self.assertTrue(swapped)
        self.assertEqual(value, "")
        self.assertEqual((outside / "brief.md").read_text(), "outside secret")

    def test_required_clip_prompt_parent_swap_fails_closed(self):
        clip = self.project / "clips" / "clip-001"
        displaced = Path(self.temp.name) / "displaced-clip"
        outside = Path(self.temp.name) / "outside-clip"
        outside.mkdir()
        (outside / "current_prompt.txt").write_text(
            "outside secret", encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_prompt.txt":
                swapped = True
                clip.rename(displaced)
                clip.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("studio_core.safe_files.os.open",
                  side_effect=swap_parent_at_final_open),
            self.assertRaisesRegex(ValueError, "regular file"),
        ):
            ds.read_project_text(clip, "current_prompt.txt", required=True)

        self.assertTrue(swapped)
        self.assertEqual(
            (outside / "current_prompt.txt").read_text(), "outside secret")

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
        cursor, events = store.chat_events(self.project.name)
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].content, "transactional")
        exported = [json.loads(line) for line in
                    (self.project / "chat.jsonl").read_text().splitlines()]
        self.assertEqual(exported[0]["content"], "transactional")

    def test_configured_cli_chat_append_inherits_clip_scope(self):
        runtime = Path(self.temp.name) / "clip-runtime"
        environment = {
            "DESIGN_STUDIO_ROOT": str(self.root),
            "HERMES_STUDIO_RUNTIME_ROOT": str(runtime),
            "HERMES_STUDIO_PROJECT": self.project.name,
            "HERMES_STUDIO_CHAT_SCOPE": "clip",
            "HERMES_STUDIO_CLIP": "clip-001",
        }
        with patch.dict(os.environ, environment):
            ds.append_chat(
                self.root, self.project.name, "system", "clip-local")
        store = JobStore(runtime / "studio.db")
        _, project_events = store.chat_events(self.project.name)
        _, clip_events = store.chat_events(
            self.project.name, clip_id="clip-001")
        self.assertEqual(project_events, [])
        self.assertEqual(
            [event.content for event in clip_events], ["clip-local"])
        clip_chat = self.project / "clips" / "clip-001" / "chat.jsonl"
        self.assertEqual(
            json.loads(clip_chat.read_text().strip())["content"], "clip-local")

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
        cursor, events = store.chat_events(self.project.name)
        self.assertEqual(cursor, events[-1].id)
        self.assertEqual(len(events), 1)
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
            job = store.create_chat_job(
                self.project.name, "question", clip_id="clip-001")
            store.claim_next("worker")
            store.complete(job.id, "worker", "answer", "session")
            store.export_chat(self.project.name, self.project / "chat.jsonl")
            clip_chat = self.project / "clips" / "clip-001" / "chat.jsonl"
            store.export_chat(
                self.project.name, clip_chat, clip_id="clip-001")
        project_rows = [json.loads(line) for line in
                        (self.project / "chat.jsonl").read_text().splitlines()]
        clip_rows = [json.loads(line) for line in clip_chat.read_text().splitlines()]
        self.assertEqual(
            [(row["role"], row["content"]) for row in project_rows],
            [("system", "external-after-import")],
        )
        self.assertEqual(
            [(row["role"], row["content"]) for row in clip_rows],
            [("user", "question"), ("assistant", "answer")],
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

        run.return_value = SimpleNamespace(
            returncode=0, stdout="clip research\n",
            stderr="session_id: grok-clip-session\n")
        ds.dispatch_grok(
            self.root, self.project.name, "clip research", clip_id="clip-001")
        clip_command = run.call_args.args[0]
        self.assertNotIn("-r", clip_command)
        clip_prompt = clip_command[clip_command.index("-q") + 1]
        self.assertIn("Conversation scope: clip", clip_prompt)
        self.assertIn("Active clip id: clip-001", clip_prompt)

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

    @patch.object(ds.subprocess, "Popen")
    def test_local_profile_dispatch_inherits_independent_clip_session(self, popen):
        process = MagicMock()
        process.returncode = 0
        popen.return_value = process
        process.communicate.return_value = (
            "project ready\n", "session_id: project-session\n")
        ds.dispatch_profile(
            self.root, self.project.name,
            "studio-storyboarder", "Project plan", clip_id="")

        process.communicate.return_value = (
            "clip ready\n", "session_id: clip-session\n")
        environment = {
            "HERMES_STUDIO_PROJECT": self.project.name,
            "HERMES_STUDIO_CHAT_SCOPE": "clip",
            "HERMES_STUDIO_CLIP": "clip-001",
            "HERMES_STUDIO_JOB_ID": "",
        }
        with patch.dict(os.environ, environment, clear=False):
            ds.dispatch_profile(
                self.root, self.project.name,
                "studio-storyboarder", "Clip plan")
            clip_command = popen.call_args.args[0]
            self.assertNotIn("-r", clip_command)
            clip_prompt = clip_command[clip_command.index("-q") + 1]
            self.assertIn("Conversation scope: clip", clip_prompt)
            self.assertIn("Active clip id: clip-001", clip_prompt)

            ds.dispatch_profile(
                self.root, self.project.name,
                "studio-storyboarder", "Revise clip")
            resumed_command = popen.call_args.args[0]
            self.assertIn("-r", resumed_command)
            self.assertIn("clip-session", resumed_command)
            self.assertNotIn("project-session", resumed_command)

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
