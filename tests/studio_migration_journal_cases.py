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


from tests.studio_migration_base import LegacyClipMigrationCase


class LegacyMigrationJournalTests(LegacyClipMigrationCase):
    def test_recovers_crash_temp_before_initial_journal_publication(self):
        project = self.legacy_project()
        temporary = self.journal_temp(project, "a", ds._new_migration_journal(project))

        report = self.migrate(project.name, apply=True)

        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(temporary.exists())
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration*")], [])
    def test_recovers_valid_prior_journal_temp_after_successful_cas(self):
        project = self.legacy_project()
        self._prepare_journal(project)
        prepared = json.loads((project / ds.CLIP_MIGRATION_JOURNAL).read_bytes())

        def interrupt(checkpoint):
            if checkpoint == "moved:current_prompt.txt":
                raise RuntimeError("stop after first journal CAS")

        with (
            patch.object(ds, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "first journal CAS"),
        ):
            self.migrate(project.name, apply=True)
        canonical = json.loads((project / ds.CLIP_MIGRATION_JOURNAL).read_bytes())
        self.assertEqual(canonical["phase"], "moving")
        temporary = self.journal_temp(project, "b", prepared)

        report = self.migrate(project.name, apply=True)

        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(temporary.exists())
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration*")], [])
    def test_invalid_symlink_and_special_journal_temps_fail_and_are_preserved(self):
        for index, case in enumerate(("invalid", "symlink", "fifo")):
            with self.subTest(case=case):
                project = self.legacy_project(f"2026-08-23_journal-temp-{index}")
                self._prepare_journal(project)
                temporary = project / (
                    "." + str(index) * 32 + "..clip-migration.json.tmp")
                outside = Path(self.temp.name) / f"outside-journal-temp-{index}"
                outside.write_bytes(b"outside")
                if case == "invalid":
                    temporary.write_bytes(b"not a journal")
                elif case == "symlink":
                    temporary.symlink_to(outside)
                else:
                    os.mkfifo(temporary)
                before = temporary.lstat()

                with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
                    self.migrate(project.name, apply=True)

                after = temporary.lstat()
                self.assertEqual(
                    (after.st_mode, after.st_dev, after.st_ino),
                    (before.st_mode, before.st_dev, before.st_ino))
                self.assertEqual(outside.read_bytes(), b"outside")
                self.assertTrue((project / ds.CLIP_MIGRATION_JOURNAL).is_file())
    def test_multiple_journal_temps_without_canonical_fail_and_are_preserved(self):
        project = self.legacy_project()
        journal = ds._new_migration_journal(project)
        temporaries = [
            self.journal_temp(project, character, journal)
            for character in ("c", "d")
        ]
        before = self.metadata_snapshot(project)

        with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
            self.migrate(project.name, apply=True)

        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertTrue(all(path.is_file() for path in temporaries))
        self.assertFalse((project / ".project.lock").exists())
    def test_completed_project_journal_temp_fails_read_only_without_lock(self):
        project = self.legacy_project()
        prepared = ds._new_migration_journal(project)
        self.migrate(project.name, apply=True)
        (project / ".project.lock").unlink()
        temporary = self.journal_temp(project, "e", prepared)
        before = self.metadata_snapshot(project)

        with self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "journal temp"):
            self.migrate(project.name, apply=True)

        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertTrue(temporary.is_file())
        self.assertFalse((project / ".project.lock").exists())
    def test_replacement_before_every_journal_cas_is_preserved_and_fails(self):
        for update_index in range(1, 7):
            with self.subTest(update_index=update_index):
                project = self.legacy_project(
                    f"2026-08-23_journal-cas-{update_index}")
                canonical = project / ds.CLIP_MIGRATION_JOURNAL
                preserved = project / f".preserved-journal-{update_index}"
                replacement = f"replacement-{update_index}".encode()
                real_renameat2 = safe_files._renameat2
                assert real_renameat2 is not None
                exchanges = 0
                injected = False

                def replace_before_exchange(
                        source_fd, source_name, destination_fd,
                        destination_name, flags):
                    nonlocal exchanges, injected
                    source = os.fsdecode(source_name)
                    destination = os.fsdecode(destination_name)
                    if (flags == safe_files._RENAME_EXCHANGE
                            and destination == ds.CLIP_MIGRATION_JOURNAL):
                        exchanges += 1
                        if exchanges == update_index:
                            injected = True
                            os.rename(
                                destination, preserved.name,
                                src_dir_fd=destination_fd,
                                dst_dir_fd=destination_fd)
                            descriptor = os.open(
                                destination,
                                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                | os.O_NOFOLLOW,
                                0o600,
                                dir_fd=destination_fd,
                            )
                            try:
                                os.write(descriptor, replacement)
                            finally:
                                os.close(descriptor)
                    return real_renameat2(
                        source_fd, source_name, destination_fd,
                        destination_name, flags)

                with (
                    patch.object(
                        safe_files, "_renameat2",
                        side_effect=replace_before_exchange),
                    self.assertRaisesRegex(
                        safe_files.SafeFilesystemError,
                        "expected target identity"),
                ):
                    result = self.migrate(project.name, apply=True)
                    self.fail(f"migration unexpectedly reported: {result}")

                self.assertTrue(injected)
                self.assertEqual(canonical.read_bytes(), replacement)
                self.assertTrue(preserved.is_file())
                self.assertGreaterEqual(len(list(
                    project.glob(".*.clip-migration.json.tmp"))), 1)
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
    def test_injected_destination_symlink_after_creation_blocks_and_resumes(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-injected-after-creation.txt"
        outside.write_bytes(b"outside must remain untouched")
        unexpected = project / "clips" / "clip-001" / "unexpected"

        def inject(checkpoint):
            if checkpoint == "directories-created":
                unexpected.symlink_to(outside)

        with (
            patch.object(ds, "_migration_checkpoint", side_effect=inject),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(unexpected.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
        self.assertTrue((project / ds.CLIP_MIGRATION_JOURNAL).is_file())
        self.assertFalse((project / "project.json").exists())

        unexpected.unlink()
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())
    def test_late_injected_destination_symlink_blocks_journal_removal_and_resumes(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-injected-before-removal.txt"
        outside.write_bytes(b"outside must remain untouched")
        unexpected = project / "clips" / "clip-001" / "unexpected"

        def inject(checkpoint):
            if checkpoint == "journal-manifest-published":
                unexpected.symlink_to(outside)

        with (
            patch.object(ds, "_migration_checkpoint", side_effect=inject),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(unexpected.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
        self.assertTrue((project / ds.CLIP_MIGRATION_JOURNAL).is_file())
        self.assertTrue((project / "project.json").is_file())

        unexpected.unlink()
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse((project / ds.CLIP_MIGRATION_JOURNAL).exists())
    def test_unlink_boundary_injection_restores_exact_journal_and_resumes(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-injected-at-unlink.txt"
        outside.write_bytes(b"outside must remain untouched")
        unexpected = project / "clips" / "clip-001" / "unexpected"
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        boundary_journal = None
        injected = False

        def inject_before_journal_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal boundary_journal, injected
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if (not injected and source == ds.CLIP_MIGRATION_JOURNAL
                    and "safe-delete" in destination):
                injected = True
                unexpected.symlink_to(outside)
                boundary_journal = journal_path.read_bytes()
            return real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)

        with (
            patch.object(
                safe_files, "_renameat2",
                side_effect=inject_before_journal_quarantine,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(injected)
        self.assertIsNotNone(boundary_journal)
        assert boundary_journal is not None
        self.assertTrue(unexpected.is_symlink())
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
        self.assertEqual(journal_path.read_bytes(), boundary_journal)
        restored = json.loads(boundary_journal)
        self.assertEqual(restored["phase"], "finalizing")
        self.assertEqual(
            restored["completed"],
            [mapping["source"] for mapping in restored["mappings"]],
        )
        self.assertEqual(json.loads((project / "project.json").read_bytes()),
                         restored["manifest"])
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration*")
             if path.name != ds.CLIP_MIGRATION_JOURNAL],
            [],
        )

        unexpected.unlink()
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(journal_path.exists())
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration*")], [])
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
    def test_journal_replacement_at_removal_boundary_is_preserved_and_fails(self):
        project = self.legacy_project()
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL
        preserved = project / ".preserved-expected-journal"
        replacement = b'{"replacement":true}\n'
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        injected = False

        def replace_before_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal injected
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if (not injected and source == ds.CLIP_MIGRATION_JOURNAL
                    and destination != ds.CLIP_MIGRATION_JOURNAL):
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

        with (
            patch.object(
                safe_files, "_renameat2", side_effect=replace_before_quarantine),
            self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "journal identity changed"),
        ):
            result = self.migrate(project.name, apply=True)
            self.fail(f"migration unexpectedly reported: {result}")

        self.assertTrue(injected)
        self.assertEqual(journal_path.read_bytes(), replacement)
        self.assertEqual(json.loads(preserved.read_bytes())["phase"], "finalizing")
        self.assertEqual(
            [entry.name for entry in project.iterdir()
             if "safe-delete" in entry.name],
            [],
        )
    def test_atomic_restore_consumes_private_temp_and_resumes_cleanly(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-restore-interruption.txt"
        outside.write_bytes(b"outside must remain untouched")
        unexpected = project / "clips" / "clip-001" / "unexpected"
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL
        real_unlink = os.unlink
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        boundary_journal = None
        injected = False
        restore_unlink_attempts = 0

        def interrupt_old_post_link_unlink(path, *args, dir_fd=None):
            nonlocal restore_unlink_attempts
            name = os.fsdecode(path)
            if name.endswith(".clip-migration.restore"):
                restore_unlink_attempts += 1
                raise OSError("injected old post-link temp unlink failure")
            return real_unlink(path, *args, dir_fd=dir_fd)

        def inject_before_journal_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal boundary_journal, injected
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if (not injected and source == ds.CLIP_MIGRATION_JOURNAL
                    and "safe-delete" in destination):
                injected = True
                unexpected.symlink_to(outside)
                boundary_journal = journal_path.read_bytes()
            return real_renameat2(
                source_fd, source_name, destination_fd, destination_name, flags)

        with (
            patch.object(
                ds.os, "unlink", side_effect=interrupt_old_post_link_unlink),
            patch.object(
                safe_files, "_renameat2",
                side_effect=inject_before_journal_quarantine,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(injected)
        self.assertIsNotNone(boundary_journal)
        assert boundary_journal is not None
        self.assertEqual(journal_path.read_bytes(), boundary_journal)
        self.assertEqual(restore_unlink_attempts, 0)
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration.restore")], [])
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")

        unexpected.unlink()
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(journal_path.exists())
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration.restore")], [])
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
    def test_rerun_removes_only_restore_hardlink_to_canonical_journal(self):
        project = self.legacy_project()
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL

        def interrupt(checkpoint):
            if checkpoint == "journal-finalizing":
                raise RuntimeError("injected interruption before finalization")

        with (
            patch.object(ds, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "before finalization"),
        ):
            self.migrate(project.name, apply=True)

        stale_artifacts = [
            project / ("." + character * 32 + ".clip-migration.restore")
            for character in ("a", "e")
        ]
        for stale in stale_artifacts:
            os.link(journal_path, stale)
            self.assertEqual(
                (journal_path.stat().st_dev, journal_path.stat().st_ino),
                (stale.stat().st_dev, stale.stat().st_ino),
            )

        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse(journal_path.exists())
        self.assertTrue(all(not stale.exists() for stale in stale_artifacts))
        self.assertEqual(
            [path.name for path in project.glob(".*clip-migration.restore")], [])
    def test_restore_artifact_replacement_at_removal_boundary_is_preserved(self):
        project = self.legacy_project()
        journal_path = project / ds.CLIP_MIGRATION_JOURNAL

        def interrupt(checkpoint):
            if checkpoint == "journal-finalizing":
                raise RuntimeError("injected interruption")

        with (
            patch.object(ds, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "interruption"),
        ):
            self.migrate(project.name, apply=True)

        artifact = project / ("." + "a" * 32 + ".clip-migration.restore")
        preserved = project / ".preserved-expected-artifact"
        replacement = b"replacement artifact"
        os.link(journal_path, artifact)
        real_renameat2 = safe_files._renameat2
        assert real_renameat2 is not None
        injected = False

        def replace_before_quarantine(
                source_fd, source_name, destination_fd, destination_name, flags):
            nonlocal injected
            source = os.fsdecode(source_name)
            destination = os.fsdecode(destination_name)
            if (not injected and source == artifact.name
                    and destination != artifact.name):
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

        with (
            patch.object(
                safe_files, "_renameat2", side_effect=replace_before_quarantine),
            self.assertRaisesRegex(
                safe_files.SafeFilesystemError, "artifact identity changed"),
        ):
            result = self.migrate(project.name, apply=True)
            self.fail(f"migration unexpectedly reported: {result}")

        self.assertTrue(injected)
        self.assertEqual(artifact.read_bytes(), replacement)
        self.assertEqual(
            (preserved.stat().st_dev, preserved.stat().st_ino),
            (journal_path.stat().st_dev, journal_path.stat().st_ino),
        )
        self.assertEqual(
            [entry.name for entry in project.iterdir()
             if "safe-delete" in entry.name],
            [],
        )
