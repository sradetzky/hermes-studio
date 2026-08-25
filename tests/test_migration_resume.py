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


class LegacyMigrationResumeTests(LegacyClipMigrationCase):
    def test_destination_parent_swap_during_inventory_fails_closed_and_resumes(self):
        project = self.legacy_project()
        outside = Path(self.temp.name) / "outside-swapped-clips"
        outside.mkdir()
        clips = project / "clips"
        displaced = project / "displaced-clips"
        real_validate = migration._validate_migration_inventory_directory
        swapped = False

        def swap_after_inventory(descriptor, tree, *, label):
            nonlocal swapped
            real_validate(descriptor, tree, label=label)
            if not swapped and label == "migration target generations":
                swapped = True
                clips.rename(displaced)
                clips.symlink_to(outside, target_is_directory=True)

        with (
            patch.object(
                migration, "_validate_migration_inventory_directory",
                side_effect=swap_after_inventory,
            ),
            self.assertRaises((ValueError, safe_files.SafeFilesystemError)),
        ):
            self.migrate(project.name, apply=True)

        self.assertTrue(swapped)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((project / migration.CLIP_MIGRATION_JOURNAL).is_file())
        self.assertFalse((project / "project.json").exists())

        clips.unlink()
        displaced.rename(clips)
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "migrated")
        self.assertFalse((project / migration.CLIP_MIGRATION_JOURNAL).exists())
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
        self.assertFalse((project / migration.CLIP_MIGRATION_JOURNAL).exists())
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
