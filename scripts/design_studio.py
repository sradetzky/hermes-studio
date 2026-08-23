#!/usr/bin/env python3
"""design_studio.py — core library + CLI for the Hermes Studio.

Manages the on-disk project structure (source of truth) and wraps H3
generation via the proven minimax-h3-run runner.

Root resolution order:
  1. --root flag / DESIGN_STUDIO_ROOT env var
  2. ~/repos/hermes-studio/studio-root

Usage:
  python3 scripts/design_studio.py create-project my-idea "A brief..."
  python3 scripts/design_studio.py list-projects
  python3 scripts/design_studio.py list-clips 2026-08-23_my-idea
  python3 scripts/design_studio.py write-prompt 2026-08-23_my-idea clip-001 "..."
  python3 scripts/design_studio.py append-chat my-idea user "hello"
  python3 scripts/design_studio.py generate my-idea clip-001 --handoff h3_handoff_x.json
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request
import uuid
from contextlib import ExitStack, closing
from dataclasses import replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if __package__ in {None, ""} and str(REPO_ROOT) not in sys.path:
    # Direct `python scripts/design_studio.py` execution needs the repo package
    # root for shared runtime modules. Installed/package imports need no shim.
    sys.path.insert(0, str(REPO_ROOT))

from webapp.clip_store import ClipStore
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_move_no_replace,
    atomic_publish_directory,
    copy_opened_file,
    open_directory,
    open_regular_beneath,
    open_regular_file,
    read_opened_text,
    verify_absolute_directory_identity,
)

RUN_H3 = Path.home() / ".hermes/skills/minimax-h3-run/scripts/run_h3.py"
KREA2 = Path(__file__).resolve().parent / "krea2_image.py"
COMFY_ROOT = Path.home() / "ComfyUI"
COMFY_OUTPUT = COMFY_ROOT / "output"
GROK_IMAGE_OUTPUT = (Path.home() / ".hermes" / "profiles" / "studio-grok" /
                     "cache" / "images")
DEFAULT_ROOT = REPO_ROOT / "studio-root"
DEFAULT_RUNTIME = DEFAULT_ROOT.parent / ".runtime"
SESSION_ID_RE = re.compile(r"session_id:\s*([A-Za-z0-9_-]+)")
LOCAL_SPECIALIST_PROFILES = {
    "studio-storyboarder",
    "studio-prompt-engineer",
    "studio-reviewer",
    "studio-illustrator",
}
SPECIALIST_TOOLSETS = {
    "studio-storyboarder": "file,terminal,skills",
    "studio-prompt-engineer": "file,terminal,skills",
    "studio-reviewer": "file,terminal,vision,skills",
    "studio-illustrator": "file,terminal,skills",
}
CLIP_STORE = ClipStore()
CLIP_MIGRATION_JOURNAL = ".clip-migration.json"
CLIP_MIGRATION_SCHEMA_VERSION = 1
CLIP_MIGRATION_MAPPINGS = (
    ("current_prompt.txt", "clips/clip-001/current_prompt.txt", False, "file"),
    ("current_generation.json", "clips/clip-001/current_generation.json", True, "file"),
    ("generations", "clips/clip-001/generations", False, "directory"),
)


def _migration_checkpoint(_name: str) -> None:
    """Test seam for simulating process interruption at durable boundaries."""


def free_comfy_vram() -> dict:
    """Cleanup for legacy direct execution; production uses MCP clear_vram."""
    request = urllib.request.Request(
        "http://127.0.0.1:8188/free",
        data=json.dumps({"unload_models": True, "free_memory": True}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def interrupt_comfy() -> dict:
    request = urllib.request.Request(
        "http://127.0.0.1:8188/interrupt", data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return {"ok": response.status == 200, "status": response.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def studio_root(override: str | None = None, *, create: bool = True) -> Path:
    root = override or os.environ.get("DESIGN_STUDIO_ROOT") or str(DEFAULT_ROOT)
    p = Path(root).expanduser().resolve()
    if create:
        for sub in ("projects", "shared", "tmp"):
            (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9-_]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError(f"cannot slugify project name: {name!r}")
    return s


def project_path(root: Path, name: str, must_exist: bool = True) -> Path:
    """Resolve a project by exact folder name. No fuzzy matching."""
    projects_path = root / "projects"
    if projects_path.is_symlink():
        raise ValueError("projects directory may not be a symlink")
    projects = projects_path.resolve()
    if not must_exist:
        today = _dt.date.today().isoformat()
        return projects / f"{today}_{slugify(name)}"
    if not name or Path(name).name != name or name in {".", ".."}:
        raise ValueError(f"invalid project id: {name!r}")
    candidate = projects / name
    if candidate.is_symlink():
        raise ValueError(f"project may not be a symlink: {name!r}")
    p = candidate.resolve()
    if p.parent != projects:
        raise ValueError(f"project escapes projects directory: {name!r}")
    if p.is_dir():
        return p
    raise FileNotFoundError(
        f"project {name!r} not found; use an exact folder name (see list-projects)")


# ---------------------------------------------------------------- project mgmt

def create_project(root: Path, name: str, brief: str = "") -> Path:
    pp = project_path(root, name, must_exist=False)
    if os.path.lexists(pp):
        raise FileExistsError(f"project already exists: {pp}")
    pp.mkdir(parents=True)
    try:
        for sub in ("references", "final", "research"):
            (pp / sub).mkdir()
        (pp / "brief.md").write_text(
            f"# {name}\n\n{brief}\n\nCreated {_dt.datetime.now().isoformat(timespec='seconds')}\n",
            encoding="utf-8")
        (pp / "chat.jsonl").touch()
        CLIP_STORE.initialize(pp, name)
    except Exception:
        shutil.rmtree(pp, ignore_errors=True)
        raise
    return pp


def list_projects(root: Path) -> list[str]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    return sorted(d.name for d in projects.iterdir()
                  if d.is_dir() and not d.is_symlink())


# ----------------------------------------------------------- clip migration

def _entry_kind(path: Path) -> str:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(details.st_mode):
        raise SafeFilesystemError(f"migration path may not be a symlink: {path}")
    if stat.S_ISREG(details.st_mode):
        return "file"
    if stat.S_ISDIR(details.st_mode):
        return "directory"
    raise SafeFilesystemError(f"migration path is a special file: {path}")


def _hash_opened_file(path: Path) -> tuple[int, str]:
    with open_regular_file(path) as opened:
        digest = hashlib.sha256()
        os.lseek(opened.descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(opened.descriptor, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
        return opened.stat.st_size, digest.hexdigest()


def _inventory_path(path: Path, expected_kind: str) -> dict:
    kind = _entry_kind(path)
    if kind != expected_kind:
        raise ValueError(
            f"migration entry must be a regular {expected_kind}: {path}")
    if kind == "file":
        size, digest = _hash_opened_file(path)
        return {
            "directories": [],
            "files": [{"relative_path": ".", "size": size, "sha256": digest}],
        }

    directories = ["."]
    files = []

    def walk(directory: Path, relative: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as exc:
            raise SafeFilesystemError(
                f"unsafe migration directory: {directory}") from exc
        for entry in entries:
            entry_path = directory / entry.name
            entry_relative = relative / entry.name
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise SafeFilesystemError(
                    f"unsafe migration entry: {entry_path}") from exc
            if stat.S_ISLNK(details.st_mode):
                raise SafeFilesystemError(
                    f"migration entry may not be a symlink: {entry_path}")
            if stat.S_ISDIR(details.st_mode):
                directories.append(entry_relative.as_posix())
                walk(entry_path, entry_relative)
            elif stat.S_ISREG(details.st_mode):
                size, digest = _hash_opened_file(entry_path)
                files.append({
                    "relative_path": entry_relative.as_posix(),
                    "size": size,
                    "sha256": digest,
                })
            else:
                raise SafeFilesystemError(
                    f"migration entry is a special file: {entry_path}")

    walk(path, Path("."))
    return {"directories": sorted(directories), "files": sorted(
        files, key=lambda item: item["relative_path"])}


def _migration_manifest(project: Path) -> dict:
    return {
        "schema_version": 1,
        "title": project.name,
        "clips": [{
            "id": "clip-001",
            "title": "Main clip",
            "enabled": True,
            "selected_take": None,
        }],
    }


def _new_migration_journal(project: Path) -> dict:
    mappings = []
    for source, target, optional, kind in CLIP_MIGRATION_MAPPINGS:
        source_path = project / source
        source_kind = _entry_kind(source_path)
        if source_kind == "missing" and optional:
            present = False
            inventory = {"directories": [], "files": []}
        else:
            if source_kind == "missing":
                raise ValueError(f"legacy migration source is missing: {source}")
            inventory = _inventory_path(source_path, kind)
            present = True
        mappings.append({
            "source": source,
            "target": target,
            "optional": optional,
            "kind": kind,
            "present": present,
            "inventory": inventory,
        })
    return {
        "schema_version": CLIP_MIGRATION_SCHEMA_VERSION,
        "project": project.name,
        "phase": "prepared",
        "completed": [],
        "manifest": _migration_manifest(project),
        "mappings": mappings,
    }


def _validate_migration_journal(project: Path, value: object) -> dict:
    if not isinstance(value, dict) or set(value) != {
            "schema_version", "project", "phase", "completed", "manifest",
            "mappings"}:
        raise ValueError("clip migration journal has invalid fields")
    if value["schema_version"] != CLIP_MIGRATION_SCHEMA_VERSION:
        raise ValueError("clip migration journal schema is unsupported")
    if value["project"] != project.name:
        raise ValueError("clip migration journal belongs to another project")
    if value["phase"] not in {
            "prepared", "moving", "targets_verified", "manifest_published",
            "finalizing"}:
        raise ValueError("clip migration journal phase is invalid")
    if value["manifest"] != _migration_manifest(project):
        raise ValueError("clip migration journal manifest is invalid")
    mappings = value["mappings"]
    if not isinstance(mappings, list) or len(mappings) != len(
            CLIP_MIGRATION_MAPPINGS):
        raise ValueError("clip migration journal mappings are invalid")
    expected_sources = []
    for mapping, expected in zip(mappings, CLIP_MIGRATION_MAPPINGS):
        source, target, optional, kind = expected
        expected_sources.append(source)
        if not isinstance(mapping, dict) or set(mapping) != {
                "source", "target", "optional", "kind", "present", "inventory"}:
            raise ValueError("clip migration journal mapping is invalid")
        if (mapping["source"], mapping["target"], mapping["optional"],
                mapping["kind"]) != expected:
            raise ValueError("clip migration journal mapping was altered")
        if not isinstance(mapping["present"], bool):
            raise ValueError("clip migration journal presence state is invalid")
        if not mapping["present"] and not optional:
            raise ValueError("required clip migration mapping is absent")
        inventory = mapping["inventory"]
        if not isinstance(inventory, dict) or set(inventory) != {
                "directories", "files"}:
            raise ValueError("clip migration inventory is invalid")
        directories = inventory["directories"]
        files = inventory["files"]
        if (not isinstance(directories, list)
                or any(not isinstance(item, str) for item in directories)
                or directories != sorted(set(directories))
                or not isinstance(files, list)):
            raise ValueError("clip migration inventory is invalid")
        previous = None
        for item in files:
            if not isinstance(item, dict) or set(item) != {
                    "relative_path", "size", "sha256"}:
                raise ValueError("clip migration file inventory is invalid")
            relative = item["relative_path"]
            if (not isinstance(relative, str) or not relative
                    or Path(relative).is_absolute()
                    or any(part == ".." for part in Path(relative).parts)
                    or not isinstance(item["size"], int) or item["size"] < 0
                    or not isinstance(item["sha256"], str)
                    or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
                    or (previous is not None and relative <= previous)):
                raise ValueError("clip migration file inventory is invalid")
            previous = relative
        if not mapping["present"] and (directories or files):
            raise ValueError("absent clip migration mapping has inventory")
    completed = value["completed"]
    if (not isinstance(completed, list)
            or completed != list(dict.fromkeys(completed))
            or any(item not in expected_sources for item in completed)):
        raise ValueError("clip migration completed list is invalid")
    return value


def _read_migration_journal(project: Path) -> dict:
    path = project / CLIP_MIGRATION_JOURNAL
    try:
        with open_regular_file(path) as opened:
            value = json.loads(read_opened_text(opened))
    except (FileNotFoundError, SafeFilesystemError) as exc:
        raise ValueError("clip migration journal is missing or unsafe") from exc
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ValueError("clip migration journal is invalid") from exc
    return _validate_migration_journal(project, value)


def _validate_migration_tree(project: Path, *, journal_exists: bool) -> None:
    clips = project / "clips"
    clips_kind = _entry_kind(clips)
    if clips_kind == "missing":
        return
    if not journal_exists:
        raise ValueError("partial clip layout exists without a valid migration journal")
    if clips_kind != "directory":
        raise ValueError("clips migration destination is unsafe")
    clip = clips / "clip-001"
    clip_kind = _entry_kind(clip)
    allowed_clips = {"clip-001"} if clip_kind != "missing" else set()
    with os.scandir(clips) as iterator:
        actual_clips = {entry.name for entry in iterator}
    if actual_clips != allowed_clips:
        raise ValueError("clips migration destination contains unexpected entries")
    if clip_kind == "missing":
        return
    if clip_kind != "directory":
        raise ValueError("default clip migration destination is unsafe")
    allowed_targets = {
        Path(target).name for _source, target, _optional, _kind
        in CLIP_MIGRATION_MAPPINGS
        if _entry_kind(project / target) != "missing"
    }
    with os.scandir(clip) as iterator:
        actual_targets = {entry.name for entry in iterator}
    if actual_targets != allowed_targets:
        raise ValueError("default clip migration destination contains unexpected entries")


def _mapping_state(project: Path, mapping: dict) -> str:
    source_kind = _entry_kind(project / mapping["source"])
    target_kind = _entry_kind(project / mapping["target"])
    source_present = source_kind != "missing"
    target_present = target_kind != "missing"
    if not mapping["present"]:
        if source_present or target_present:
            raise ValueError(
                f"optional migration entry appeared: {mapping['source']}")
        return "missing-optional"
    if source_present and target_present:
        raise ValueError(
            f"both source and target exist for {mapping['source']}")
    if not source_present and not target_present:
        raise ValueError(
            f"neither source nor target exists for {mapping['source']}")
    current = project / (mapping["source"] if source_present else mapping["target"])
    current_inventory = _inventory_path(current, mapping["kind"])
    if current_inventory != mapping["inventory"]:
        label = "source" if source_present else "target"
        raise ValueError(
            f"migration {label} inventory changed for {mapping['source']}")
    return "source" if source_present else "target"


def _report_from_journal(project: Path, journal: dict, status: str) -> dict:
    operations = []
    inventory = []
    for mapping in journal["mappings"]:
        state = _mapping_state(project, mapping)
        operations.append({
            "source": mapping["source"],
            "target": mapping["target"],
            "optional": mapping["optional"],
            "state": state,
        })
        for item in mapping["inventory"]["files"]:
            relative = (mapping["source"] if item["relative_path"] == "." else
                        f"{mapping['source']}/{item['relative_path']}")
            inventory.append({"relative_path": relative,
                              "size": item["size"],
                              "sha256": item["sha256"]})
    inventory.sort(key=lambda item: item["relative_path"])
    return {
        "project": project.name,
        "status": status,
        "phase": journal["phase"],
        "operations": operations,
        "inventory": inventory,
        "file_count": len(inventory),
        "total_bytes": sum(item["size"] for item in inventory),
        "active_jobs": [],
    }


def _assess_migration_project(project: Path) -> tuple[dict, dict | None]:
    lock_kind = _entry_kind(project / ".project.lock")
    if lock_kind not in {"missing", "file"}:
        raise ValueError("project lock is unsafe")
    journal_kind = _entry_kind(project / CLIP_MIGRATION_JOURNAL)
    if journal_kind not in {"missing", "file"}:
        raise ValueError("clip migration journal is unsafe")
    manifest_kind = _entry_kind(project / "project.json")
    if manifest_kind not in {"missing", "file"}:
        raise ValueError("project manifest is unsafe")

    if journal_kind == "file":
        journal = _read_migration_journal(project)
        _validate_migration_tree(project, journal_exists=True)
        if manifest_kind == "file":
            manifest = CLIP_STORE.describe(project)
            if manifest != journal["manifest"]:
                raise ValueError(
                    "published manifest does not match the migration journal")
        report = _report_from_journal(project, journal, "resumable")
        return report, journal

    source_kinds = {
        source: _entry_kind(project / source)
        for source, _target, _optional, _kind in CLIP_MIGRATION_MAPPINGS
    }
    if manifest_kind == "file":
        if any(kind != "missing" for kind in source_kinds.values()):
            raise ValueError("legacy sources remain without a migration journal")
        _validate_completed_migration(project)
        return ({
            "project": project.name,
            "status": "already-migrated",
            "phase": "complete",
            "operations": [],
            "inventory": [],
            "file_count": 0,
            "total_bytes": 0,
            "active_jobs": [],
        }, None)

    _validate_migration_tree(project, journal_exists=False)
    journal = _new_migration_journal(project)
    return _report_from_journal(project, journal, "planned"), journal


def _active_migration_jobs(project_ids: list[str]) -> dict[str, list[dict]]:
    result = {project_id: [] for project_id in project_ids}
    if not project_ids:
        return result
    runtime = Path(os.environ.get(
        "HERMES_STUDIO_RUNTIME_ROOT", DEFAULT_RUNTIME)).expanduser()
    database = runtime / "studio.db"
    kind = _entry_kind(database)
    if kind == "missing":
        return result
    if kind != "file":
        raise ValueError("runtime database is unsafe")
    uri = database.absolute().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(
                uri, uri=True, timeout=5)) as connection:
            placeholders = ",".join("?" for _ in project_ids)
            with closing(connection.execute(
                    f"SELECT id, project, status FROM jobs "
                    f"WHERE project IN ({placeholders}) "
                    "AND status IN ('queued', 'running') "
                    "ORDER BY project, id",
                    project_ids,
            )) as cursor:
                rows = cursor.fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table: jobs" in str(exc):
            return result
        raise ValueError(f"could not inspect runtime jobs: {exc}") from exc
    for job_id, project_id, status in rows:
        result[project_id].append({"id": job_id, "status": status})
    return result


def _atomic_json_file(project: Path, filename: str, value: dict, *,
                      replace: bool) -> None:
    target_kind = _entry_kind(project / filename)
    if replace:
        if target_kind != "file":
            raise ValueError(f"atomic JSON target is missing or unsafe: {filename}")
    elif target_kind != "missing":
        raise ValueError(f"atomic JSON target already exists: {filename}")
    project_fd = os.open(
        project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{uuid.uuid4().hex}.{filename}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=project_fd,
        )
        data = (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
                + "\n").encode("utf-8")
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            os.replace(
                temporary, filename,
                src_dir_fd=project_fd, dst_dir_fd=project_fd)
        else:
            atomic_move_no_replace(project / temporary, project / filename)
        os.fsync(project_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=project_fd)
        except FileNotFoundError:
            pass
        os.close(project_fd)


_MIGRATION_DIRECTORY_FLAGS = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)


def _open_migration_directory(
        parent_fd: int, name: str, *, label: str) -> int:
    try:
        descriptor = os.open(
            name, _MIGRATION_DIRECTORY_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SafeFilesystemError(f"{label} is unsafe")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _open_or_create_migration_directory(
        parent_fd: int, name: str, *, label: str) -> tuple[int, bool]:
    created = False
    try:
        os.mkdir(name, 0o777, dir_fd=parent_fd)
        created = True
    except FileExistsError:
        pass
    except OSError as exc:
        raise SafeFilesystemError(f"could not create {label}") from exc
    return _open_migration_directory(parent_fd, name, label=label), created


def _verify_migration_directory_identity(
        parent_fd: int, name: str, descriptor: int, *, label: str) -> None:
    try:
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} changed during migration") from exc
    retained = os.fstat(descriptor)
    if (not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino)
            != (retained.st_dev, retained.st_ino)):
        raise SafeFilesystemError(f"{label} changed during migration")


_MIGRATION_FILE_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)


def _migration_directory_names(descriptor: int, *, label: str) -> set[str]:
    try:
        return set(os.listdir(descriptor))
    except OSError as exc:
        raise SafeFilesystemError(f"could not inspect {label}") from exc


def _open_migration_regular_file(
        parent_fd: int, name: str, *, label: str) -> tuple[int, os.stat_result]:
    try:
        descriptor = os.open(
            name, _MIGRATION_FILE_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SafeFilesystemError(f"{label} is unsafe")
        return descriptor, details
    except Exception:
        os.close(descriptor)
        raise


def _hash_migration_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return digest.hexdigest()
        digest.update(chunk)


def _migration_stat_signature(details: os.stat_result) -> tuple[int, ...]:
    return (
        details.st_dev,
        details.st_ino,
        details.st_mode,
        details.st_nlink,
        details.st_uid,
        details.st_gid,
        details.st_size,
        details.st_mtime_ns,
        details.st_ctime_ns,
    )


def _migration_entry_is_absent(
        parent_fd: int, name: str, *, label: str) -> bool:
    """Inspect one canonical name without following any replacement entry."""
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return True
    except OSError as exc:
        raise SafeFilesystemError(f"could not inspect {label}") from exc
    return False


def _require_migration_journal_absent(project_fd: int) -> None:
    if not _migration_entry_is_absent(
            project_fd, CLIP_MIGRATION_JOURNAL,
            label="clip migration journal"):
        raise SafeFilesystemError(
            "clip migration journal exists after finalization")


def _inventory_migration_regular_file(
        parent_fd: int, name: str, relative: str, *, label: str
) -> tuple[dict, tuple[int, ...]]:
    """Inventory one retained regular file and reject name/identity races."""
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    if not stat.S_ISREG(named.st_mode):
        raise SafeFilesystemError(f"{label} is unsafe")
    descriptor, before = _open_migration_regular_file(
        parent_fd, name, label=label)
    try:
        if ((before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
            raise SafeFilesystemError(f"{label} changed while opening")
        digest = _hash_migration_descriptor(descriptor)
        after = os.fstat(descriptor)
        signature = _migration_stat_signature(after)
        if (not stat.S_ISREG(after.st_mode)
                or signature != _migration_stat_signature(before)):
            raise SafeFilesystemError(f"{label} changed while hashing")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or _migration_stat_signature(current) != signature):
            raise SafeFilesystemError(f"{label} changed during inventory")
        return ({
            "relative_path": relative,
            "size": after.st_size,
            "sha256": digest,
        }, signature)
    finally:
        os.close(descriptor)


def _inventory_migration_directory_descriptor(
        descriptor: int, relative: Path, *, label: str
) -> tuple[list[str], list[dict]]:
    """Build an exact inventory through one retained descriptor tree."""
    before = os.fstat(descriptor)
    names = _migration_directory_names(descriptor, label=label)
    directories = [relative.as_posix()]
    files = []
    retained = {}
    for name in sorted(names):
        child_relative = relative / name
        child_label = f"{label}/{name}"
        try:
            named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SafeFilesystemError(f"{child_label} is unsafe") from exc
        if stat.S_ISREG(named.st_mode):
            item, signature = _inventory_migration_regular_file(
                descriptor, name, child_relative.as_posix(), label=child_label)
            files.append(item)
            retained[name] = ("file", signature)
            continue
        if not stat.S_ISDIR(named.st_mode):
            raise SafeFilesystemError(f"{child_label} is unsafe")
        child_fd = _open_migration_directory(
            descriptor, name, label=child_label)
        try:
            child_before = os.fstat(child_fd)
            if ((child_before.st_dev, child_before.st_ino)
                    != (named.st_dev, named.st_ino)):
                raise SafeFilesystemError(f"{child_label} changed while opening")
            child_directories, child_files = (
                _inventory_migration_directory_descriptor(
                    child_fd, child_relative, label=child_label))
            _verify_migration_directory_identity(
                descriptor, name, child_fd, label=child_label)
            child_after = os.fstat(child_fd)
            signature = _migration_stat_signature(child_after)
            if signature != _migration_stat_signature(child_before):
                raise SafeFilesystemError(
                    f"{child_label} changed during inventory")
            directories.extend(child_directories)
            files.extend(child_files)
            retained[name] = ("directory", signature)
        finally:
            os.close(child_fd)

    if _migration_directory_names(descriptor, label=label) != names:
        raise SafeFilesystemError(f"{label} changed during inventory")
    for name, (kind, signature) in retained.items():
        current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR
        if (not expected_type(current.st_mode)
                or _migration_stat_signature(current) != signature):
            raise SafeFilesystemError(
                f"{label}/{name} changed during inventory")
    if _migration_stat_signature(os.fstat(descriptor)) != (
            _migration_stat_signature(before)):
        raise SafeFilesystemError(f"{label} changed during inventory")
    return directories, files


def _inventory_migration_target(
        clip_fd: int, name: str, kind: str) -> dict:
    label = f"default clip migration destination/{name}"
    if kind == "file":
        item, _signature = _inventory_migration_regular_file(
            clip_fd, name, ".", label=label)
        return {"directories": [], "files": [item]}

    try:
        named = os.stat(name, dir_fd=clip_fd, follow_symlinks=False)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    if not stat.S_ISDIR(named.st_mode):
        raise SafeFilesystemError(f"{label} is unsafe")
    target_fd = _open_migration_directory(clip_fd, name, label=label)
    try:
        before = os.fstat(target_fd)
        if ((before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)):
            raise SafeFilesystemError(f"{label} changed while opening")
        directories, files = _inventory_migration_directory_descriptor(
            target_fd, Path("."), label=label)
        _verify_migration_directory_identity(
            clip_fd, name, target_fd, label=label)
        if (_migration_stat_signature(os.fstat(target_fd))
                != _migration_stat_signature(before)):
            raise SafeFilesystemError(f"{label} changed during inventory")
        return {"directories": directories, "files": files}
    finally:
        os.close(target_fd)


def _migration_inventory_tree(inventory: dict) -> dict:
    """Convert a validated journal inventory into an exact child-name tree."""
    root = {"kind": "directory", "children": {}}
    directories = inventory["directories"]
    if "." not in directories:
        raise SafeFilesystemError("migration directory inventory has no root")

    def add(relative: str, value: dict) -> None:
        parts = Path(relative).parts
        if (not parts or any(part in {"", ".", ".."} for part in parts)):
            raise SafeFilesystemError("migration inventory path is unsafe")
        parent = root
        for part in parts[:-1]:
            child = parent["children"].get(part)
            if child is None or child["kind"] != "directory":
                raise SafeFilesystemError("migration inventory parent is missing")
            parent = child
        if parts[-1] in parent["children"]:
            raise SafeFilesystemError("migration inventory contains duplicate entries")
        parent["children"][parts[-1]] = value

    for relative in sorted(
            (item for item in directories if item != "."),
            key=lambda item: (len(Path(item).parts), item)):
        add(relative, {"kind": "directory", "children": {}})
    for item in inventory["files"]:
        if item["relative_path"] == ".":
            raise SafeFilesystemError("migration directory inventory contains a root file")
        add(item["relative_path"], {"kind": "file", "inventory": item})
    return root


def _validate_migration_file_descriptor(
        parent_fd: int, name: str, expected: dict, *, label: str) -> tuple[int, ...]:
    try:
        before_name = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    if not stat.S_ISREG(before_name.st_mode):
        raise SafeFilesystemError(f"{label} is unsafe")
    descriptor, before = _open_migration_regular_file(
        parent_fd, name, label=label)
    try:
        identity = (before.st_dev, before.st_ino)
        if identity != (before_name.st_dev, before_name.st_ino):
            raise SafeFilesystemError(f"{label} changed while opening")
        if before.st_size != expected["size"]:
            raise SafeFilesystemError(f"{label} size does not match migration journal")
        digest = _hash_migration_descriptor(descriptor)
        after = os.fstat(descriptor)
        if ((after.st_dev, after.st_ino) != identity
                or not stat.S_ISREG(after.st_mode)
                or after.st_size != before.st_size
                or after.st_mtime_ns != before.st_mtime_ns
                or after.st_ctime_ns != before.st_ctime_ns):
            raise SafeFilesystemError(f"{label} changed while hashing")
        if digest != expected["sha256"]:
            raise SafeFilesystemError(f"{label} hash does not match migration journal")
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or _migration_stat_signature(current)
                != _migration_stat_signature(after)):
            raise SafeFilesystemError(f"{label} changed during validation")
        return _migration_stat_signature(after)
    finally:
        os.close(descriptor)


def _validate_migration_inventory_directory(
        descriptor: int, tree: dict, *, label: str) -> None:
    before_directory = os.fstat(descriptor)
    expected_names = set(tree["children"])
    if _migration_directory_names(descriptor, label=label) != expected_names:
        raise SafeFilesystemError(f"{label} contains unexpected entries")
    retained_entries = {}
    for name, expected in tree["children"].items():
        entry_label = f"{label}/{name}"
        if expected["kind"] == "file":
            retained_entries[name] = (
                "file",
                _validate_migration_file_descriptor(
                    descriptor, name, expected["inventory"], label=entry_label),
            )
            continue
        try:
            before_name = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SafeFilesystemError(f"{entry_label} is unsafe") from exc
        if not stat.S_ISDIR(before_name.st_mode):
            raise SafeFilesystemError(f"{entry_label} is unsafe")
        child_fd = _open_migration_directory(
            descriptor, name, label=entry_label)
        try:
            identity = (before_name.st_dev, before_name.st_ino)
            retained = os.fstat(child_fd)
            if (retained.st_dev, retained.st_ino) != identity:
                raise SafeFilesystemError(f"{entry_label} changed while opening")
            _validate_migration_inventory_directory(
                child_fd, expected, label=entry_label)
            _verify_migration_directory_identity(
                descriptor, name, child_fd, label=entry_label)
            after = os.fstat(child_fd)
            if (_migration_stat_signature(after)
                    != _migration_stat_signature(retained)):
                raise SafeFilesystemError(
                    f"{entry_label} changed during validation")
            retained_entries[name] = (
                "directory", _migration_stat_signature(after))
        finally:
            os.close(child_fd)

    if _migration_directory_names(descriptor, label=label) != expected_names:
        raise SafeFilesystemError(f"{label} changed during validation")
    for name, (kind, signature) in retained_entries.items():
        try:
            current = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        except OSError as exc:
            raise SafeFilesystemError(
                f"{label}/{name} changed during validation") from exc
        expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR
        if (not expected_type(current.st_mode)
                or _migration_stat_signature(current) != signature):
            raise SafeFilesystemError(
                f"{label}/{name} changed during validation")
    after_directory = os.fstat(descriptor)
    if (_migration_stat_signature(after_directory)
            != _migration_stat_signature(before_directory)):
        raise SafeFilesystemError(f"{label} changed during validation")


def _validate_migration_destination_descriptors(
        project_fd: int, clips_fd: int, clip_fd: int, journal: dict, *,
        require_all_targets: bool = False) -> None:
    """Validate the exact destination through retained no-follow descriptors."""
    project_before = os.fstat(project_fd)
    clips_before = os.fstat(clips_fd)
    clip_before = os.fstat(clip_fd)
    try:
        _verify_migration_directory_identity(
            project_fd, "clips", clips_fd,
            label="clips migration destination")
        _verify_migration_directory_identity(
            clips_fd, "clip-001", clip_fd,
            label="default clip migration destination")
        if _migration_directory_names(
                clips_fd, label="clips migration destination") != {"clip-001"}:
            raise SafeFilesystemError(
                "clips migration destination contains unexpected entries")

        allowed = {
            Path(mapping["target"]).name: mapping
            for mapping in journal["mappings"] if mapping["present"]
        }
        actual = _migration_directory_names(
            clip_fd, label="default clip migration destination")
        if actual - set(allowed):
            raise SafeFilesystemError(
                "default clip migration destination contains unexpected entries")
        required = {
            Path(mapping["target"]).name
            for mapping in journal["mappings"]
            if mapping["present"] and (
                require_all_targets
                or mapping["source"] in journal["completed"]
                or journal["phase"] in {
                    "targets_verified", "manifest_published", "finalizing"})
        }
        if not required <= actual:
            raise SafeFilesystemError(
                "default clip migration destination is missing verified entries")

        retained_targets = {}
        for name in sorted(actual):
            mapping = allowed[name]
            inventory = mapping["inventory"]
            if mapping["kind"] == "file":
                if (inventory["directories"]
                        or len(inventory["files"]) != 1
                        or inventory["files"][0]["relative_path"] != "."):
                    raise SafeFilesystemError("migration file inventory is invalid")
                retained_targets[name] = (
                    "file",
                    _validate_migration_file_descriptor(
                        clip_fd, name, inventory["files"][0],
                        label=f"default clip migration destination/{name}"),
                )
                continue

            before_name = os.stat(name, dir_fd=clip_fd, follow_symlinks=False)
            if not stat.S_ISDIR(before_name.st_mode):
                raise SafeFilesystemError(f"migration target is unsafe: {name}")
            target_fd = _open_migration_directory(
                clip_fd, name, label=f"migration target {name}")
            try:
                retained = os.fstat(target_fd)
                if ((retained.st_dev, retained.st_ino)
                        != (before_name.st_dev, before_name.st_ino)):
                    raise SafeFilesystemError(
                        f"migration target changed while opening: {name}")
                _validate_migration_inventory_directory(
                    target_fd, _migration_inventory_tree(inventory),
                    label=f"migration target {name}")
                _verify_migration_directory_identity(
                    clip_fd, name, target_fd, label=f"migration target {name}")
                after = os.fstat(target_fd)
                if (_migration_stat_signature(after)
                        != _migration_stat_signature(retained)):
                    raise SafeFilesystemError(
                        f"migration target changed during validation: {name}")
                retained_targets[name] = (
                    "directory", _migration_stat_signature(after))
            finally:
                os.close(target_fd)

        if _migration_directory_names(
                clip_fd, label="default clip migration destination") != actual:
            raise SafeFilesystemError(
                "default clip migration destination changed during validation")
        for name, (kind, signature) in retained_targets.items():
            current = os.stat(name, dir_fd=clip_fd, follow_symlinks=False)
            expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR
            if (not expected_type(current.st_mode)
                    or _migration_stat_signature(current) != signature):
                raise SafeFilesystemError(
                    f"migration target changed during validation: {name}")

        _verify_migration_directory_identity(
            clips_fd, "clip-001", clip_fd,
            label="default clip migration destination")
        _verify_migration_directory_identity(
            project_fd, "clips", clips_fd,
            label="clips migration destination")
        for descriptor, before, label in (
                (clip_fd, clip_before, "default clip migration destination"),
                (clips_fd, clips_before, "clips migration destination"),
                (project_fd, project_before, "migration project")):
            if (_migration_stat_signature(os.fstat(descriptor))
                    != _migration_stat_signature(before)):
                raise SafeFilesystemError(f"{label} changed during validation")
    except OSError as exc:
        raise SafeFilesystemError(
            "migration destination changed during validation") from exc


def _validate_migration_destination_tree(
        project: Path, journal: dict, *, require_all_targets: bool = False) -> None:
    """Open the destination chain once and validate it descriptor-relatively."""
    with open_directory(project.parent) as parent_fd:
        project_fd = clips_fd = clip_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            clips_fd = _open_migration_directory(
                project_fd, "clips", label="clips migration destination")
            clip_fd = _open_migration_directory(
                clips_fd, "clip-001", label="default clip migration destination")
            _validate_migration_destination_descriptors(
                project_fd, clips_fd, clip_fd, journal,
                require_all_targets=require_all_targets)
            _verify_migration_descriptor_chain(
                project, parent_fd, project_fd, clips_fd, clip_fd)
        finally:
            for descriptor in (clip_fd, clips_fd, project_fd):
                if descriptor is not None:
                    os.close(descriptor)


def _verify_migration_descriptor_chain(
        project: Path, parent_fd: int, project_fd: int,
        clips_fd: int, clip_fd: int) -> None:
    verify_absolute_directory_identity(
        project.parent, parent_fd, label="projects parent")
    _verify_migration_directory_identity(
        parent_fd, project.name, project_fd, label="migration project")
    _verify_migration_directory_identity(
        project_fd, "clips", clips_fd, label="clips migration destination")
    _verify_migration_directory_identity(
        clips_fd, "clip-001", clip_fd,
        label="default clip migration destination")


def _read_migration_descriptor_bytes(descriptor: int) -> bytes:
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _verify_migration_named_regular_descriptor(
        parent_fd: int, name: str, descriptor: int, expected: os.stat_result,
        expected_bytes: bytes, *, label: str) -> None:
    try:
        current = os.stat(
            name, dir_fd=parent_fd, follow_symlinks=False)
        retained = os.fstat(descriptor)
        content = _read_migration_descriptor_bytes(descriptor)
    except OSError as exc:
        raise SafeFilesystemError(f"{label} changed during validation") from exc
    signature = _migration_stat_signature(expected)
    if (not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(retained.st_mode)
            or _migration_stat_signature(current) != signature
            or _migration_stat_signature(retained) != signature
            or content != expected_bytes):
        raise SafeFilesystemError(f"{label} changed during validation")


def _verify_migration_journal_descriptor(
        project_fd: int, journal_fd: int, expected: os.stat_result,
        expected_bytes: bytes) -> None:
    _verify_migration_named_regular_descriptor(
        project_fd, CLIP_MIGRATION_JOURNAL, journal_fd, expected,
        expected_bytes, label="clip migration journal")


def _validate_canonical_clip_descriptor(clip_fd: int, clip_id: str) -> tuple[int, ...]:
    """Validate one complete canonical clip through a retained descriptor."""
    label = f"canonical clip {clip_id}"
    before = os.fstat(clip_fd)
    required = {"current_prompt.txt", "generations"}
    optional_files = {
        "current_generation.json",
        # archive_generation() owns this clip-local lock file.
        ".generation-archive.lock",
    }
    names = _migration_directory_names(clip_fd, label=label)
    if not required <= names or names - (required | optional_files):
        raise SafeFilesystemError(f"{label} contains unexpected entries")

    retained = {}
    for name in sorted(({"current_prompt.txt"} | optional_files) & names):
        _inventory, signature = _inventory_migration_regular_file(
            clip_fd, name, ".", label=f"{label}/{name}")
        retained[name] = ("file", signature)

    generations_name = os.stat(
        "generations", dir_fd=clip_fd, follow_symlinks=False)
    if not stat.S_ISDIR(generations_name.st_mode):
        raise SafeFilesystemError(f"{label}/generations is unsafe")
    generations_fd = _open_migration_directory(
        clip_fd, "generations", label=f"{label}/generations")
    try:
        generations_before = os.fstat(generations_fd)
        if ((generations_before.st_dev, generations_before.st_ino)
                != (generations_name.st_dev, generations_name.st_ino)):
            raise SafeFilesystemError(
                f"{label}/generations changed while opening")
        _inventory_migration_directory_descriptor(
            generations_fd, Path("."), label=f"{label}/generations")
        _verify_migration_directory_identity(
            clip_fd, "generations", generations_fd,
            label=f"{label}/generations")
        generations_after = os.fstat(generations_fd)
        if (_migration_stat_signature(generations_after)
                != _migration_stat_signature(generations_before)):
            raise SafeFilesystemError(
                f"{label}/generations changed during validation")
        retained["generations"] = (
            "directory", _migration_stat_signature(generations_after))
    finally:
        os.close(generations_fd)

    if _migration_directory_names(clip_fd, label=label) != names:
        raise SafeFilesystemError(f"{label} changed during validation")
    for name, (kind, signature) in retained.items():
        current = os.stat(name, dir_fd=clip_fd, follow_symlinks=False)
        expected_type = stat.S_ISREG if kind == "file" else stat.S_ISDIR
        if (not expected_type(current.st_mode)
                or _migration_stat_signature(current) != signature):
            raise SafeFilesystemError(
                f"{label}/{name} changed during validation")
    after = os.fstat(clip_fd)
    if _migration_stat_signature(after) != _migration_stat_signature(before):
        raise SafeFilesystemError(f"{label} changed during validation")
    return _migration_stat_signature(after)


def _validate_completed_migration(project: Path) -> None:
    """Read-only validation for any complete canonical clip-store layout."""
    described_manifest = CLIP_STORE.describe(project)
    with open_directory(project.parent) as parent_fd:
        project_fd = clips_fd = manifest_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            project_before = os.fstat(project_fd)
            if not _migration_entry_is_absent(
                    project_fd, CLIP_MIGRATION_JOURNAL,
                    label="clip migration journal"):
                raise SafeFilesystemError(
                    "clip migration journal appeared during assessment")
            for source, _target, _optional, _kind in CLIP_MIGRATION_MAPPINGS:
                if not _migration_entry_is_absent(
                        project_fd, source, label=f"legacy source {source}"):
                    raise ValueError(
                        "legacy sources remain without a migration journal")

            manifest_fd, manifest_details = _open_migration_regular_file(
                project_fd, "project.json", label="project manifest")
            manifest_bytes = _read_migration_descriptor_bytes(manifest_fd)
            _verify_migration_named_regular_descriptor(
                project_fd, "project.json", manifest_fd, manifest_details,
                manifest_bytes, label="project manifest")
            try:
                retained_manifest = json.loads(manifest_bytes)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SafeFilesystemError("project manifest is invalid") from exc
            if CLIP_STORE._validate_manifest(retained_manifest) != described_manifest:
                raise SafeFilesystemError(
                    "project manifest changed during canonical validation")

            clips_fd = _open_migration_directory(
                project_fd, "clips", label="clips migration destination")
            clips_before = os.fstat(clips_fd)
            clip_ids = [entry["id"] for entry in described_manifest["clips"]]
            expected_clip_names = set(clip_ids)
            if _migration_directory_names(
                    clips_fd, label="clips migration destination") != expected_clip_names:
                raise SafeFilesystemError(
                    "clips migration destination contains unexpected entries")

            retained_clips = {}
            for clip_id in clip_ids:
                named = os.stat(
                    clip_id, dir_fd=clips_fd, follow_symlinks=False)
                if not stat.S_ISDIR(named.st_mode):
                    raise SafeFilesystemError(
                        f"canonical clip {clip_id} is unsafe")
                clip_fd = _open_migration_directory(
                    clips_fd, clip_id, label=f"canonical clip {clip_id}")
                try:
                    opened = os.fstat(clip_fd)
                    if ((opened.st_dev, opened.st_ino)
                            != (named.st_dev, named.st_ino)):
                        raise SafeFilesystemError(
                            f"canonical clip {clip_id} changed while opening")
                    retained_clips[clip_id] = (
                        _validate_canonical_clip_descriptor(clip_fd, clip_id))
                    _verify_migration_directory_identity(
                        clips_fd, clip_id, clip_fd,
                        label=f"canonical clip {clip_id}")
                finally:
                    os.close(clip_fd)

            if _migration_directory_names(
                    clips_fd, label="clips migration destination") != expected_clip_names:
                raise SafeFilesystemError(
                    "clips migration destination changed during validation")
            for clip_id, signature in retained_clips.items():
                current = os.stat(
                    clip_id, dir_fd=clips_fd, follow_symlinks=False)
                if (not stat.S_ISDIR(current.st_mode)
                        or _migration_stat_signature(current) != signature):
                    raise SafeFilesystemError(
                        f"canonical clip {clip_id} changed during validation")
            if (_migration_stat_signature(os.fstat(clips_fd))
                    != _migration_stat_signature(clips_before)):
                raise SafeFilesystemError(
                    "clips migration destination changed during validation")

            _verify_migration_named_regular_descriptor(
                project_fd, "project.json", manifest_fd, manifest_details,
                manifest_bytes, label="project manifest")
            for source, _target, _optional, _kind in CLIP_MIGRATION_MAPPINGS:
                if not _migration_entry_is_absent(
                        project_fd, source, label=f"legacy source {source}"):
                    raise ValueError(
                        "legacy sources remain without a migration journal")
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd, label="migration project")
            _verify_migration_directory_identity(
                project_fd, "clips", clips_fd,
                label="clips migration destination")
            if (_migration_stat_signature(os.fstat(project_fd))
                    != _migration_stat_signature(project_before)):
                raise SafeFilesystemError(
                    "migration project changed during canonical validation")
            _require_migration_journal_absent(project_fd)
        finally:
            for descriptor in (manifest_fd, clips_fd, project_fd):
                if descriptor is not None:
                    os.close(descriptor)


def _restore_migration_journal(project_fd: int, content: bytes) -> None:
    """Atomically restore journal bytes without replacing a new journal."""
    temporary = f".{uuid.uuid4().hex}.clip-migration.restore"
    descriptor = None
    linked = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=project_fd,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(
                temporary, CLIP_MIGRATION_JOURNAL,
                src_dir_fd=project_fd, dst_dir_fd=project_fd,
                follow_symlinks=False)
        except FileExistsError as exc:
            raise SafeFilesystemError(
                "replacement clip migration journal was preserved") from exc
        linked = True
        os.unlink(temporary, dir_fd=project_fd)
        os.fsync(project_fd)
    except OSError as exc:
        raise SafeFilesystemError(
            "could not restore clip migration journal") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not linked:
            try:
                os.unlink(temporary, dir_fd=project_fd)
            except FileNotFoundError:
                pass


def _finalize_migration(project: Path, journal: dict) -> None:
    """Validate and remove the journal through one retained descriptor chain."""
    with open_directory(project.parent) as parent_fd:
        project_fd = clips_fd = clip_fd = journal_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            clips_fd = _open_migration_directory(
                project_fd, "clips", label="clips migration destination")
            clip_fd = _open_migration_directory(
                clips_fd, "clip-001",
                label="default clip migration destination")

            journal_name = os.stat(
                CLIP_MIGRATION_JOURNAL, dir_fd=project_fd,
                follow_symlinks=False)
            if not stat.S_ISREG(journal_name.st_mode):
                raise SafeFilesystemError("clip migration journal is unsafe")
            journal_fd, journal_details = _open_migration_regular_file(
                project_fd, CLIP_MIGRATION_JOURNAL,
                label="clip migration journal")
            if ((journal_name.st_dev, journal_name.st_ino)
                    != (journal_details.st_dev, journal_details.st_ino)):
                raise SafeFilesystemError(
                    "clip migration journal changed while opening")
            journal_bytes = _read_migration_descriptor_bytes(journal_fd)
            _verify_migration_journal_descriptor(
                project_fd, journal_fd, journal_details, journal_bytes)
            try:
                retained_journal = _validate_migration_journal(
                    project, json.loads(journal_bytes))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise SafeFilesystemError(
                    "clip migration journal is invalid during finalization") from exc
            if retained_journal != journal:
                raise SafeFilesystemError(
                    "clip migration journal changed before finalization")

            _validate_migration_destination_descriptors(
                project_fd, clips_fd, clip_fd, journal,
                require_all_targets=True)
            _verify_migration_descriptor_chain(
                project, parent_fd, project_fd, clips_fd, clip_fd)
            _verify_migration_journal_descriptor(
                project_fd, journal_fd, journal_details, journal_bytes)

            unlink_attempted = False
            try:
                unlink_attempted = True
                os.unlink(CLIP_MIGRATION_JOURNAL, dir_fd=project_fd)
                os.fsync(project_fd)
                _require_migration_journal_absent(project_fd)
                _validate_migration_destination_descriptors(
                    project_fd, clips_fd, clip_fd, journal,
                    require_all_targets=True)
                _verify_migration_descriptor_chain(
                    project, parent_fd, project_fd, clips_fd, clip_fd)
                # This canonical no-follow absence check must remain last.
                _require_migration_journal_absent(project_fd)
            except BaseException:
                if unlink_attempted:
                    try:
                        if _migration_entry_is_absent(
                                project_fd, CLIP_MIGRATION_JOURNAL,
                                label="clip migration journal"):
                            _restore_migration_journal(project_fd, journal_bytes)
                    except BaseException as restore_exc:
                        raise SafeFilesystemError(
                            "migration finalization failed and journal recovery failed"
                        ) from restore_exc
                raise
        finally:
            for descriptor in (journal_fd, clip_fd, clips_fd, project_fd):
                if descriptor is not None:
                    os.close(descriptor)


def _create_migration_directories(project: Path) -> None:
    with open_directory(project) as project_fd:
        clips_fd = None
        clip_fd = None
        try:
            clips_fd, clips_created = _open_or_create_migration_directory(
                project_fd, "clips", label="clips migration destination")
            _verify_migration_directory_identity(
                project_fd, "clips", clips_fd,
                label="clips migration destination")
            if clips_created:
                os.fsync(clips_fd)
                os.fsync(project_fd)

            clip_fd, clip_created = _open_or_create_migration_directory(
                clips_fd, "clip-001",
                label="default clip migration destination")
            _verify_migration_directory_identity(
                clips_fd, "clip-001", clip_fd,
                label="default clip migration destination")
            _verify_migration_directory_identity(
                project_fd, "clips", clips_fd,
                label="clips migration destination")
            if clip_created:
                os.fsync(clip_fd)
                os.fsync(clips_fd)
        finally:
            if clip_fd is not None:
                os.close(clip_fd)
            if clips_fd is not None:
                os.close(clips_fd)


def _apply_migration_project(project: Path) -> dict:
    report, _journal = _assess_migration_project(project)
    if report["status"] == "already-migrated":
        return report
    with CLIP_STORE.locked_project(project):
        active = _active_migration_jobs([project.name])[project.name]
        if active:
            raise ValueError(
                f"project {project.name} has active jobs: "
                + ", ".join(job["id"] for job in active))
        report, journal = _assess_migration_project(project)
        if report["status"] == "already-migrated":
            return report
        assert journal is not None
        journal_exists = _entry_kind(
            project / CLIP_MIGRATION_JOURNAL) == "file"
        if not journal_exists:
            _atomic_json_file(
                project, CLIP_MIGRATION_JOURNAL, journal, replace=False)
            _migration_checkpoint("journal-prepared")

        _create_migration_directories(project)
        _migration_checkpoint("directories-created")
        _validate_migration_destination_tree(project, journal)
        for mapping in journal["mappings"]:
            state = _mapping_state(project, mapping)
            if state == "source":
                atomic_move_no_replace(
                    project / mapping["source"], project / mapping["target"])
                state = _mapping_state(project, mapping)
                assert state == "target"
            if state == "target" and mapping["source"] not in journal["completed"]:
                journal["completed"].append(mapping["source"])
                journal["phase"] = "moving"
                _atomic_json_file(
                    project, CLIP_MIGRATION_JOURNAL, journal, replace=True)
            if state != "missing-optional":
                _migration_checkpoint(f"moved:{mapping['source']}")
            _validate_migration_destination_tree(project, journal)

        for mapping in journal["mappings"]:
            state = _mapping_state(project, mapping)
            if state not in {"target", "missing-optional"}:
                raise ValueError("migration target verification failed")
        _validate_migration_destination_tree(
            project, journal, require_all_targets=True)
        journal["phase"] = "targets_verified"
        _atomic_json_file(
            project, CLIP_MIGRATION_JOURNAL, journal, replace=True)
        _migration_checkpoint("targets-verified")

        manifest_path = project / "project.json"
        manifest_kind = _entry_kind(manifest_path)
        if manifest_kind == "missing":
            CLIP_STORE._validate_manifest(journal["manifest"])
            _validate_migration_destination_tree(
                project, journal, require_all_targets=True)
            _atomic_json_file(
                project, "project.json", journal["manifest"], replace=False)
        elif manifest_kind == "file":
            _validate_migration_destination_tree(
                project, journal, require_all_targets=True)
            if CLIP_STORE.describe(project) != journal["manifest"]:
                raise ValueError("published manifest does not match migration journal")
        else:
            raise ValueError("project manifest is unsafe")
        _migration_checkpoint("manifest-published")

        journal["phase"] = "manifest_published"
        _atomic_json_file(
            project, CLIP_MIGRATION_JOURNAL, journal, replace=True)
        _migration_checkpoint("journal-manifest-published")
        if CLIP_STORE.describe(project) != journal["manifest"]:
            raise ValueError("published manifest verification failed")
        for mapping in journal["mappings"]:
            if _mapping_state(project, mapping) not in {
                    "target", "missing-optional"}:
                raise ValueError("migration target verification failed")
        journal["phase"] = "finalizing"
        _atomic_json_file(
            project, CLIP_MIGRATION_JOURNAL, journal, replace=True)
        _migration_checkpoint("journal-finalizing")
        report = _report_from_journal(project, journal, "migrated")
        _finalize_migration(project, journal)
        report["phase"] = "complete"
        return report


def migrate_clips(root: Path, project: str | None = None, *,
                  apply: bool = False) -> dict:
    """Plan or explicitly apply the resumable legacy Project→clip migration."""
    if project is None:
        project_ids = list_projects(root)
    else:
        project_ids = [project_path(root, project).name]
    projects = [project_path(root, project_id) for project_id in project_ids]
    assessed = []
    for selected in projects:
        report, _journal = _assess_migration_project(selected)
        assessed.append(report)
    active = _active_migration_jobs(project_ids)
    for report in assessed:
        report["active_jobs"] = active[report["project"]]
    if apply:
        blocked = [project_id for project_id in project_ids if active[project_id]]
        if blocked:
            raise ValueError(
                "cannot migrate projects with active jobs: " + ", ".join(blocked))
        assessed = [_apply_migration_project(selected) for selected in projects]
    return {
        "schema_version": CLIP_MIGRATION_SCHEMA_VERSION,
        "command": "migrate-clips",
        "mode": "apply" if apply else "dry-run",
        "root": str(root),
        "projects": assessed,
        "summary": {
            "project_count": len(assessed),
            "already_migrated": sum(
                item["status"] == "already-migrated" for item in assessed),
            "migratable": sum(
                item["status"] in {"planned", "resumable", "migrated"}
                for item in assessed),
            "active_job_count": sum(
                len(item["active_jobs"]) for item in assessed),
            "file_count": sum(item["file_count"] for item in assessed),
            "total_bytes": sum(item["total_bytes"] for item in assessed),
        },
    }


# ------------------------------------------------------------- prompt & chat

def clip_path(root: Path, project: str, clip_id: str) -> Path:
    """Resolve one exact manifest-owned clip ID within an exact project."""
    pp = project_path(root, project)
    return CLIP_STORE.resolve_clip(pp, clip_id)


def write_prompt(root: Path, project: str, clip_id: str, prompt: str) -> Path:
    clip = clip_path(root, project, clip_id)
    out = clip / "current_prompt.txt"
    if os.path.lexists(out) and (out.is_symlink() or not out.is_file()):
        raise ValueError("current prompt is not a regular clip file")
    temp = clip / f".{uuid.uuid4().hex}.current-prompt"
    try:
        temp.write_text(prompt.rstrip() + "\n", encoding="utf-8")
        temp.replace(out)
    finally:
        temp.unlink(missing_ok=True)
    return out


def append_chat(root: Path, project: str, role: str, content: str) -> None:
    pp = project_path(root, project)
    configured_root = Path(
        os.environ.get("DESIGN_STUDIO_ROOT", DEFAULT_ROOT)
    ).expanduser().resolve()
    if root.resolve() == configured_root:
        # Lazy import avoids a module cycle: webapp routes import this CLI core.
        from webapp.job_store import JobStore

        runtime = Path(os.environ.get(
            "HERMES_STUDIO_RUNTIME_ROOT", DEFAULT_RUNTIME
        )).expanduser().resolve()
        store = JobStore(runtime / "studio.db")
        store.initialize()
        store.import_chat_if_empty(pp.name, pp / "chat.jsonl")
        store.append_external_event(pp.name, role, content)
        store.export_chat(pp.name, pp / "chat.jsonl")
        return
    entry = {"role": role,
             "content": content,
             "ts": _dt.datetime.now().isoformat(timespec="seconds")}
    # O_APPEND + single write: atomic enough for line-sized records even with
    # concurrent writers (threads / CLI + webapp).
    data = json.dumps(entry, ensure_ascii=False).encode("utf-8") + b"\n"
    lock_path = pp / ".chat.lock"
    chat_path = pp / "chat.jsonl"
    if lock_path.is_symlink() or chat_path.is_symlink():
        raise ValueError("project chat files may not be symlinks")
    lock_fd = os.open(
        lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            fd = os.open(
                chat_path,
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
            )
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------- generation

def next_generation_dir(root: Path, project: str, clip_id: str) -> Path:
    clip = clip_path(root, project, clip_id)
    generations = clip / "generations"
    if (generations.is_symlink() or not generations.is_dir()
            or generations.resolve().parent != clip):
        raise ValueError("generations directory is not a regular clip directory")
    numbers = []
    for entry in generations.iterdir():
        if not entry.name.isdigit():
            continue
        if entry.is_symlink() or not entry.is_dir():
            raise ValueError(f"generation entry is unsafe: {entry.name}")
        numbers.append(int(entry.name))
    return generations / f"{max(numbers, default=0) + 1:03d}"


def read_project_text(project: Path, filename: str, limit: int | None = None,
                      *, required: bool = False) -> str:
    """Read safe metadata, returning empty for unsafe optional entries."""
    if Path(filename).name != filename:
        raise ValueError(f"invalid project filename: {filename!r}")
    path = project / filename
    try:
        with open_regular_file(path) as opened:
            value = read_opened_text(opened)
    except FileNotFoundError as exc:
        if required:
            raise ValueError(f"{filename} is missing") from exc
        return ""
    except (SafeFilesystemError, OSError, UnicodeDecodeError) as exc:
        if required:
            raise ValueError(f"{filename} is not a regular file") from exc
        return ""
    return value[:limit] if limit is not None else value


def archive_outputs(root: Path, project: str, clip_id: str,
                    outputs: list[str], metadata: dict | None = None,
                    source_root: Path | None = None,
                    transport: str = "comfyui-mcp",
                    prompt_text: str | None = None) -> Path:
    """Archive outputs beneath one exact clip from one trusted source root."""
    clip = clip_path(root, project, clip_id)
    output_root = Path(os.path.abspath(
        os.path.expanduser(os.fspath(source_root or COMFY_OUTPUT))))
    with ExitStack() as source_descriptors:
        sources = []
        for output in outputs:
            try:
                source = source_descriptors.enter_context(
                    open_regular_beneath(output_root, output))
            except FileNotFoundError as exc:
                raise FileNotFoundError(
                    f"ComfyUI output not found: {output}") from exc
            except SafeFilesystemError as exc:
                raise ValueError(
                    "output may not be a symlink, special file, or escape "
                    f"the ComfyUI output directory: {output!r}"
                ) from exc
            if source.name in {"prompt.txt", "settings.json", "meta.json"}:
                raise ValueError(f"output filename is reserved: {source.name}")
            sources.append(source)
        if not sources:
            raise ValueError("at least one output file is required")

        generations = clip / "generations"
        if (generations.is_symlink() or not generations.is_dir()
                or generations.resolve().parent != clip):
            raise ValueError("generations directory is not a regular clip directory")
        lock_path = clip / ".generation-archive.lock"
        if lock_path.is_symlink():
            raise ValueError("generation archive lock may not be a symlink")
        lock_fd = os.open(
            lock_path, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600)
        with os.fdopen(lock_fd, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            gen_dir = next_generation_dir(root, project, clip_id)
            staging = generations / f".publishing-{uuid.uuid4().hex}"
            staging.mkdir()
            copied = []
            try:
                for source in sources:
                    target = staging / source.name
                    if target.exists():
                        raise FileExistsError(
                            f"duplicate output filename: {source.name}")
                    copy_opened_file(source, target)
                    copied.append(target.name)
                archived_prompt = (
                    read_project_text(clip, "current_prompt.txt", required=True)
                    if prompt_text is None else prompt_text.rstrip() + "\n"
                )
                (staging / "prompt.txt").write_text(
                    archived_prompt, encoding="utf-8")
                settings_path = clip / "current_generation.json"
                try:
                    with open_regular_file(settings_path) as settings:
                        copy_opened_file(settings, staging / "settings.json")
                except FileNotFoundError:
                    # JSON null is the stable snapshot for an unsaved state.
                    (staging / "settings.json").write_text(
                        "null\n", encoding="utf-8")
                except SafeFilesystemError as exc:
                    raise ValueError(
                        "current generation settings are not a regular clip file"
                    ) from exc
                meta = {
                    **(metadata or {}),
                    "generated": _dt.datetime.now().isoformat(timespec="seconds"),
                    "transport": transport,
                    "files": copied,
                    "sources": [str(source.path) for source in sources],
                }
                (staging / "meta.json").write_text(
                    json.dumps(meta, indent=2) + "\n", encoding="utf-8")
                atomic_publish_directory(staging, gen_dir)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    return gen_dir


def dispatch_profile(root: Path, project: str, profile: str, task: str,
                     timeout: int = 1800) -> str:
    """Run one serialized local specialist handoff with a persistent session."""
    if profile not in LOCAL_SPECIALIST_PROFILES:
        raise ValueError(f"unsupported Studio specialist profile: {profile}")
    pp = project_path(root, project)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{pp.name}.{profile}"
    lock_path = root / "tmp" / ".profile-dispatch.lock"
    command = ["hermes", "-p", profile]
    session_id = None
    if session_file.exists():
        candidate = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", candidate):
            session_id = candidate
            command += ["-r", candidate]
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n\n"
        f"Handoff from the studio orchestrator:\n{task}"
    )
    command += [
        "chat", "-Q", "-t", SPECIALIST_TOOLSETS[profile],
        "--source", "studio-handoff",
        "-q", prompt,
    ]

    bridge = None
    store = None
    settings = None
    specialist_job = None
    bridge_type = None
    job_id = os.environ.get("HERMES_STUDIO_JOB_ID", "").strip()
    if job_id:
        try:
            from webapp.config import Settings
            from webapp.hermes_events import HermesSessionEventBridge
            from webapp.job_store import JobStore

            settings = Settings.from_environment()
            store = JobStore(settings.database_path)
            store.initialize()
            parent_job = store.get_job(job_id)
            specialist_job = replace(parent_job, profile=profile)
            bridge_type = HermesSessionEventBridge
        except Exception:
            store = None
            settings = None
            specialist_job = None
            bridge_type = None

    with lock_path.open("a+b") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if store and settings and specialist_job and bridge_type:
            store.append_job_event(
                job_id,
                profile,
                "handoff.started",
                f"Handoff started: {profile}",
                status="running",
                detail={"task": task[:500]},
            )
            bridge = bridge_type(
                store,
                settings,
                specialist_job,
                source="studio-handoff",
                started_at=time.time(),
                session_id=session_id,
            )
            bridge.prepare()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        while True:
            if bridge:
                bridge.poll()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                process.communicate()
                if store:
                    store.append_job_event(
                        job_id, profile, "handoff.failed",
                        f"Handoff timed out after {timeout}s", status="failed")
                raise TimeoutError(
                    f"Studio specialist {profile} timed out after {timeout}s")
            try:
                stdout, stderr = process.communicate(timeout=min(1.0, remaining))
                if bridge:
                    bridge.poll()
                break
            except subprocess.TimeoutExpired:
                continue

    if process.returncode:
        error = stderr.strip() or f"exit code {process.returncode}"
        if store:
            store.append_job_event(
                job_id, profile, "handoff.failed",
                f"{profile} failed", status="failed",
                detail={"error": error[:500]})
        raise RuntimeError(f"{profile} failed ({process.returncode}): {error}")
    reply = stdout.strip()
    if not reply:
        raise RuntimeError(f"{profile} returned an empty reply")
    match = SESSION_ID_RE.search(stderr)
    resolved_session = match.group(1) if match else (
        bridge.session_id if bridge else session_id)
    if resolved_session:
        temp = session_file.with_suffix(session_file.suffix + ".tmp")
        temp.write_text(resolved_session + "\n", encoding="utf-8")
        temp.replace(session_file)
    if store:
        store.append_job_event(
            job_id, profile, "handoff.completed",
            f"Handoff completed: {profile}", status="completed",
            detail={"session_id": resolved_session or ""})
    return reply


def dispatch_grok(root: Path, project: str, task: str,
                  timeout: int = 600) -> str:
    """Run a persistent per-project task on the xAI backup profile."""
    pp = project_path(root, project)
    session_dir = root / "tmp" / "profile-sessions"
    session_dir.mkdir(parents=True, exist_ok=True)
    session_file = session_dir / f"{pp.name}.studio-grok"
    cmd = ["hermes", "-p", "studio-grok"]
    if session_file.exists():
        session_id = session_file.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
            cmd += ["-r", session_id]
    prompt = (
        f"Hermes Studio project id: {pp.name}\n"
        f"Project path: {pp}\n\n"
        f"Task from the Studio orchestrator:\n{task}"
    )
    cmd += ["chat", "-Q", "-t",
            "web,x_search,image_gen,vision,file,terminal", "-q", prompt]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(
            f"studio-grok failed ({result.returncode}): {result.stderr.strip()}")
    reply = result.stdout.strip()
    if not reply:
        raise RuntimeError("studio-grok returned an empty reply")
    match = SESSION_ID_RE.search(result.stderr)
    if match:
        temp = session_file.with_suffix(".tmp")
        temp.write_text(match.group(1) + "\n", encoding="utf-8")
        temp.replace(session_file)
    return reply


def run_generation(root: Path, project: str, clip_id: str,
                   handoff: str | None = None,
                   extra_args: list[str] | None = None,
                   timeout: int = 7200, dry_run: bool = False) -> dict:
    """Submit an H3 generation via the proven run_h3.py runner, then archive."""
    clip_path(root, project, clip_id)
    cmd = [sys.executable, str(RUN_H3), "--comfy-root", str(COMFY_ROOT),
           "-o", "/dev/stdout"]
    if handoff:
        h = Path(handoff).expanduser()
        if not h.exists():  # same archive fallback run_h3.py uses
            h = Path.home() / "Documents/MinimaxH3" / h.name
        if not h.exists():
            raise FileNotFoundError(f"handoff not found: {handoff}")
        cmd += ["--handoff", str(h)]
    if extra_args:
        cmd += extra_args

    if dry_run:
        result = subprocess.run(cmd + ["--dry-run"], capture_output=True, text=True)
        return {"dry_run": True, "ok": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout[-4000:], "stderr": result.stderr[-2000:]}

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout)
    except subprocess.TimeoutExpired:
        interrupt_comfy()
        free_comfy_vram()
        raise
    cleanup = free_comfy_vram()

    summary = {}
    try:  # runner writes its summary JSON via -o /dev/stdout
        summary = json.loads(result.stdout[result.stdout.index("{"):])
    except Exception:
        pass
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-3000:], "summary": summary,
                "vram_cleanup": cleanup}

    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "handoff": str(handoff or ""), "runner": str(RUN_H3),
            "vram_cleanup": cleanup, **summary}

    video = None
    for key in ("video", "output", "path"):  # locate produced file in summary
        v = summary.get(key)
        if v and Path(v).exists():
            video = Path(v)
            break
    if not video:
        return {"ok": False, "error": "runner completed without a video output",
                "summary": summary, "vram_cleanup": cleanup}
    meta["source"] = str(video)
    gen_dir = archive_outputs(
        root, project, clip_id, [str(video)], meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct")
    # preview.jpg is created by the reviewer/user; do not extract frames automatically.
    return {"ok": cleanup.get("ok", False), "generation": str(gen_dir),
            "meta": meta}


def run_image_generation(root: Path, project: str, clip_id: str,
                         recipe: str, prompt: str = "",
                         image: str | None = None, extra_args: list[str] | None = None,
                         timeout: int = 900) -> dict:
    """Krea 2 still image via scripts/krea2_image.py, archived like generations."""
    clip_path(root, project, clip_id)
    prefix = f"studio_{slugify(project)}"
    cmd = [sys.executable, str(KREA2), "--recipe", recipe, "--prefix", prefix]
    if prompt:
        cmd += ["--prompt", prompt]
    if image:
        cmd += ["--image", str(Path(image).expanduser())]
    if extra_args:
        cmd += extra_args

    print(f"[design-studio] submitting: {' '.join(cmd)}", file=sys.stderr)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    seed, prompt_id, files = None, None, []
    # runner prints a one-line summary JSON then an indented result JSON
    dec, pos = json.JSONDecoder(), 0
    while True:
        start = result.stdout.find("{", pos)
        if start < 0:
            break
        try:
            obj, end = dec.raw_decode(result.stdout[start:])
        except ValueError:
            pos = start + 1
            continue
        if isinstance(obj, dict):
            seed = obj.get("seed", seed)
            prompt_id = obj.get("prompt_id", prompt_id)
            f = obj.get("files")
            if isinstance(f, list) and f:
                files = [x for x in f if isinstance(x, str)]
        pos = start + end
    if result.returncode != 0:
        return {"ok": False, "returncode": result.returncode,
                "stderr": result.stderr[-2000:] or result.stdout[-2000:]}

    if not files:
        return {"ok": False, "error": "runner completed without image outputs",
                "prompt_id": prompt_id}
    meta = {"generated": _dt.datetime.now().isoformat(timespec="seconds"),
            "kind": "image", "recipe": recipe, "prompt": prompt,
            "input_image": str(image or ""), "seed": seed,
            "prompt_id": prompt_id}
    gen_dir = archive_outputs(
        root, project, clip_id, files, meta, source_root=COMFY_OUTPUT,
        transport="legacy-direct", prompt_text=prompt)
    meta = json.loads((gen_dir / "meta.json").read_text(encoding="utf-8"))
    return {"ok": True, "generation": str(gen_dir), "meta": meta}


# ---------------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", help="studio root (default: $DESIGN_STUDIO_ROOT or repo studio-root/)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("create-project")
    sp.add_argument("name"); sp.add_argument("brief", nargs="*", default=[])

    sub.add_parser("list-projects")

    sp = sub.add_parser("migrate-clips")
    mode = sp.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    sp.add_argument("project", nargs="?")

    sp = sub.add_parser("list-clips")
    sp.add_argument("project")

    sp = sub.add_parser("create-clip")
    sp.add_argument("project"); sp.add_argument("title")

    sp = sub.add_parser("update-clip")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--title")
    enabled = sp.add_mutually_exclusive_group()
    enabled.add_argument("--enable", dest="enabled", action="store_true")
    enabled.add_argument("--disable", dest="enabled", action="store_false")
    sp.set_defaults(enabled=None)

    sp = sub.add_parser("reorder-clips")
    sp.add_argument("project"); sp.add_argument("clips", nargs="+")

    sp = sub.add_parser("select-take")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("generation", nargs="?"); sp.add_argument("filename", nargs="?")
    sp.add_argument("--clear", action="store_true")

    sp = sub.add_parser("write-prompt")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("prompt", nargs="+")

    sp = sub.add_parser("append-chat")
    sp.add_argument("project"); sp.add_argument("role"); sp.add_argument("content")

    sp = sub.add_parser("generate")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--handoff")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra run_h3.py arg, repeatable. '--arg --turbo' style: use e.g. --arg=--mp --arg=0.9")
    sp.add_argument("--timeout", type=int, default=7200)
    sp.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("generate-image")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("--recipe", required=True,
                    choices=["t2i", "t2i-nvfp4", "style-ref", "upscale", "edit"])
    sp.add_argument("--prompt", default="")
    sp.add_argument("--image", help="input image for style-ref / upscale / edit")
    sp.add_argument("--ref-boost", type=float, default=None, help="edit recipe")
    sp.add_argument("--arg", action="append", default=[],
                    help="extra krea2_image.py arg, e.g. --arg=--aspect --arg=16:9")
    sp.add_argument("--timeout", type=int, default=900)

    sp = sub.add_parser("archive-output")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--kind", default="")
    sp.add_argument("--recipe", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("archive-grok")
    sp.add_argument("project"); sp.add_argument("clip")
    sp.add_argument("outputs", nargs="+")
    sp.add_argument("--prompt-id", default="")
    sp.add_argument("--meta-json", default="{}")

    sp = sub.add_parser("dispatch-grok")
    sp.add_argument("project")
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=600)

    sp = sub.add_parser("dispatch-profile")
    sp.add_argument("project")
    sp.add_argument("profile", choices=sorted(LOCAL_SPECIALIST_PROFILES))
    sp.add_argument("task")
    sp.add_argument("--timeout", type=int, default=1800)

    args = ap.parse_args(argv)
    root = studio_root(
        args.root,
        create=not (args.cmd == "migrate-clips" and args.dry_run),
    )

    if args.cmd == "create-project":
        print(create_project(root, args.name, " ".join(args.brief)))
    elif args.cmd == "list-projects":
        print("\n".join(list_projects(root)) or "(no projects)")
    elif args.cmd == "migrate-clips":
        print(json.dumps(
            migrate_clips(root, args.project, apply=args.apply),
            indent=2,
            ensure_ascii=False,
            sort_keys=True,
        ))
    elif args.cmd == "list-clips":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.describe(pp), indent=2, ensure_ascii=False))
    elif args.cmd == "create-clip":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.create_clip(pp, args.title), indent=2, ensure_ascii=False))
    elif args.cmd == "update-clip":
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.update_clip(
            pp, args.clip, title=args.title, enabled=args.enabled),
            indent=2, ensure_ascii=False))
    elif args.cmd == "reorder-clips":
        pp = project_path(root, args.project)
        print(json.dumps(
            CLIP_STORE.reorder(pp, args.clips), indent=2, ensure_ascii=False))
    elif args.cmd == "select-take":
        if args.clear and (args.generation is not None or args.filename is not None):
            ap.error("--clear cannot be combined with a generation or filename")
        if not args.clear and (args.generation is None or args.filename is None):
            ap.error("selection requires both generation and filename, or --clear")
        pp = project_path(root, args.project)
        print(json.dumps(CLIP_STORE.select_take(
            pp, args.clip, None if args.clear else args.generation,
            None if args.clear else args.filename), indent=2, ensure_ascii=False))
    elif args.cmd == "write-prompt":
        print(write_prompt(
            root, args.project, args.clip, " ".join(args.prompt)))
    elif args.cmd == "append-chat":
        append_chat(root, args.project, args.role, args.content)
        print("appended")
    elif args.cmd == "generate":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        out = run_generation(root, args.project, args.clip, args.handoff, extra,
                             timeout=args.timeout, dry_run=args.dry_run)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") or out.get("dry_run") else 1
    elif args.cmd == "generate-image":
        extra = []
        for a in args.arg:
            extra.extend(shlex.split(a))
        if args.ref_boost is not None:
            extra += ["--ref-boost", str(args.ref_boost)]
        out = run_image_generation(
            root, args.project, args.clip, args.recipe, args.prompt,
            image=args.image, extra_args=extra, timeout=args.timeout)
        print(json.dumps(out, indent=2))
        return 0 if out.get("ok") else 1
    elif args.cmd == "archive-output":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        if args.kind:
            meta["kind"] = args.kind
        if args.recipe:
            meta["recipe"] = args.recipe
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta))
    elif args.cmd == "archive-grok":
        try:
            meta = json.loads(args.meta_json)
        except json.JSONDecodeError as exc:
            ap.error(f"invalid --meta-json: {exc}")
        if not isinstance(meta, dict):
            ap.error("--meta-json must be a JSON object")
        if args.prompt_id:
            meta["prompt_id"] = args.prompt_id
        meta.setdefault("kind", "image")
        meta.setdefault("recipe", "grok-imagine-image-quality")
        print(archive_outputs(
            root, args.project, args.clip, args.outputs, meta,
            source_root=GROK_IMAGE_OUTPUT, transport="xai-imagine"))
    elif args.cmd == "dispatch-grok":
        print(dispatch_grok(root, args.project, args.task, args.timeout))
    elif args.cmd == "dispatch-profile":
        print(dispatch_profile(
            root, args.project, args.profile, args.task, args.timeout))
    return 0


if __name__ == "__main__":
    sys.exit(main())
