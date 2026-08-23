import json
import hashlib
import os
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

    def test_create_clip_rejects_unmanifested_existing_target(self):
        self.store.initialize(self.project, "Test project")
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        target = self.project / "clips" / "clip-002"
        target.mkdir()

        with self.assertRaisesRegex(ClipStoreError, "already exists"):
            self.store.create_clip(self.project, "Second scene")

        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_create_clip_collision_at_publication_preserves_target_and_manifest(self):
        self.store.initialize(self.project, "Test project")
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        target = self.project / "clips" / "clip-002"
        real_publish = clip_store.atomic_publish_directory

        def collide_at_publication(source, destination):
            destination.mkdir()
            return real_publish(source, destination)

        with (
            patch.object(
                clip_store, "atomic_publish_directory",
                side_effect=collide_at_publication,
            ),
            self.assertRaisesRegex(ClipStoreError, "already exists"),
        ):
            self.store.create_clip(self.project, "Second scene")

        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])

    def test_create_clip_manifest_failure_rolls_back_published_directory(self):
        self.store.initialize(self.project, "Test project")
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        target = self.project / "clips" / "clip-002"

        with (
            patch.object(
                self.store, "_write_manifest_unlocked",
                side_effect=RuntimeError("injected manifest failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected manifest failure"),
        ):
            self.store.create_clip(self.project, "Second scene")

        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertFalse(os.path.lexists(target))

    def test_create_clip_rollback_does_not_remove_replacement_directory(self):
        self.store.initialize(self.project, "Test project")
        target = self.project / "clips" / "clip-002"
        displaced = self.project / "displaced-published-clip"

        def replace_then_fail(_project, _manifest):
            target.rename(displaced)
            target.mkdir()
            raise RuntimeError("injected manifest failure")

        with (
            patch.object(
                self.store, "_write_manifest_unlocked",
                side_effect=replace_then_fail,
            ),
            self.assertRaisesRegex(RuntimeError, "injected manifest failure"),
        ):
            self.store.create_clip(self.project, "Second scene")

        self.assertTrue(target.is_dir())
        self.assertEqual(list(target.iterdir()), [])
        self.assertTrue((displaced / "current_prompt.txt").is_file())

    def test_create_clip_source_swap_is_not_blessed_or_deleted(self):
        self.store.initialize(self.project, "Test project")
        manifest_path = self.project / "project.json"
        original_manifest = manifest_path.read_bytes()
        target = self.project / "clips" / "clip-002"
        displaced = self.project / "clips" / ".displaced-clip-source"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        swapped = False

        def swap_source(parent_fd, source_name, destination_fd,
                        destination_name, flags):
            nonlocal swapped
            source = os.fsdecode(source_name)
            if not swapped and source.startswith(".creating-"):
                swapped = True
                os.rename(source, displaced.name,
                          src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.mkdir(source, dir_fd=parent_fd)
                replacement_fd = os.open(
                    source, os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
                try:
                    marker_fd = os.open(
                        "replacement.txt",
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=replacement_fd,
                    )
                    try:
                        os.write(marker_fd, b"replacement")
                    finally:
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
            self.store.create_clip(self.project, "Second scene")

        self.assertTrue(swapped)
        self.assertEqual(manifest_path.read_bytes(), original_manifest)
        self.assertEqual((target / "replacement.txt").read_bytes(), b"replacement")
        self.assertTrue((displaced / "current_prompt.txt").is_file())

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

    def test_concurrent_manifest_readers_only_observe_complete_old_or_new_json(self):
        self.store.initialize(self.project, "Test project")
        old = self.store.describe(self.project)
        new = json.loads(json.dumps(old))
        new["clips"][0]["title"] = "New title"
        publication_entered = threading.Barrier(2)
        old_read = threading.Barrier(2)
        publication_finished = threading.Barrier(2)
        new_read = threading.Barrier(2)
        writer_returned = threading.Event()
        real_replace = Path.replace

        def publish_between_barriers(temp, target):
            publication_entered.wait(timeout=2)
            old_read.wait(timeout=2)
            result = real_replace(temp, target)
            publication_finished.wait(timeout=2)
            new_read.wait(timeout=2)
            return result

        def read_while_writing():
            publication_entered.wait(timeout=2)
            observed_old = self.store.describe(self.project)
            self.assertFalse(writer_returned.is_set())
            old_read.wait(timeout=2)
            publication_finished.wait(timeout=2)
            observed_new = self.store.describe(self.project)
            self.assertFalse(writer_returned.is_set())
            new_read.wait(timeout=2)
            return [observed_old, observed_new]

        def publish_manifest():
            try:
                self.store._write_manifest_unlocked(self.project, new)
            finally:
                writer_returned.set()

        with (
            patch.object(Path, "replace", autospec=True,
                         side_effect=publish_between_barriers),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            reader = pool.submit(read_while_writing)
            writer = pool.submit(publish_manifest)
            observed = reader.result(timeout=5)
            writer.result(timeout=5)

        self.assertEqual(observed, [old, new])
        self.assertTrue(writer_returned.is_set())

    def test_manifest_swap_at_descriptor_open_does_not_read_symlink_target(self):
        self.store.initialize(self.project, "Test project")
        manifest = self.project / "project.json"
        outside = Path(self.temp.name) / "outside-manifest.json"
        outside.write_text(manifest.read_text(encoding="utf-8"), encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_at_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "project.json":
                swapped = True
                manifest.unlink()
                manifest.symlink_to(outside)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open", side_effect=swap_at_open),
            self.assertRaisesRegex(ClipStoreError, "missing or unsafe"),
        ):
            self.store.describe(self.project)
        self.assertTrue(swapped)

    def test_manifest_parent_swap_at_final_open_fails_closed(self):
        self.store.initialize(self.project, "Test project")
        displaced = Path(self.temp.name) / "displaced-project"
        outside = Path(self.temp.name) / "outside-project"
        outside.mkdir()
        (outside / "project.json").write_text(
            json.dumps({"outside": "secret"}), encoding="utf-8")
        real_open = os.open
        swapped = False

        def swap_parent_at_final_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            if not swapped and Path(path).name == "project.json":
                swapped = True
                self.project.rename(displaced)
                self.project.symlink_to(outside, target_is_directory=True)
            return real_open(path, flags, mode, dir_fd=dir_fd)

        with (
            patch("webapp.safe_files.os.open",
                  side_effect=swap_parent_at_final_open),
            self.assertRaisesRegex(ClipStoreError, "missing or unsafe"),
        ):
            self.store.describe(self.project)

        self.assertTrue(swapped)
        self.assertEqual(
            json.loads((outside / "project.json").read_text()),
            {"outside": "secret"},
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


class SafeFilesystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.parent = Path(self.temp.name) / "publication"
        self.parent.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def test_atomic_publication_rejects_swapped_source_identity(self):
        source = self.parent / "source"
        source.mkdir()
        (source / "original.txt").write_text("original")
        destination = self.parent / "destination"
        displaced = self.parent / "displaced-source"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None

        def swap_source(parent_fd, source_name, destination_fd,
                        destination_name, flags):
            os.rename("source", "displaced-source",
                      src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir("source", dir_fd=parent_fd)
            replacement_fd = os.open(
                "source", os.O_RDONLY | os.O_DIRECTORY, dir_fd=parent_fd)
            try:
                marker_fd = os.open(
                    "replacement.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=replacement_fd,
                )
                try:
                    os.write(marker_fd, b"replacement")
                finally:
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
            safe_files.atomic_publish_directory(source, destination)

        self.assertEqual((displaced / "original.txt").read_text(), "original")
        self.assertEqual(
            (destination / "replacement.txt").read_text(), "replacement")

    def test_atomic_move_refuses_to_replace_existing_target(self):
        source = self.parent / "source.txt"
        destination = self.parent / "destination.txt"
        source.write_bytes(b"source")
        destination.write_bytes(b"destination")

        with self.assertRaises(FileExistsError):
            safe_files.atomic_move_no_replace(source, destination)

        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(destination.read_bytes(), b"destination")

    def test_atomic_move_does_not_follow_symlink_source(self):
        outside = self.parent / "outside.txt"
        source = self.parent / "source.txt"
        destination = self.parent / "destination.txt"
        outside.write_bytes(b"outside")
        source.symlink_to(outside)

        with self.assertRaises(safe_files.SafeFilesystemError):
            safe_files.atomic_move_no_replace(source, destination)

        self.assertTrue(source.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside")
        self.assertFalse(destination.exists())

    def test_atomic_move_fsyncs_both_distinct_parent_directories(self):
        source_parent = self.parent / "source-parent"
        destination_parent = self.parent / "destination-parent"
        source_parent.mkdir()
        destination_parent.mkdir()
        source = source_parent / "source.txt"
        destination = destination_parent / "destination.txt"
        source.write_bytes(b"source")
        expected = [
            (source_parent.stat().st_dev, source_parent.stat().st_ino),
            (destination_parent.stat().st_dev, destination_parent.stat().st_ino),
        ]
        fsynced = []

        def record_fsync(descriptor):
            details = os.fstat(descriptor)
            fsynced.append((details.st_dev, details.st_ino))

        with patch.object(
                safe_files, "_fsync_directory",
                side_effect=record_fsync):
            safe_files.atomic_move_no_replace(source, destination)

        self.assertEqual(fsynced, expected)
        self.assertFalse(source.exists())
        self.assertEqual(destination.read_bytes(), b"source")

    def test_rollback_quarantine_preserves_moved_original_and_replacement(self):
        source = self.parent / "source"
        source.mkdir()
        (source / "original.txt").write_text("original")
        destination = self.parent / "destination"
        identity = safe_files.atomic_publish_directory(source, destination)
        displaced = self.parent / "displaced-publication"
        real_open_directory = safe_files._open_directory
        swapped = False

        def swap_quarantine_before_verification(path, *, dir_fd=None):
            nonlocal swapped
            name = os.fsdecode(path)
            if (not swapped and dir_fd is not None
                    and ".rollback-" in name):
                swapped = True
                os.rename(name, displaced.name,
                          src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                os.mkdir(name, dir_fd=dir_fd)
                replacement_fd = os.open(
                    name, os.O_RDONLY | os.O_DIRECTORY, dir_fd=dir_fd)
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
            return real_open_directory(path, dir_fd=dir_fd)

        with patch.object(
                safe_files, "_open_directory",
                side_effect=swap_quarantine_before_verification):
            removed = safe_files.remove_published_directory_if_same(
                destination, identity)

        self.assertFalse(removed)
        self.assertTrue(swapped)
        self.assertEqual((displaced / "original.txt").read_text(), "original")
        quarantines = list(self.parent.glob("*.rollback-*"))
        self.assertEqual(len(quarantines), 1)
        self.assertTrue((quarantines[0] / "replacement.txt").is_file())


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


class LegacyClipMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = ds.studio_root(self.temp.name)
        self.runtime = Path(self.temp.name) / "runtime"

    def tearDown(self):
        self.temp.cleanup()

    def legacy_project(self, project_id="2026-08-23_legacy", *, settings=True):
        project = self.root / "projects" / project_id
        project.mkdir()
        (project / "brief.md").write_text("# Legacy\n", encoding="utf-8")
        (project / "chat.jsonl").write_text(
            '{"role":"user","content":"keep"}\n', encoding="utf-8")
        for name in ("references", "research", "final"):
            (project / name).mkdir()
        (project / "references" / "guide.png").write_bytes(b"reference")
        (project / "current_prompt.txt").write_bytes(b"legacy prompt\n")
        if settings:
            (project / "current_generation.json").write_bytes(
                b'{"schema_version":2,"seed":42}\n')
        generation = project / "generations" / "001"
        generation.mkdir(parents=True)
        (generation / "take.mp4").write_bytes(b"\x00video\xff")
        (generation / "take.mp4.review.json").write_bytes(
            b'{"verdict":"keep"}\n')
        (generation / "prompt.txt").write_bytes(b"archived prompt\n")
        empty = project / "generations" / "002" / "empty"
        empty.mkdir(parents=True)
        return project

    @staticmethod
    def snapshot(path):
        result = []
        for entry in sorted(path.rglob("*")):
            relative = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                result.append((relative, "symlink", os.readlink(entry)))
            elif entry.is_dir():
                result.append((relative, "directory"))
            else:
                result.append((
                    relative, "file", entry.stat().st_size,
                    hashlib.sha256(entry.read_bytes()).hexdigest(),
                    entry.stat().st_mtime_ns,
                ))
        return result

    @staticmethod
    def file_inventory(path):
        return {
            entry.relative_to(path).as_posix(): (
                entry.stat().st_size,
                hashlib.sha256(entry.read_bytes()).hexdigest(),
            )
            for entry in path.rglob("*") if entry.is_file()
        }

    @staticmethod
    def metadata_snapshot(path):
        result = []
        for entry in [path, *sorted(path.rglob("*"))]:
            details = entry.lstat()
            result.append((
                entry.relative_to(path).as_posix(),
                details.st_mode,
                details.st_dev,
                details.st_ino,
                details.st_nlink,
                details.st_size,
                details.st_mtime_ns,
                details.st_ctime_ns,
            ))
        return result

    def migrate(self, project=None, *, apply=False):
        with patch.dict(os.environ, {
            "HERMES_STUDIO_RUNTIME_ROOT": str(self.runtime),
        }):
            return ds.migrate_clips(self.root, project, apply=apply)

    def test_dry_run_is_deterministic_machine_readable_and_writes_nothing(self):
        project = self.legacy_project()
        before = self.snapshot(self.root)
        first = self.migrate(project.name)
        second = self.migrate(project.name)
        self.assertEqual(first, second)
        self.assertEqual(first["mode"], "dry-run")
        self.assertEqual(first["projects"][0]["status"], "planned")
        self.assertEqual(
            [operation["source"] for operation in
             first["projects"][0]["operations"]],
            ["current_prompt.txt", "current_generation.json", "generations"],
        )
        self.assertGreater(first["projects"][0]["file_count"], 4)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())
        self.assertFalse((project / ".project.lock").exists())

    def test_apply_preserves_every_byte_and_publishes_default_manifest(self):
        project = self.legacy_project()
        before = self.file_inventory(project)
        project_level = {
            name: (project / name).read_bytes()
            for name in ("brief.md", "chat.jsonl")
        }
        report = self.migrate(project.name, apply=True)
        clip = project / "clips" / "clip-001"
        self.assertEqual(report["projects"][0]["status"], "migrated")
        manifest = json.loads((project / "project.json").read_text())
        self.assertEqual(manifest, {
            "schema_version": 1,
            "title": project.name,
            "clips": [{
                "id": "clip-001", "title": "Main clip",
                "enabled": True, "selected_take": None,
            }],
        })
        expected = {}
        for relative, value in before.items():
            if relative == "current_prompt.txt":
                expected["current_prompt.txt"] = value
            elif relative == "current_generation.json":
                expected["current_generation.json"] = value
            elif relative.startswith("generations/"):
                expected[relative] = value
        self.assertEqual(self.file_inventory(clip), expected)
        for name, content in project_level.items():
            self.assertEqual((project / name).read_bytes(), content)
        self.assertTrue((project / "references" / "guide.png").is_file())
        self.assertTrue((clip / "generations" / "002" / "empty").is_dir())
        self.assertFalse((project / "current_prompt.txt").exists())
        self.assertFalse((project / "current_generation.json").exists())
        self.assertFalse((project / "generations").exists())
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())

    def test_optional_settings_and_all_project_selection(self):
        first = self.legacy_project("2026-08-23_first", settings=False)
        second = self.legacy_project("2026-08-23_second")
        current = self.root / "projects" / "2026-08-23_current"
        current.mkdir()
        ClipStore().initialize(current, "Current")
        report = self.migrate(apply=True)
        self.assertEqual(
            [(item["project"], item["status"]) for item in report["projects"]],
            [(current.name, "already-migrated"),
             (first.name, "migrated"), (second.name, "migrated")],
        )
        self.assertFalse((first / "clips" / "clip-001" /
                          "current_generation.json").exists())

    def test_apply_is_idempotent_and_second_run_performs_no_writes(self):
        project = self.legacy_project()
        self.migrate(project.name, apply=True)
        (project / ".project.lock").unlink()
        before = self.metadata_snapshot(project)
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "already-migrated")
        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertFalse((project / ".project.lock").exists())

    def test_interrupted_apply_resumes_after_every_checkpoint(self):
        checkpoints = [
            "journal-prepared",
            "directories-created",
            "moved:current_prompt.txt",
            "moved:current_generation.json",
            "moved:generations",
            "targets-verified",
            "manifest-published",
            "journal-manifest-published",
        ]
        for index, checkpoint in enumerate(checkpoints):
            with self.subTest(checkpoint=checkpoint):
                project = self.legacy_project(f"2026-08-23_resume-{index}")
                expected = {
                    relative: value for relative, value in
                    self.file_inventory(project).items()
                    if (relative in {
                        "current_prompt.txt", "current_generation.json"}
                        or relative.startswith("generations/"))
                }

                def interrupt(observed):
                    if observed == checkpoint:
                        raise RuntimeError(f"interrupted at {checkpoint}")

                with (
                    patch.object(ds, "_migration_checkpoint",
                                 side_effect=interrupt),
                    self.assertRaisesRegex(RuntimeError, "interrupted"),
                ):
                    self.migrate(project.name, apply=True)
                report = self.migrate(project.name, apply=True)
                self.assertEqual(report["projects"][0]["status"], "migrated")
                self.assertEqual(
                    self.file_inventory(project / "clips" / "clip-001"),
                    expected,
                )
                self.assertFalse(
                    (project / ds.CLIP_MIGRATION_JOURNAL).exists())

    def test_refuses_unsafe_sources_destinations_and_nested_entries(self):
        cases = ("prompt", "settings", "generations", "nested", "clips",
                 "manifest", "journal")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                project = self.legacy_project(f"2026-08-23_unsafe-{index}")
                outside = Path(self.temp.name) / f"outside-{index}"
                outside.write_bytes(b"outside")
                if case == "prompt":
                    (project / "current_prompt.txt").unlink()
                    (project / "current_prompt.txt").symlink_to(outside)
                elif case == "settings":
                    (project / "current_generation.json").unlink()
                    (project / "current_generation.json").symlink_to(outside)
                elif case == "generations":
                    os.rename(project / "generations", project / "real-generations")
                    (project / "generations").symlink_to(
                        project / "real-generations", target_is_directory=True)
                elif case == "nested":
                    (project / "generations" / "001" / "linked").symlink_to(outside)
                elif case == "clips":
                    outside.unlink()
                    outside.mkdir()
                    (project / "clips").symlink_to(outside, target_is_directory=True)
                else:
                    filename = ("project.json" if case == "manifest" else
                                ds.CLIP_MIGRATION_JOURNAL)
                    (project / filename).symlink_to(outside)
                before = self.snapshot(project)
                with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
                    self.migrate(project.name, apply=case != "journal")
                self.assertEqual(self.snapshot(project), before)
                self.assertEqual(outside.read_bytes() if outside.is_file() else
                                 list(outside.iterdir()),
                                 b"outside" if outside.is_file() else [])

    def _prepare_journal(self, project):
        def interrupt(checkpoint):
            if checkpoint == "journal-prepared":
                raise RuntimeError("stop")
        with (
            patch.object(ds, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaises(RuntimeError),
        ):
            self.migrate(project.name, apply=True)

    def test_rename_fsync_failure_does_not_advance_journal_and_resumes(self):
        project = self.legacy_project()
        self._prepare_journal(project)
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL
        original_journal = journal_path.read_bytes()
        fsynced = []

        def fail_destination_fsync(descriptor):
            details = os.fstat(descriptor)
            fsynced.append((details.st_dev, details.st_ino))
            if len(fsynced) == 2:
                raise OSError("injected destination parent fsync failure")

        with (
            patch.object(
                safe_files, "_fsync_directory",
                side_effect=fail_destination_fsync,
            ),
            self.assertRaises(safe_files.SafeFilesystemError),
        ):
            self.migrate(project.name, apply=True)

        target = project / "clips" / "clip-001" / "current_prompt.txt"
        self.assertEqual(len(fsynced), 2)
        self.assertFalse((project / "current_prompt.txt").exists())
        self.assertEqual(target.read_bytes(), b"legacy prompt\n")
        self.assertEqual(journal_path.read_bytes(), original_journal)
        self.assertEqual(json.loads(original_journal)["completed"], [])

        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertEqual(target.read_bytes(), b"legacy prompt\n")
        self.assertFalse(journal_path.exists())

    def test_destination_creation_fails_closed_during_clips_symlink_swap(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-destination"
        outside.mkdir()
        clips = project / "clips"
        displaced = project / "displaced-clips"
        real_mkdir = os.mkdir
        swapped = False

        def swap_clips_before_clip_creation(path, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            name = os.fsdecode(path)
            if (not swapped
                    and (name == "clip-001" or Path(name) == clips / "clip-001")):
                swapped = True
                os.rename(clips, displaced)
                clips.symlink_to(outside, target_is_directory=True)
            return real_mkdir(path, mode, dir_fd=dir_fd)

        with (
            patch.object(ds.os, "mkdir", side_effect=swap_clips_before_clip_creation),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((project / ds.CLIP_MIGRATION_JOURNAL).is_file())
        self.assertTrue((project / "current_prompt.txt").is_file())

    def test_resume_accepts_existing_safe_destination_directories(self):
        project = self.legacy_project()
        self._prepare_journal(project)
        (project / "clips" / "clip-001").mkdir(parents=True)

        report = self.migrate(project.name, apply=True)

        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertEqual(
            (project / "clips" / "clip-001" / "current_prompt.txt").read_bytes(),
            b"legacy prompt\n",
        )
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())

    def test_resume_fails_closed_when_source_and_target_both_exist(self):
        project = self.legacy_project()
        self._prepare_journal(project)
        target = project / "clips" / "clip-001" / "current_prompt.txt"
        target.parent.mkdir(parents=True)
        target.write_bytes((project / "current_prompt.txt").read_bytes())
        before = self.snapshot(project)
        with self.assertRaisesRegex(ValueError, "both source and target"):
            self.migrate(project.name, apply=True)
        self.assertEqual(self.snapshot(project), before)

    def test_resume_fails_closed_when_source_and_target_are_both_missing(self):
        project = self.legacy_project()
        self._prepare_journal(project)
        (project / "current_prompt.txt").unlink()
        before = self.snapshot(project)
        with self.assertRaisesRegex(ValueError, "neither source nor target"):
            self.migrate(project.name, apply=True)
        self.assertEqual(self.snapshot(project), before)

    def test_active_jobs_are_reported_on_dry_run_and_block_all_apply_writes(self):
        first = self.legacy_project("2026-08-23_active")
        second = self.legacy_project("2026-08-23_inactive")
        self.runtime.mkdir()
        database = self.runtime / "studio.db"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute(
                "CREATE TABLE jobs (id TEXT, project TEXT, status TEXT)")
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)",
                ("job-1", first.name, "running"),
            )
            connection.commit()
        runtime_before = self.snapshot(self.runtime)
        dry_run = self.migrate()
        self.assertEqual(dry_run["projects"][0]["active_jobs"], [{
            "id": "job-1", "status": "running",
        }])
        before = self.snapshot(self.root)
        with self.assertRaisesRegex(ValueError, "active jobs"):
            self.migrate(apply=True)
        self.assertEqual(self.snapshot(self.root), before)
        self.assertEqual(self.snapshot(self.runtime), runtime_before)
        self.assertFalse((second / ".project.lock").exists())

    def test_missing_database_or_jobs_table_is_not_initialized(self):
        project = self.legacy_project()
        self.runtime.mkdir()
        database = self.runtime / "studio.db"
        before = self.snapshot(self.runtime)
        self.migrate(project.name)
        self.assertEqual(self.snapshot(self.runtime), before)
        database.touch()
        before = self.snapshot(self.runtime)
        self.migrate(project.name)
        self.assertEqual(self.snapshot(self.runtime), before)

    def test_cli_requires_exactly_one_mode_and_prints_json(self):
        project = self.legacy_project()
        for arguments in (("migrate-clips", project.name),
                          ("migrate-clips", "--dry-run", "--apply",
                           project.name)):
            with self.subTest(arguments=arguments), self.assertRaises(SystemExit):
                ds.main(["--root", str(self.root), *arguments])
        output = StringIO()
        with (
            patch.dict(os.environ, {
                "HERMES_STUDIO_RUNTIME_ROOT": str(self.runtime)}),
            redirect_stdout(output),
        ):
            self.assertEqual(ds.main([
                "--root", str(self.root), "migrate-clips", "--dry-run",
                project.name,
            ]), 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "dry-run")

        absent_root = Path(self.temp.name) / "absent-studio-root"
        output = StringIO()
        with (
            patch.dict(os.environ, {
                "HERMES_STUDIO_RUNTIME_ROOT": str(self.runtime)}),
            redirect_stdout(output),
        ):
            self.assertEqual(ds.main([
                "--root", str(absent_root), "migrate-clips", "--dry-run",
            ]), 0)
        self.assertFalse(absent_root.exists())
        self.assertEqual(json.loads(output.getvalue())["projects"], [])

    def test_existing_commands_do_not_silently_migrate_legacy_layout(self):
        project = self.legacy_project()
        before = self.snapshot(project)
        with self.assertRaisesRegex(ClipStoreError, "manifest"):
            ds.main(["--root", str(self.root), "list-clips", project.name])
        self.assertEqual(self.snapshot(project), before)


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
