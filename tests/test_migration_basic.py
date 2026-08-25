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


class LegacyMigrationBasicTests(LegacyClipMigrationCase):
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
        self.assertFalse((project / migration.CLIP_MIGRATION_JOURNAL).exists())
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
        self.assertFalse((project / migration.CLIP_MIGRATION_JOURNAL).exists())
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
    def test_already_migrated_accepts_any_complete_manifest_layout_read_only(self):
        project = self.root / "projects" / "2026-08-23_current-custom"
        project.mkdir()
        store = ClipStore()
        store.initialize(project, "Custom current project")
        store.create_clip(project, "Closing scene")
        store.update_clip(
            project, "clip-001", title="Disabled opening", enabled=False)
        generation = project / "clips" / "clip-002" / "generations" / "007"
        generation.mkdir()
        (generation / "take.mp4").write_bytes(b"canonical video")
        store.select_take(project, "clip-002", "007", "take.mp4")
        expected_manifest = store.reorder(project, ["clip-002", "clip-001"])
        (project / "clips" / "clip-001" /
         ".generation-archive.lock").write_bytes(b"")
        (project / "clips" / "clip-002" /
         "current_generation.json").write_bytes(b'{"seed":7}\n')
        (project / ".project.lock").unlink()

        orphan = project / "clips" / "clip-999"
        orphan.mkdir()
        (orphan / "current_prompt.txt").touch()
        (orphan / "generations").mkdir()
        before = self.metadata_snapshot(project)
        with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
            self.migrate(project.name, apply=True)
        self.assertEqual(self.metadata_snapshot(project), before)
        shutil.rmtree(orphan)

        outside = Path(self.temp.name) / "outside-canonical.txt"
        outside.write_bytes(b"outside must remain untouched")
        nested_link = (
            project / "clips" / "clip-001" / "generations" / "unexpected")
        nested_link.symlink_to(outside)
        before = self.metadata_snapshot(project)
        with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
            self.migrate(project.name, apply=True)
        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertEqual(outside.read_bytes(), b"outside must remain untouched")
        nested_link.unlink()

        before = self.metadata_snapshot(project)
        report = self.migrate(project.name, apply=True)
        self.assertEqual(report["projects"][0]["status"], "already-migrated")
        self.assertEqual(ClipStore().describe(project), expected_manifest)
        self.assertEqual(self.metadata_snapshot(project), before)
        self.assertFalse((project / ".project.lock").exists())
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
            "journal-finalizing",
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
                    patch.object(migration, "_migration_checkpoint",
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
                    (project / migration.CLIP_MIGRATION_JOURNAL).exists())
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
                                migration.CLIP_MIGRATION_JOURNAL)
                    (project / filename).symlink_to(outside)
                before = self.snapshot(project)
                with self.assertRaises((ValueError, safe_files.SafeFilesystemError)):
                    self.migrate(project.name, apply=case != "journal")
                self.assertEqual(self.snapshot(project), before)
                self.assertEqual(outside.read_bytes() if outside.is_file() else
                                 list(outside.iterdir()),
                                 b"outside" if outside.is_file() else [])
