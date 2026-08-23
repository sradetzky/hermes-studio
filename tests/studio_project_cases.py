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
from webapp import clip_store, safe_files
from webapp.clip_store import ClipStore, ClipStoreError
from webapp.job_store import JobStore


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

        with patch("webapp.safe_files.os.open",
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
            patch("webapp.safe_files.os.open",
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

    def _assert_archive_swap_at_open_is_rejected(self, filename, source, outside):
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == filename:
                swapped = True
                source.unlink()
                source.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source.parent if filename == "result.png" else outside.parent,
            )
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )

    def test_archive_media_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "media-swap"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        outside = Path(self.temp.name) / "outside-media.bin"
        outside.write_bytes(b"SECRET")
        self._assert_archive_swap_at_open_is_rejected(
            "result.png", source, outside)

    def test_archive_prompt_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "prompt-swap"
        source_root.mkdir()
        media = source_root / "result.png"
        media.write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        prompt = clip / "current_prompt.txt"
        prompt.write_text("safe prompt\n", encoding="utf-8")
        outside = Path(self.temp.name) / "outside-prompt.txt"
        outside.write_bytes(b"SECRET")
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_prompt.txt":
                swapped = True
                prompt.unlink()
                prompt.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")

    def test_archive_settings_swap_at_descriptor_open_is_rejected(self):
        source_root = Path(self.temp.name) / "settings-swap"
        source_root.mkdir()
        media = source_root / "result.png"
        media.write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        settings = clip / "current_generation.json"
        settings.write_text('{"seed": 1}\n', encoding="utf-8")
        outside = Path(self.temp.name) / "outside-settings.json"
        outside.write_bytes(b"SECRET")
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_generation.json":
                swapped = True
                settings.unlink()
                settings.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)
        self.assertTrue(swapped)
        self.assertEqual(outside.read_bytes(), b"SECRET")

    def test_archive_settings_parent_swap_does_not_copy_outside(self):
        source_root = Path(self.temp.name) / "settings-parent-swap"
        source_root.mkdir()
        (source_root / "result.png").write_bytes(b"safe image")
        clip = self.project / "clips" / "clip-001"
        (clip / "current_generation.json").write_text(
            '{"seed": 1}\n', encoding="utf-8")
        displaced = Path(self.temp.name) / "displaced-settings-clip"
        outside = Path(self.temp.name) / "outside-settings-clip"
        outside.mkdir()
        (outside / "current_generation.json").write_text(
            '{"outside": "secret"}\n', encoding="utf-8")
        real_open = os.open
        swapped = False
        copied_settings = []
        real_copy = ds.copy_opened_file

        def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "current_generation.json":
                swapped = True
                clip.rename(displaced)
                clip.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        def inspect_copy(source, target):
            if source.name == "current_generation.json":
                copied_settings.append(os.pread(source.descriptor, 4096, 0))
            return real_copy(source, target)

        with (
            patch("webapp.safe_files.os.open",
                  side_effect=swap_parent_at_final_open),
            patch.object(ds, "copy_opened_file", side_effect=inspect_copy),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertNotIn(b'{"outside": "secret"}\n', copied_settings)
        self.assertEqual(
            (outside / "current_generation.json").read_text(),
            '{"outside": "secret"}\n',
        )

    def test_archive_publication_rejects_swapped_staging_identity(self):
        source_root = Path(self.temp.name) / "archive-publication-swap"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        generations = self.project / "clips" / "clip-001" / "generations"
        target = generations / "001"
        displaced = generations / ".displaced-archive-source"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        swapped = False

        def swap_source(parent_fd, source_name, destination_fd,
                        destination_name, flags):
            nonlocal swapped
            name = os.fsdecode(source_name)
            if not swapped and name.startswith(".publishing-"):
                swapped = True
                os.rename(name, displaced.name,
                          src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(name, dir_fd=parent_fd)
                replacement_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
                try:
                    marker_fd = os.open(
                        "replacement.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=replacement_fd,
                    )
                    os.close(marker_fd)
                finally:
                    os.close(replacement_fd)
            return real_renameat2(
                parent_fd, source_name, destination_fd, destination_name, flags)

        with (
            patch.object(safe_files, "_renameat2", side_effect=swap_source),
            self.assertRaisesRegex(safe_files.SafeFilesystemError,
                                   "publication identity"),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertEqual(source.read_bytes(), b"safe image")
        self.assertTrue((target / "replacement.txt").is_file())
        self.assertEqual((displaced / "result.png").read_bytes(), b"safe image")

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

    def test_archive_rejects_symlinked_output_directory_component(self):
        comfy_output = Path(self.temp.name) / "nested-comfy-output"
        comfy_output.mkdir()
        outside = Path(self.temp.name) / "outside-output-directory"
        outside.mkdir()
        (outside / "secret.png").write_bytes(b"SECRET")
        (comfy_output / "linked").symlink_to(
            outside, target_is_directory=True)

        with self.assertRaisesRegex(ValueError, "symlink"):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001",
                ["linked/secret.png"], source_root=comfy_output)
        self.assertEqual((outside / "secret.png").read_bytes(), b"SECRET")

    def test_archive_rejects_swapped_media_root_parent(self):
        media_parent = Path(self.temp.name) / "media-parent"
        source_root = media_parent / "source"
        source_root.mkdir(parents=True)
        (source_root / "result.png").write_bytes(b"safe image")
        displaced = Path(self.temp.name) / "displaced-media-parent"
        outside_parent = Path(self.temp.name) / "outside-media-parent"
        outside_root = outside_parent / "source"
        outside_root.mkdir(parents=True)
        (outside_root / "result.png").write_bytes(b"OUTSIDE")
        real_open = os.open
        swapped = False

        def swap_parent_before_root_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            component_walk = dir_fd is not None and os.fsdecode(path) == media_parent.name
            if not swapped and (Path(path) == source_root or component_walk):
                swapped = True
                media_parent.rename(displaced)
                media_parent.symlink_to(outside_parent, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open",
                  side_effect=swap_parent_before_root_open),
            self.assertRaises(ValueError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertTrue(swapped)
        self.assertEqual((outside_root / "result.png").read_bytes(), b"OUTSIDE")
        self.assertEqual(
            list((self.project / "clips" / "clip-001" / "generations").iterdir()),
            [],
        )

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
        real_copy = ds.copy_opened_file

        def inspect_copy(source_path, target_path):
            visible_during_copy.append([
                item.name for item in (
                    self.project / "clips" / "clip-001" / "generations").iterdir()
                if item.name.isdigit()
            ])
            return real_copy(source_path, target_path)

        with patch.object(ds, "copy_opened_file", side_effect=inspect_copy):
            generation = ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=comfy_output)
        self.assertEqual(visible_during_copy, [[]])
        self.assertEqual(generation.name, "001")
        self.assertTrue((generation / "meta.json").is_file())

    def test_archive_collision_at_publication_preserves_destination_and_source(self):
        source_root = Path(self.temp.name) / "publication-collision"
        source_root.mkdir()
        source = source_root / "result.png"
        source.write_bytes(b"safe image")
        generations = self.project / "clips" / "clip-001" / "generations"
        target = generations / "001"
        real_publish = ds.atomic_publish_directory

        def collide_at_publication(staging, destination):
            destination.mkdir()
            return real_publish(staging, destination)

        with (
            patch.object(
                ds, "atomic_publish_directory",
                side_effect=collide_at_publication,
            ),
            self.assertRaises(FileExistsError),
        ):
            ds.archive_outputs(
                self.root, self.project.name, "clip-001", ["result.png"],
                source_root=source_root)

        self.assertEqual(source.read_bytes(), b"safe image")
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertEqual(
            [path for path in generations.iterdir() if path.name.startswith(".publishing-")],
            [],
        )

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
