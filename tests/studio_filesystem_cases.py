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

    def test_updates_project_title_and_brief_without_changing_identity(self):
        self.store.initialize(self.project, "Test project")
        (self.project / "brief.md").write_text(
            "Original brief", encoding="utf-8")

        metadata = self.store.update_project_metadata(
            self.project, title="Renamed project", brief="Revised **brief**")

        self.assertEqual(metadata["title"], "Renamed project")
        self.assertEqual(metadata["brief"], "Revised **brief**")
        self.assertEqual(metadata["clips"][0]["id"], "clip-001")
        self.assertEqual(self.project.name, "project")
        self.assertEqual(
            json.loads((self.project / "project.json").read_text())["title"],
            "Renamed project",
        )
        self.assertEqual(
            (self.project / "brief.md").read_text(encoding="utf-8"),
            "Revised **brief**",
        )

    def test_project_metadata_validation_rejects_empty_title_and_large_brief(self):
        self.store.initialize(self.project, "Test project")
        (self.project / "brief.md").write_text("Original", encoding="utf-8")

        with self.assertRaisesRegex(ClipStoreError, "project title"):
            self.store.update_project_metadata(
                self.project, title="   ", brief="Still valid")
        with self.assertRaisesRegex(ClipStoreError, "project brief"):
            self.store.update_project_metadata(
                self.project, title="Valid", brief="x" * 100_001)

    def test_project_metadata_manifest_failure_restores_original_brief(self):
        self.store.initialize(self.project, "Test project")
        brief = self.project / "brief.md"
        brief.write_text("Original brief", encoding="utf-8")
        original_manifest = (self.project / "project.json").read_bytes()

        with (
            patch.object(
                self.store, "_write_manifest_unlocked",
                side_effect=RuntimeError("injected manifest failure"),
            ),
            self.assertRaisesRegex(RuntimeError, "injected manifest failure"),
        ):
            self.store.update_project_metadata(
                self.project, title="Renamed", brief="Replacement brief")

        self.assertEqual(brief.read_text(encoding="utf-8"), "Original brief")
        self.assertEqual(
            (self.project / "project.json").read_bytes(), original_manifest)

    def test_project_metadata_rejects_unsafe_brief_without_changing_manifest(self):
        self.store.initialize(self.project, "Test project")
        manifest = self.project / "project.json"
        original_manifest = manifest.read_bytes()
        outside = Path(self.temp.name) / "outside-brief.md"
        outside.write_text("Outside", encoding="utf-8")
        (self.project / "brief.md").symlink_to(outside)

        with self.assertRaisesRegex(ClipStoreError, "brief"):
            self.store.update_project_metadata(
                self.project, title="Renamed", brief="Replacement brief")

        self.assertEqual(manifest.read_bytes(), original_manifest)
        self.assertEqual(outside.read_text(encoding="utf-8"), "Outside")

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
        real_exchange = safe_files.atomic_exchange_regular_file_at

        def publish_between_barriers(*args, **kwargs):
            publication_entered.wait(timeout=2)
            old_read.wait(timeout=2)
            result = real_exchange(*args, **kwargs)
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
            patch.object(safe_files, "atomic_exchange_regular_file_at",
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
            patch("studio_core.safe_files.os.open", side_effect=swap_at_open),
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
            patch("studio_core.safe_files.os.open",
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

    def test_atomic_move_at_uses_retained_parent_after_path_swap(self):
        source = self.parent / "source.txt"
        source.write_bytes(b"source")
        source_identity = (source.stat().st_dev, source.stat().st_ino)
        retained_identity = (self.parent.stat().st_dev, self.parent.stat().st_ino)
        displaced = Path(self.temp.name) / "displaced-publication"
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        fsynced = []
        try:
            self.parent.rename(displaced)
            self.parent.mkdir()
            (self.parent / "replacement.txt").write_bytes(b"replacement")

            def record_fsync(descriptor):
                details = os.fstat(descriptor)
                fsynced.append((details.st_dev, details.st_ino))

            with patch.object(
                    safe_files, "_fsync_directory", side_effect=record_fsync):
                moved_identity = safe_files.atomic_move_no_replace_at(
                    parent_fd, "source.txt", "destination.txt",
                    expected_source_identity=source_identity)
        finally:
            os.close(parent_fd)

        self.assertEqual(moved_identity, source_identity)
        self.assertEqual(fsynced, [retained_identity])
        self.assertFalse((displaced / "source.txt").exists())
        self.assertEqual(
            (displaced / "destination.txt").read_bytes(), b"source")
        self.assertEqual(
            (self.parent / "replacement.txt").read_bytes(), b"replacement")
        self.assertFalse((self.parent / "destination.txt").exists())

    def test_atomic_move_at_refuses_existing_target(self):
        source = self.parent / "source.txt"
        destination = self.parent / "destination.txt"
        source.write_bytes(b"source")
        destination.write_bytes(b"destination")
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with self.assertRaises(FileExistsError):
                safe_files.atomic_move_no_replace_at(
                    parent_fd, source.name, destination.name)
        finally:
            os.close(parent_fd)

        self.assertEqual(source.read_bytes(), b"source")
        self.assertEqual(destination.read_bytes(), b"destination")

    def test_atomic_move_at_fails_closed_without_renameat2(self):
        source = self.parent / "source.txt"
        source.write_bytes(b"source")
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with (
                patch.object(safe_files, "_renameat2", None),
                self.assertRaises(safe_files.AtomicPublicationUnavailable),
            ):
                safe_files.atomic_move_no_replace_at(
                    parent_fd, "source.txt", "destination.txt")
        finally:
            os.close(parent_fd)

        self.assertEqual(source.read_bytes(), b"source")
        self.assertFalse((self.parent / "destination.txt").exists())

    def test_atomic_exchange_cas_publishes_new_file_and_removes_expected_old(self):
        temporary = self.parent / ".new.tmp"
        canonical = self.parent / "journal.json"
        temporary.write_bytes(b"new")
        canonical.write_bytes(b"old")
        new_identity = (temporary.stat().st_dev, temporary.stat().st_ino)
        old_identity = (canonical.stat().st_dev, canonical.stat().st_ino)
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            published = safe_files.atomic_exchange_regular_file_at(
                parent_fd, temporary.name, canonical.name,
                expected_source_identity=new_identity,
                expected_target_identity=old_identity,
                label="test journal",
            )
        finally:
            os.close(parent_fd)

        self.assertEqual(published, new_identity)
        self.assertEqual(canonical.read_bytes(), b"new")
        self.assertFalse(temporary.exists())

    def test_atomic_exchange_cas_rolls_back_target_replacement_and_preserves_both(self):
        temporary = self.parent / ".new.tmp"
        canonical = self.parent / "journal.json"
        displaced = self.parent / ".expected-old"
        temporary.write_bytes(b"new")
        canonical.write_bytes(b"old")
        new_identity = (temporary.stat().st_dev, temporary.stat().st_ino)
        old_identity = (canonical.stat().st_dev, canonical.stat().st_ino)
        replacement = b"replacement"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        injected = False

        def replace_before_exchange(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal injected
            if not injected and flags == safe_files._RENAME_EXCHANGE:
                injected = True
                os.rename(
                    canonical.name, displaced.name,
                    src_dir_fd=destination_fd, dst_dir_fd=destination_fd)
                descriptor = os.open(
                    canonical.name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=destination_fd,
                )
                try:
                    os.write(descriptor, replacement)
                finally:
                    os.close(descriptor)
            return real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)

        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with (
                patch.object(
                    safe_files, "_renameat2", side_effect=replace_before_exchange),
                self.assertRaisesRegex(
                    safe_files.SafeFilesystemError, "expected target identity"),
            ):
                safe_files.atomic_exchange_regular_file_at(
                    parent_fd, temporary.name, canonical.name,
                    expected_source_identity=new_identity,
                    expected_target_identity=old_identity,
                    label="test journal",
                )
        finally:
            os.close(parent_fd)

        self.assertTrue(injected)
        self.assertEqual(canonical.read_bytes(), replacement)
        self.assertEqual(temporary.read_bytes(), b"new")
        self.assertEqual(displaced.read_bytes(), b"old")

    def test_atomic_remove_regular_file_deletes_exact_identity_without_quarantine(self):
        target = self.parent / "target.txt"
        target.write_bytes(b"target")
        identity = (target.stat().st_dev, target.stat().st_ino)
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            safe_files.atomic_remove_regular_file_at(
                parent_fd, target.name, identity, label="test target")
        finally:
            os.close(parent_fd)

        self.assertFalse(target.exists())
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_atomic_remove_rejects_quarantine_collision_without_deleting(self):
        target = self.parent / "target.txt"
        target.write_bytes(b"target")
        identity = (target.stat().st_dev, target.stat().st_ino)
        collision = self.parent / (
            ".safe-delete-" + "0" * 32 + ".quarantine")
        collision.write_bytes(b"collision")
        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with (
                patch.object(
                    safe_files.uuid, "uuid4",
                    return_value=SimpleNamespace(hex="0" * 32),
                ),
                self.assertRaisesRegex(
                    safe_files.SafeFilesystemError, "quarantine name collided"),
            ):
                safe_files.atomic_remove_regular_file_at(
                    parent_fd, target.name, identity, label="test target")
        finally:
            os.close(parent_fd)

        self.assertEqual(target.read_bytes(), b"target")
        self.assertEqual(collision.read_bytes(), b"collision")

    def test_atomic_remove_mismatch_with_occupied_canonical_preserves_both(self):
        expected = self.parent / "expected.txt"
        target = self.parent / "target.txt"
        expected.write_bytes(b"expected")
        target.write_bytes(b"displaced replacement")
        expected_identity = (expected.stat().st_dev, expected.stat().st_ino)
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        rename_calls = 0

        def occupy_canonical_before_restore(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal rename_calls
            rename_calls += 1
            if rename_calls == 2:
                occupied_fd = os.open(
                    "target.txt",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=destination_fd,
                )
                try:
                    os.write(occupied_fd, b"canonical occupant")
                finally:
                    os.close(occupied_fd)
            return real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)

        parent_fd = os.open(
            self.parent,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with (
                patch.object(
                    safe_files, "_renameat2",
                    side_effect=occupy_canonical_before_restore,
                ),
                self.assertRaisesRegex(
                    safe_files.SafeFilesystemError, "identity changed"),
            ):
                safe_files.atomic_remove_regular_file_at(
                    parent_fd, target.name, expected_identity,
                    label="test target")
        finally:
            os.close(parent_fd)

        self.assertEqual(target.read_bytes(), b"canonical occupant")
        quarantines = [
            entry for entry in self.parent.iterdir()
            if entry.name not in {expected.name, target.name}
        ]
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(quarantines[0].read_bytes(), b"displaced replacement")
        self.assertEqual(expected.read_bytes(), b"expected")

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
