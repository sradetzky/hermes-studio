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
from studio_core import migration
from scripts import krea2_image
from scripts.krea2_image import parse_loras
from webapp import clip_store, safe_files
from webapp.clip_store import ClipStore, ClipStoreError
from webapp.job_store import JobStore


from tests.studio_migration_base import LegacyClipMigrationCase


class LegacyMigrationRestoreTests(LegacyClipMigrationCase):
    def test_restore_temp_replacement_at_cleanup_boundary_is_preserved(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL
        journal_path.write_bytes(b"canonical replacement")
        preserved = project / ".preserved-expected-restore-temp"
        replacement = b"replacement restore temp"
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        injected = False
        created_temporary = None

        def replace_before_cleanup_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal injected, created_temporary
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if source.endswith(".clip-migration.restore"):
                created_temporary = source
                if (not injected
                        and destination != migration.CLIP_MIGRATION_JOURNAL):
                    injected = True
                    os.rename(
                        source, preserved.name,
                        src_dir_fd=source_fd, dst_dir_fd=source_fd)
                    descriptor = os.open(
                        source,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                        0o600,
                        dir_fd=source_fd,
                    )
                    try:
                        os.write(descriptor, replacement)
                    finally:
                        os.close(descriptor)
            return real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)

        project_fd = os.open(
            project,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            with (
                patch.object(
                    safe_files, "_renameat2",
                    side_effect=replace_before_cleanup_quarantine,
                ),
                self.assertRaisesRegex(
                    safe_files.SafeFilesystemError, "restore temp identity changed"),
            ):
                migration._restore_migration_journal(project_fd, b"expected restore temp")
        finally:
            os.close(project_fd)

        self.assertTrue(injected)
        self.assertIsNotNone(created_temporary)
        assert created_temporary is not None
        self.assertEqual(journal_path.read_bytes(), b"canonical replacement")
        self.assertEqual((project / created_temporary).read_bytes(), replacement)
        self.assertEqual(preserved.read_bytes(), b"expected restore temp")
        self.assertEqual(
            [entry.name for entry in project.iterdir()
             if "safe-delete" in entry.name],
            [],
        )
    def test_rerun_preserves_same_content_separate_restore_named_file(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL

        def interrupt(checkpoint):
            if checkpoint == "journal-finalizing":
                raise RuntimeError("injected interruption before finalization")

        with (
            patch.object(migration, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "before finalization"),
        ):
            self.migrate(project.name, apply=True)

        journal_bytes = journal_path.read_bytes()
        user_file = project / ("." + "b" * 32 + ".clip-migration.restore")
        user_bytes = journal_bytes
        user_file.write_bytes(user_bytes)

        with self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "unrecognized"):
            self.migrate(project.name, apply=True)

        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertEqual(user_file.read_bytes(), user_bytes)
    def test_rerun_preserves_symlink_fifo_and_unrecognized_restore_names(self):
        cases = ("symlink", "fifo", "unrecognized")
        for index, case in enumerate(cases):
            with self.subTest(case=case):
                project = self.legacy_project(
                    f"2026-08-23_restore-artifact-{index}")
                journal_path = project / migration.CLIP_MIGRATION_JOURNAL

                def interrupt(checkpoint):
                    if checkpoint == "journal-finalizing":
                        raise RuntimeError("injected interruption")

                with (
                    patch.object(
                        migration, "_migration_checkpoint", side_effect=interrupt),
                    self.assertRaisesRegex(RuntimeError, "interruption"),
                ):
                    self.migrate(project.name, apply=True)

                if case == "unrecognized":
                    artifact = project / (
                        "." + "A" * 32 + ".clip-migration.restore")
                    os.link(journal_path, artifact)
                    report = self.migrate(project.name, apply=True)
                    self.assertEqual(
                        report["projects"][0]["status"], "migrated")
                    self.assertTrue(artifact.is_file())
                    continue

                artifact = project / (
                    "." + str(index) * 32 + ".clip-migration.restore")
                if case == "symlink":
                    artifact.symlink_to(journal_path)
                else:
                    os.mkfifo(artifact)
                before = artifact.lstat()
                with self.assertRaisesRegex(
                        safe_files.SafeFilesystemError, "unrecognized"):
                    self.migrate(project.name, apply=True)
                after = artifact.lstat()
                self.assertEqual(
                    (after.st_mode, after.st_dev, after.st_ino),
                    (before.st_mode, before.st_dev, before.st_ino),
                )
                self.assertTrue(journal_path.is_file())
    def test_rerun_preserves_artifact_when_canonical_journal_was_replaced(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL

        def interrupt(checkpoint):
            if checkpoint == "journal-finalizing":
                raise RuntimeError("injected interruption")

        with (
            patch.object(migration, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "interruption"),
        ):
            self.migrate(project.name, apply=True)

        journal_bytes = journal_path.read_bytes()
        stale = project / ("." + "c" * 32 + ".clip-migration.restore")
        os.link(journal_path, stale)
        original_identity = (stale.stat().st_dev, stale.stat().st_ino)
        replacement = project / ".replacement-journal"
        replacement.write_bytes(journal_bytes)
        os.replace(replacement, journal_path)

        with self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "unrecognized"):
            self.migrate(project.name, apply=True)

        self.assertEqual(stale.read_bytes(), journal_bytes)
        self.assertEqual(journal_path.read_bytes(), journal_bytes)
        self.assertEqual(
            (stale.stat().st_dev, stale.stat().st_ino), original_identity)
        self.assertNotEqual(
            (journal_path.stat().st_dev, journal_path.stat().st_ino),
            original_identity,
        )
    def test_completed_project_restore_artifact_fails_without_writes(self):
        project = self.legacy_project()
        self.migrate(project.name, apply=True)
        (project / ".project.lock").unlink()
        artifact = project / ("." + "d" * 32 + ".clip-migration.restore")
        artifact.write_bytes(b"must remain untouched")
        before = self.metadata_snapshot(project)

        with self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "restore artifact remains"):
            self.migrate(project.name, apply=True)

        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertEqual(artifact.read_bytes(), b"must remain untouched")
        self.assertFalse((project / ".project.lock").exists())
    def test_unlink_boundary_replacement_journal_is_never_overwritten(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL
        replacement = b'{"replacement":true}\n'
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        replaced = False

        def replace_after_journal_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal replaced
            result = real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if (not replaced and source == migration.CLIP_MIGRATION_JOURNAL
                    and "safe-delete" in destination):
                replaced = True
                journal_path.write_bytes(replacement)
            return result

        with (
            patch.object(
                safe_files, "_renameat2",
                side_effect=replace_after_journal_quarantine,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(replaced)
        self.assertEqual(journal_path.read_bytes(), replacement)
        quarantines = [
            path for path in project.iterdir() if "safe-delete" in path.name
        ]
        self.assertEqual(len(quarantines), 1)
        self.assertEqual(json.loads(quarantines[0].read_bytes())["phase"],
                         "finalizing")
    def test_replacement_journal_after_destination_validation_blocks_success(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL
        replacement = b'{"late-replacement":true}\n'
        retained_journal = None
        finalizing_validations = 0
        real_validate = migration._validate_migration_destination_descriptors

        def replace_after_validation(*args, **kwargs):
            nonlocal retained_journal, finalizing_validations
            real_validate(*args, **kwargs)
            journal = args[3]
            if journal["phase"] == "finalizing":
                finalizing_validations += 1
                if finalizing_validations == 2:
                    retained_journal = journal_path.read_bytes() if journal_path.exists() else (
                        json.dumps(journal, indent=2, ensure_ascii=False,
                                   sort_keys=True) + "\n").encode("utf-8")
                    journal_path.write_bytes(replacement)

        with (
            patch.object(
                migration, "_validate_migration_destination_descriptors",
                side_effect=replace_after_validation,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertEqual(finalizing_validations, 2)
        self.assertEqual(journal_path.read_bytes(), replacement)
        self.assertIsNotNone(retained_journal)

        journal_path.unlink()
        assert retained_journal is not None
        journal_path.write_bytes(retained_journal)
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(journal_path.exists())
    def test_exception_after_actual_unlink_restores_finalizing_journal(self):
        project = self.legacy_project()
        journal_path = project / migration.CLIP_MIGRATION_JOURNAL
        retained_journal = None
        real_unlink = os.unlink
        interrupted = False

        def unlink_then_interrupt(path, *args, dir_fd=None):
            nonlocal retained_journal, interrupted
            name = os.fsdecode(path)
            if not interrupted and "safe-delete" in name:
                descriptor = os.open(
                    name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    candidate = os.read(descriptor, 1024 * 1024)
                finally:
                    os.close(descriptor)
                try:
                    phase = json.loads(candidate)["phase"]
                except (json.JSONDecodeError, KeyError, TypeError):
                    phase = None
                if phase == "finalizing":
                    interrupted = True
                    retained_journal = candidate
                    real_unlink(path, *args, dir_fd=dir_fd)
                    raise OSError("interrupted after actual journal unlink")
            return real_unlink(path, *args, dir_fd=dir_fd)

        with (
            patch.object(migration.os, "unlink", side_effect=unlink_then_interrupt),
            self.assertRaisesRegex(
                safe_files.SafeFilesystemError,
                "quarantine cleanup could not be proven"),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(interrupted)
        self.assertIsNotNone(retained_journal)
        assert retained_journal is not None
        self.assertEqual(journal_path.read_bytes(), retained_journal)
        self.assertEqual(json.loads(retained_journal)["phase"], "finalizing")

        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(journal_path.exists())
    def test_no_journal_manifest_rejects_unsafe_extra_then_is_read_only(self):
        project = self.legacy_project()
        self.migrate(project.name, apply=True)
        (project / ".project.lock").unlink()
        outside = Path(self.temp.name) / "outside-already-migrated.txt"
        outside.write_bytes(b"must remain untouched")
        unexpected = project / "clips" / "clip-001" / "unexpected"
        unexpected.symlink_to(outside)
        before = self.metadata_snapshot(project)

        with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
            self.migrate(project.name, apply=True)

        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertEqual(outside.read_bytes(), b"must remain untouched")

        unexpected.unlink()
        before = self.metadata_snapshot(project)
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "already-migrated")
        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertFalse((project / ".project.lock").exists())
    def test_projects_parent_swap_during_finalization_fails_and_recovers(self):
        project = self.legacy_project()
        expected = {
            relative: value for relative, value in self.file_inventory(project).items()
            if (relative in {"current_prompt.txt", "current_generation.json"}
                or relative.startswith("generations/"))
        }
        projects = self.root / "projects"
        replacement = Path(self.temp.name) / "replacement-projects"
        displaced = Path(self.temp.name) / "displaced-projects"
        returned_replacement = Path(self.temp.name) / "returned-replacement-projects"
        shutil.copytree(projects, replacement)
        marker = replacement / project.name / "replacement-marker"
        marker.write_bytes(b"replacement bytes")
        replacement_before = self.snapshot(replacement)
        finalizing_validations = 0
        swapped = False
        real_validate = migration._validate_migration_destination_descriptors

        def swap_projects_parent_after_validation(*args, **kwargs):
            nonlocal finalizing_validations, swapped
            real_validate(*args, **kwargs)
            journal = args[3]
            if journal["phase"] == "finalizing":
                finalizing_validations += 1
                if finalizing_validations == 2:
                    projects.rename(displaced)
                    replacement.rename(projects)
                    swapped = True

        with (
            patch.object(
                migration, "_validate_migration_destination_descriptors",
                side_effect=swap_projects_parent_after_validation,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(swapped)
        self.assertEqual(self.snapshot(projects), replacement_before)
        original = displaced / project.name
        journal_path = original / migration.CLIP_MIGRATION_JOURNAL
        self.assertTrue(journal_path.is_file())
        self.assertEqual(json.loads(journal_path.read_bytes())["phase"], "finalizing")
        self.assertEqual(
            self.file_inventory(original / "clips" / "clip-001"), expected)
        self.assertEqual((projects / project.name /
                          "replacement-marker").read_bytes(), b"replacement bytes")

        projects.rename(returned_replacement)
        displaced.rename(projects)
        self.assertEqual(self.snapshot(returned_replacement), replacement_before)
        self.assertTrue((project / migration.CLIP_MIGRATION_JOURNAL).is_file())
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse((project / migration.CLIP_MIGRATION_JOURNAL).exists())
