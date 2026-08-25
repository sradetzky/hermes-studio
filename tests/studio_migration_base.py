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


class LegacyClipMigrationCase(unittest.TestCase):
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
            return migration.migrate_clips(
                self.root, project, apply=apply, clip_store=ds.CLIP_STORE)
    def _prepare_journal(self, project):
        def interrupt(checkpoint):
            if checkpoint == "journal-prepared":
                raise RuntimeError("stop")
        with (
            patch.object(migration, "_migration_checkpoint", side_effect=interrupt),
            self.assertRaises(RuntimeError),
        ):
            self.migrate(project.name, apply=True)
    @staticmethod
    def journal_bytes(journal):
        return (json.dumps(
            journal, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode(
                "utf-8")
    @staticmethod
    def journal_temp(project, character, journal):
        temporary = project / (
            "." + character * 32 + "..clip-migration.json.tmp")
        temporary.write_bytes(LegacyClipMigrationCase.journal_bytes(journal))
        return temporary
