"""Resumable one-off migration from legacy project media to clip storage."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from studio_core.safe_files import (
    SafeFilesystemError,
    atomic_exchange_regular_file_at,
    atomic_move_no_replace,
    atomic_move_no_replace_at,
    atomic_remove_regular_file_at,
    open_directory,
    open_regular_file,
    verify_absolute_directory_identity,
)

DEFAULT_RUNTIME = Path(__file__).resolve().parent.parent / ".runtime"


def slugify(name: str) -> str:
    value = re.sub(r"[^a-z0-9_]+", "-", name.strip().lower()).strip("-")
    if not value:
        raise ValueError("name must contain at least one letter or digit")
    return value

CLIP_MIGRATION_JOURNAL = ".clip-migration.json"
CLIP_MIGRATION_RESTORE_RE = re.compile(
    r"\.[0-9a-f]{32}\.clip-migration\.restore")
CLIP_MIGRATION_TEMP_RE = re.compile(
    r"\.[0-9a-f]{32}\.\.clip-migration\.json\.tmp")
CLIP_MIGRATION_SCHEMA_VERSION = 1
CLIP_MIGRATION_MAPPINGS = (
    ("current_prompt.txt", "clips/clip-001/current_prompt.txt", False, "file"),
    ("current_generation.json", "clips/clip-001/current_generation.json", True, "file"),
    ("generations", "clips/clip-001/generations", False, "directory"),
)


@dataclass(frozen=True)
class _MigrationJournalState:
    value: dict
    content: bytes
    identity: tuple[int, int]


_MIGRATION_PHASES = (
    "prepared", "moving", "targets_verified", "manifest_published", "finalizing")
_MIGRATION_PHASE_RANK = {
    phase: index for index, phase in enumerate(_MIGRATION_PHASES)
}


def _migration_checkpoint(_name: str) -> None:
    """Test seam for simulating process interruption at durable boundaries."""

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
def list_projects(root: Path) -> list[str]:
    projects = root / "projects"
    if not projects.is_dir():
        return []
    return sorted(d.name for d in projects.iterdir()
                  if d.is_dir() and not d.is_symlink())

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
    if value["phase"] not in _MIGRATION_PHASE_RANK:
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
    present_sources = [
        mapping["source"] for mapping in mappings if mapping["present"]
    ]
    if completed != present_sources[:len(completed)]:
        raise ValueError("clip migration completed list is not monotonic")
    if value["phase"] == "prepared" and completed:
        raise ValueError("prepared clip migration journal is already completed")
    if value["phase"] == "moving" and not completed:
        raise ValueError("moving clip migration journal has no completed entry")
    if (_MIGRATION_PHASE_RANK[value["phase"]]
            >= _MIGRATION_PHASE_RANK["targets_verified"]
            and completed != present_sources):
        raise ValueError("verified clip migration journal is incomplete")
    return value


def _migration_json_bytes(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
            + "\n").encode("utf-8")


def _read_migration_journal_state_at(
        project: Path, project_fd: int, name: str,
        *, label: str) -> _MigrationJournalState:
    descriptor = None
    try:
        named = os.stat(name, dir_fd=project_fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode):
            raise SafeFilesystemError(f"{label} is unsafe")
        descriptor, opened = _open_migration_regular_file(
            project_fd, name, label=label)
        if ((named.st_dev, named.st_ino)
                != (opened.st_dev, opened.st_ino)):
            raise SafeFilesystemError(f"{label} changed while opening")
        content = _read_migration_descriptor_bytes(descriptor)
        _verify_migration_named_regular_descriptor(
            project_fd, name, descriptor, opened, content, label=label)
        try:
            value = _validate_migration_journal(project, json.loads(content))
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise SafeFilesystemError(f"{label} is invalid") from exc
        if content != _migration_json_bytes(value):
            raise SafeFilesystemError(f"{label} is not deterministic JSON")
        return _MigrationJournalState(
            value=value,
            content=content,
            identity=(opened.st_dev, opened.st_ino),
        )
    except (FileNotFoundError, SafeFilesystemError):
        raise
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is unsafe") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_migration_journal_state(project: Path) -> _MigrationJournalState:
    with open_directory(project.parent) as parent_fd:
        project_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            state = _read_migration_journal_state_at(
                project, project_fd, CLIP_MIGRATION_JOURNAL,
                label="clip migration journal")
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd, label="migration project")
            return state
        finally:
            if project_fd is not None:
                os.close(project_fd)


def _read_migration_journal(project: Path) -> dict:
    try:
        return _read_migration_journal_state(project).value
    except (FileNotFoundError, SafeFilesystemError) as exc:
        raise ValueError("clip migration journal is missing, invalid, or unsafe") from exc


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


def _assess_migration_project(
        project: Path, clip_store,
) -> tuple[dict, _MigrationJournalState | None]:
    lock_kind = _entry_kind(project / ".project.lock")
    if lock_kind not in {"missing", "file"}:
        raise ValueError("project lock is unsafe")
    journal_kind = _entry_kind(project / CLIP_MIGRATION_JOURNAL)
    if journal_kind not in {"missing", "file"}:
        raise ValueError("clip migration journal is unsafe")
    manifest_kind = _entry_kind(project / "project.json")
    if manifest_kind not in {"missing", "file"}:
        raise ValueError("project manifest is unsafe")
    temp_names = _migration_temp_names_for_project(project)

    if journal_kind == "file":
        state = _read_migration_journal_state(project)
        _inspect_migration_journal_temps(project, state)
        journal = state.value
        _validate_migration_tree(project, journal_exists=True)
        if manifest_kind == "file":
            manifest = clip_store.describe(project)
            if manifest != journal["manifest"]:
                raise ValueError(
                    "published manifest does not match the migration journal")
        report = _report_from_journal(project, journal, "resumable")
        return report, state

    _require_no_migration_restore_artifacts_for_project(project)
    source_kinds = {
        source: _entry_kind(project / source)
        for source, _target, _optional, _kind in CLIP_MIGRATION_MAPPINGS
    }
    if manifest_kind == "file":
        if temp_names:
            raise SafeFilesystemError(
                "clip migration journal temp remains without a journal")
        if any(kind != "missing" for kind in source_kinds.values()):
            raise ValueError("legacy sources remain without a migration journal")
        _validate_completed_migration(project, clip_store)
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

    if temp_names:
        candidates = _inspect_migration_journal_temps(project, None)
        if len(candidates) != 1:
            raise SafeFilesystemError(
                "clip migration journal temp recovery is ambiguous")
        candidate = candidates[0][1]
        journal = candidate.value
        _validate_recoverable_journal_state(project, journal)
        return _report_from_journal(project, journal, "resumable"), candidate

    _validate_migration_tree(project, journal_exists=False)
    journal = _new_migration_journal(project)
    return _report_from_journal(project, journal, "planned"), None


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


def _atomic_json_file(
        project: Path, filename: str, value: dict, *, replace: bool,
        expected: _MigrationJournalState | None = None,
) -> _MigrationJournalState | None:
    target_kind = _entry_kind(project / filename)
    if replace:
        if filename != CLIP_MIGRATION_JOURNAL or expected is None:
            raise ValueError("atomic JSON replacement requires expected journal state")
        if target_kind != "file":
            raise ValueError(f"atomic JSON target is missing or unsafe: {filename}")
        if (expected.content != _migration_json_bytes(expected.value)
                or _validate_migration_journal(project, expected.value)
                != expected.value):
            raise ValueError("expected clip migration journal state is invalid")
    elif expected is not None:
        raise ValueError("atomic no-replace JSON publication has no expected target")
    elif target_kind != "missing":
        raise ValueError(f"atomic JSON target already exists: {filename}")
    project_fd = os.open(
        project, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    temporary = f".{uuid.uuid4().hex}.{filename}.tmp"
    descriptor = None
    temporary_identity = None
    publication_attempted = False
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=project_fd,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SafeFilesystemError("atomic JSON temp is unsafe")
        temporary_identity = (details.st_dev, details.st_ino)
        data = _migration_json_bytes(value)
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        if replace:
            assert expected is not None
            current = _read_migration_journal_state_at(
                project, project_fd, filename, label="clip migration journal")
            if (current.identity != expected.identity
                    or current.content != expected.content
                    or current.value != expected.value):
                raise SafeFilesystemError(
                    "clip migration journal changed before CAS publication")
            publication_attempted = True
            published_identity = atomic_exchange_regular_file_at(
                project_fd, temporary, filename,
                expected_source_identity=temporary_identity,
                expected_target_identity=expected.identity,
                label="clip migration journal CAS",
            )
            if published_identity != temporary_identity:
                raise SafeFilesystemError(
                    "clip migration journal CAS published the wrong identity")
        else:
            publication_attempted = True
            published_identity = atomic_move_no_replace_at(
                project_fd, temporary, filename,
                expected_source_identity=temporary_identity)
            if published_identity != temporary_identity:
                raise SafeFilesystemError("atomic JSON publication identity changed")
        published = True
        os.fsync(project_fd)
        if filename == CLIP_MIGRATION_JOURNAL:
            state = _read_migration_journal_state_at(
                project, project_fd, filename, label="clip migration journal")
            if (state.identity != temporary_identity
                    or state.content != data or state.value != value):
                raise SafeFilesystemError(
                    "published clip migration journal changed")
            return state
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if (not published and not publication_attempted
                and temporary_identity is not None):
            try:
                atomic_remove_regular_file_at(
                    project_fd, temporary, temporary_identity,
                    label="atomic JSON temp")
            except FileNotFoundError:
                pass
        os.close(project_fd)


def _write_migration_journal(
        project: Path, value: dict, *,
        expected: _MigrationJournalState | None = None,
) -> _MigrationJournalState:
    state = _atomic_json_file(
        project, CLIP_MIGRATION_JOURNAL, value,
        replace=expected is not None, expected=expected)
    if state is None:
        raise SafeFilesystemError(
            "clip migration journal publication returned no state")
    return state


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
        "chat.jsonl",
        ".chat.lock",
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


def _validate_completed_migration(project: Path, clip_store) -> None:
    """Read-only validation for any complete canonical clip-store layout."""
    described_manifest = clip_store.describe(project)
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
            _require_no_migration_temp_artifacts(project_fd)
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
            if clip_store._validate_manifest(retained_manifest) != described_manifest:
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
            _require_no_migration_temp_artifacts(project_fd)
            _require_migration_journal_absent(project_fd)
        finally:
            for descriptor in (manifest_fd, clips_fd, project_fd):
                if descriptor is not None:
                    os.close(descriptor)


def _migration_restore_names(project_fd: int) -> list[str]:
    return sorted(
        name for name in _migration_directory_names(
            project_fd, label="migration project")
        if CLIP_MIGRATION_RESTORE_RE.fullmatch(name))


def _migration_temp_names(project_fd: int) -> list[str]:
    return sorted(
        name for name in _migration_directory_names(
            project_fd, label="migration project")
        if CLIP_MIGRATION_TEMP_RE.fullmatch(name))


def _migration_temp_names_for_project(project: Path) -> list[str]:
    with open_directory(project.parent) as parent_fd:
        project_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            names = _migration_temp_names(project_fd)
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd, label="migration project")
            return names
        finally:
            if project_fd is not None:
                os.close(project_fd)


def _journals_share_migration(value: dict, other: dict) -> bool:
    return all(value[key] == other[key] for key in (
        "schema_version", "project", "manifest", "mappings"))


def _journal_is_immediate_predecessor(previous: dict, current: dict) -> bool:
    if not _journals_share_migration(previous, current):
        return False
    previous_phase = previous["phase"]
    current_phase = current["phase"]
    previous_completed = previous["completed"]
    current_completed = current["completed"]
    if previous_phase == current_phase == "moving":
        return (len(current_completed) == len(previous_completed) + 1
                and current_completed[:-1] == previous_completed)
    if previous_phase == "prepared" and current_phase == "moving":
        return len(current_completed) == 1 and not previous_completed
    return (
        _MIGRATION_PHASE_RANK[current_phase]
        == _MIGRATION_PHASE_RANK[previous_phase] + 1
        and previous_completed == current_completed
    )


def _validate_recoverable_journal_state(project: Path, journal: dict) -> None:
    """Prove a journal inventory agrees with the current source/target tree."""
    _validate_migration_tree(project, journal_exists=True)
    states = {
        mapping["source"]: _mapping_state(project, mapping)
        for mapping in journal["mappings"]
    }
    for source in journal["completed"]:
        if states[source] != "target":
            raise SafeFilesystemError(
                "clip migration journal completed state does not match inventory")
    for mapping in journal["mappings"]:
        source = mapping["source"]
        expected = (
            "missing-optional" if not mapping["present"] else
            "target" if source in journal["completed"] else "source")
        if states[source] != expected:
            raise SafeFilesystemError(
                "clip migration journal state does not match current inventory")
    if (_MIGRATION_PHASE_RANK[journal["phase"]]
            >= _MIGRATION_PHASE_RANK["targets_verified"]
            and any(state not in {"target", "missing-optional"}
                    for state in states.values())):
        raise SafeFilesystemError(
            "clip migration journal phase does not match inventory")


def _inspect_migration_journal_temps(
        project: Path, canonical: _MigrationJournalState | None,
) -> list[tuple[str, _MigrationJournalState]]:
    """Read-only validation of every exact atomic journal-temp name."""
    with open_directory(project.parent) as parent_fd:
        project_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            names = _migration_temp_names(project_fd)
            if canonical is None and len(names) > 1:
                raise SafeFilesystemError(
                    "multiple clip migration journal temps are ambiguous")
            states = []
            for name in names:
                state = _read_migration_journal_state_at(
                    project, project_fd, name,
                    label="clip migration journal temp")
                if canonical is not None and not (
                        state.content == canonical.content
                        and state.value == canonical.value
                        or _journal_is_immediate_predecessor(
                            state.value, canonical.value)):
                    raise SafeFilesystemError(
                        "clip migration journal temp is not a provable prior state")
                states.append((name, state))
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd, label="migration project")
            return states
        finally:
            if project_fd is not None:
                os.close(project_fd)


def _recover_migration_journal_temps(
        project: Path) -> _MigrationJournalState | None:
    """Recover only provable crash remnants while holding the project lock."""
    with open_directory(project.parent) as parent_fd:
        project_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")

            def verify_project_identity() -> None:
                verify_absolute_directory_identity(
                    project.parent, parent_fd, label="projects parent")
                _verify_migration_directory_identity(
                    parent_fd, project.name, project_fd,
                    label="migration project")

            names = _migration_temp_names(project_fd)
            canonical = None
            if not _migration_entry_is_absent(
                    project_fd, CLIP_MIGRATION_JOURNAL,
                    label="clip migration journal"):
                canonical = _read_migration_journal_state_at(
                    project, project_fd, CLIP_MIGRATION_JOURNAL,
                    label="clip migration journal")
            if not names:
                verify_project_identity()
                return canonical
            if canonical is None and len(names) != 1:
                raise SafeFilesystemError(
                    "multiple clip migration journal temps are ambiguous")

            candidates = []
            for name in names:
                candidate = _read_migration_journal_state_at(
                    project, project_fd, name,
                    label="clip migration journal temp")
                if canonical is not None and not (
                        candidate.content == canonical.content
                        and candidate.value == canonical.value
                        or _journal_is_immediate_predecessor(
                            candidate.value, canonical.value)):
                    raise SafeFilesystemError(
                        "clip migration journal temp is not a provable prior state")
                candidates.append((name, candidate))

            if canonical is None:
                name, candidate = candidates[0]
                _validate_recoverable_journal_state(project, candidate.value)
                verify_project_identity()
                retained = _read_migration_journal_state_at(
                    project, project_fd, name,
                    label="clip migration journal temp")
                if retained != candidate:
                    raise SafeFilesystemError(
                        "clip migration journal temp changed before promotion")
                atomic_move_no_replace_at(
                    project_fd, name, CLIP_MIGRATION_JOURNAL,
                    expected_source_identity=candidate.identity)
                canonical = _read_migration_journal_state_at(
                    project, project_fd, CLIP_MIGRATION_JOURNAL,
                    label="clip migration journal")
                if canonical != candidate:
                    raise SafeFilesystemError(
                        "promoted clip migration journal temp changed")
                verify_project_identity()
                return canonical

            for name, candidate in candidates:
                retained_canonical = _read_migration_journal_state_at(
                    project, project_fd, CLIP_MIGRATION_JOURNAL,
                    label="clip migration journal")
                if retained_canonical != canonical:
                    raise SafeFilesystemError(
                        "clip migration journal changed during temp recovery")
                retained_candidate = _read_migration_journal_state_at(
                    project, project_fd, name,
                    label="clip migration journal temp")
                if retained_candidate != candidate:
                    raise SafeFilesystemError(
                        "clip migration journal temp changed before cleanup")
                verify_project_identity()
                atomic_remove_regular_file_at(
                    project_fd, name, candidate.identity,
                    label="clip migration journal temp")
            if _migration_temp_names(project_fd):
                raise SafeFilesystemError(
                    "clip migration journal temp remains after recovery")
            retained_canonical = _read_migration_journal_state_at(
                project, project_fd, CLIP_MIGRATION_JOURNAL,
                label="clip migration journal")
            if retained_canonical != canonical:
                raise SafeFilesystemError(
                    "clip migration journal changed during temp recovery")
            verify_project_identity()
            return retained_canonical
        finally:
            if project_fd is not None:
                os.close(project_fd)


def _require_no_migration_temp_artifacts(project_fd: int) -> None:
    if _migration_temp_names(project_fd):
        raise SafeFilesystemError("clip migration journal temp remains")


def _require_no_migration_restore_artifacts(project_fd: int) -> None:
    if _migration_restore_names(project_fd):
        raise SafeFilesystemError(
            "clip migration restore artifact remains")


def _require_no_migration_restore_artifacts_for_project(project: Path) -> None:
    """Reject recognized restore artifacts through a retained project fd."""
    with open_directory(project.parent) as parent_fd:
        project_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            _require_no_migration_restore_artifacts(project_fd)
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd,
                label="migration project")
        finally:
            if project_fd is not None:
                os.close(project_fd)


def _cleanup_migration_restore_artifacts(project: Path) -> None:
    """Remove only old restore hardlinks to the retained canonical journal."""
    with open_directory(project.parent) as parent_fd:
        project_fd = journal_fd = None
        try:
            project_fd = _open_migration_directory(
                parent_fd, project.name, label="migration project")
            names = _migration_restore_names(project_fd)
            if not names:
                return

            try:
                journal_named = os.stat(
                    CLIP_MIGRATION_JOURNAL, dir_fd=project_fd,
                    follow_symlinks=False)
            except OSError as exc:
                raise SafeFilesystemError(
                    "restore artifacts exist without a safe migration journal") from exc
            if not stat.S_ISREG(journal_named.st_mode):
                raise SafeFilesystemError(
                    "restore artifacts exist without a safe migration journal")
            journal_fd, journal_opened = _open_migration_regular_file(
                project_fd, CLIP_MIGRATION_JOURNAL,
                label="clip migration journal")
            journal_identity = (journal_opened.st_dev, journal_opened.st_ino)
            if journal_identity != (journal_named.st_dev, journal_named.st_ino):
                raise SafeFilesystemError(
                    "clip migration journal changed while inspecting restore artifacts")
            journal_bytes = _read_migration_descriptor_bytes(journal_fd)
            journal_expected = journal_opened

            for name in names:
                artifact_fd = None
                try:
                    named = os.stat(
                        name, dir_fd=project_fd, follow_symlinks=False)
                    if (not stat.S_ISREG(named.st_mode)
                            or (named.st_dev, named.st_ino) != journal_identity):
                        raise SafeFilesystemError(
                            "unrecognized clip migration restore artifact")
                    artifact_fd, opened = _open_migration_regular_file(
                        project_fd, name,
                        label="clip migration restore artifact")
                    retained = os.fstat(artifact_fd)
                    if ((opened.st_dev, opened.st_ino) != journal_identity
                            or (retained.st_dev,
                                retained.st_ino) != journal_identity):
                        raise SafeFilesystemError(
                            "clip migration restore artifact changed")
                    if (_read_migration_descriptor_bytes(artifact_fd)
                            != journal_bytes):
                        raise SafeFilesystemError(
                            "clip migration restore artifact changed")
                    _verify_migration_journal_descriptor(
                        project_fd, journal_fd, journal_expected,
                        journal_bytes)
                    current = os.stat(
                        name, dir_fd=project_fd, follow_symlinks=False)
                    if (not stat.S_ISREG(current.st_mode)
                            or (current.st_dev, current.st_ino) != journal_identity):
                        raise SafeFilesystemError(
                            "clip migration restore artifact changed")
                    atomic_remove_regular_file_at(
                        project_fd, name, journal_identity,
                        label="clip migration restore artifact")
                    journal_expected = os.fstat(journal_fd)
                except SafeFilesystemError:
                    raise
                except OSError as exc:
                    raise SafeFilesystemError(
                        "could not remove clip migration restore artifact") from exc
                finally:
                    if artifact_fd is not None:
                        os.close(artifact_fd)

            journal_after = os.fstat(journal_fd)
            if (not stat.S_ISREG(journal_after.st_mode)
                    or (journal_after.st_dev,
                        journal_after.st_ino) != journal_identity):
                raise SafeFilesystemError(
                    "clip migration journal changed while removing restore artifacts")
            _verify_migration_journal_descriptor(
                project_fd, journal_fd, journal_after, journal_bytes)
            _require_no_migration_restore_artifacts(project_fd)
            verify_absolute_directory_identity(
                project.parent, parent_fd, label="projects parent")
            _verify_migration_directory_identity(
                parent_fd, project.name, project_fd,
                label="migration project")
        finally:
            for descriptor in (journal_fd, project_fd):
                if descriptor is not None:
                    os.close(descriptor)


def _remove_migration_restore_temp(
        project_fd: int, temporary: str,
        expected_identity: tuple[int, int]) -> None:
    """Remove only the exact private regular file created for restoration."""
    try:
        named = os.stat(temporary, dir_fd=project_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SafeFilesystemError(
            "could not inspect clip migration restore temp") from exc
    if (not stat.S_ISREG(named.st_mode)
            or (named.st_dev, named.st_ino) != expected_identity):
        raise SafeFilesystemError("clip migration restore temp changed")

    descriptor = None
    try:
        descriptor, opened = _open_migration_regular_file(
            project_fd, temporary, label="clip migration restore temp")
        retained = os.fstat(descriptor)
        if ((opened.st_dev, opened.st_ino) != expected_identity
                or (retained.st_dev, retained.st_ino) != expected_identity):
            raise SafeFilesystemError("clip migration restore temp changed")
        current = os.stat(
            temporary, dir_fd=project_fd, follow_symlinks=False)
        if (not stat.S_ISREG(current.st_mode)
                or (current.st_dev, current.st_ino) != expected_identity):
            raise SafeFilesystemError("clip migration restore temp changed")
        atomic_remove_regular_file_at(
            project_fd, temporary, expected_identity,
            label="clip migration restore temp")
    except (SafeFilesystemError, FileNotFoundError):
        raise
    except OSError as exc:
        raise SafeFilesystemError(
            "could not remove clip migration restore temp") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _restore_migration_journal(project_fd: int, content: bytes) -> None:
    """Atomically restore journal bytes without replacing a new journal."""
    temporary = f".{uuid.uuid4().hex}.clip-migration.restore"
    descriptor = None
    temporary_identity = None
    published = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=project_fd,
        )
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SafeFilesystemError("clip migration restore temp is unsafe")
        temporary_identity = (details.st_dev, details.st_ino)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        try:
            restored_identity = atomic_move_no_replace_at(
                project_fd, temporary, CLIP_MIGRATION_JOURNAL,
                expected_source_identity=temporary_identity)
        except FileExistsError as exc:
            raise SafeFilesystemError(
                "replacement clip migration journal was preserved") from exc
        if restored_identity != temporary_identity:
            raise SafeFilesystemError(
                "restored clip migration journal identity changed")
        published = True
    except SafeFilesystemError:
        raise
    except OSError as exc:
        raise SafeFilesystemError(
            "could not restore clip migration journal") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if not published and temporary_identity is not None:
            _remove_migration_restore_temp(
                project_fd, temporary, temporary_identity)


def _finalize_migration(
        project: Path, journal_state: _MigrationJournalState) -> None:
    """Validate and remove the journal through one retained descriptor chain."""
    journal = journal_state.value
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
            if ((journal_details.st_dev, journal_details.st_ino)
                    != journal_state.identity
                    or journal_bytes != journal_state.content
                    or retained_journal != journal):
                raise SafeFilesystemError(
                    "clip migration journal changed before finalization")

            _require_no_migration_temp_artifacts(project_fd)
            _require_no_migration_restore_artifacts(project_fd)
            _validate_migration_destination_descriptors(
                project_fd, clips_fd, clip_fd, journal,
                require_all_targets=True)
            _verify_migration_descriptor_chain(
                project, parent_fd, project_fd, clips_fd, clip_fd)
            _verify_migration_journal_descriptor(
                project_fd, journal_fd, journal_details, journal_bytes)

            removal_attempted = False
            try:
                removal_attempted = True
                atomic_remove_regular_file_at(
                    project_fd, CLIP_MIGRATION_JOURNAL,
                    (journal_details.st_dev, journal_details.st_ino),
                    label="clip migration journal")
                _require_migration_journal_absent(project_fd)
                _validate_migration_destination_descriptors(
                    project_fd, clips_fd, clip_fd, journal,
                    require_all_targets=True)
                _verify_migration_descriptor_chain(
                    project, parent_fd, project_fd, clips_fd, clip_fd)
                _require_no_migration_temp_artifacts(project_fd)
                _require_no_migration_restore_artifacts(project_fd)
                # This canonical no-follow absence check must remain last.
                _require_migration_journal_absent(project_fd)
            except BaseException:
                if removal_attempted:
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


def _apply_migration_project(project: Path, clip_store) -> dict:
    report, _journal = _assess_migration_project(project, clip_store)
    if report["status"] == "already-migrated":
        return report
    with clip_store.locked_project(project):
        active = _active_migration_jobs([project.name])[project.name]
        if active:
            raise ValueError(
                f"project {project.name} has active jobs: "
                + ", ".join(job["id"] for job in active))
        report, journal_state = _assess_migration_project(project, clip_store)
        if report["status"] == "already-migrated":
            return report
        if report["status"] == "resumable":
            journal_state = _recover_migration_journal_temps(project)
            if journal_state is None:
                raise SafeFilesystemError(
                    "resumable clip migration has no recoverable journal")
            _cleanup_migration_restore_artifacts(project)
        else:
            journal = _new_migration_journal(project)
            journal_state = _write_migration_journal(project, journal)
            _migration_checkpoint("journal-prepared")
        journal = journal_state.value

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
                updated = {
                    **journal,
                    "completed": [*journal["completed"], mapping["source"]],
                    "phase": "moving",
                }
                journal_state = _write_migration_journal(
                    project, updated, expected=journal_state)
                journal = journal_state.value
            if state != "missing-optional":
                _migration_checkpoint(f"moved:{mapping['source']}")
            _validate_migration_destination_tree(project, journal)

        for mapping in journal["mappings"]:
            state = _mapping_state(project, mapping)
            if state not in {"target", "missing-optional"}:
                raise ValueError("migration target verification failed")
        _validate_migration_destination_tree(
            project, journal, require_all_targets=True)
        updated = {**journal, "phase": "targets_verified"}
        journal_state = _write_migration_journal(
            project, updated, expected=journal_state)
        journal = journal_state.value
        _migration_checkpoint("targets-verified")

        manifest_path = project / "project.json"
        manifest_kind = _entry_kind(manifest_path)
        if manifest_kind == "missing":
            clip_store._validate_manifest(journal["manifest"])
            _validate_migration_destination_tree(
                project, journal, require_all_targets=True)
            _atomic_json_file(
                project, "project.json", journal["manifest"], replace=False)
        elif manifest_kind == "file":
            _validate_migration_destination_tree(
                project, journal, require_all_targets=True)
            if clip_store.describe(project) != journal["manifest"]:
                raise ValueError("published manifest does not match migration journal")
        else:
            raise ValueError("project manifest is unsafe")
        _migration_checkpoint("manifest-published")

        updated = {**journal, "phase": "manifest_published"}
        journal_state = _write_migration_journal(
            project, updated, expected=journal_state)
        journal = journal_state.value
        _migration_checkpoint("journal-manifest-published")
        if clip_store.describe(project) != journal["manifest"]:
            raise ValueError("published manifest verification failed")
        for mapping in journal["mappings"]:
            if _mapping_state(project, mapping) not in {
                    "target", "missing-optional"}:
                raise ValueError("migration target verification failed")
        updated = {**journal, "phase": "finalizing"}
        journal_state = _write_migration_journal(
            project, updated, expected=journal_state)
        journal = journal_state.value
        _migration_checkpoint("journal-finalizing")
        report = _report_from_journal(project, journal, "migrated")
        _finalize_migration(project, journal_state)
        report["phase"] = "complete"
        return report


def migrate_clips(root: Path, project: str | None = None, *,
                  apply: bool = False, clip_store) -> dict:
    """Plan or explicitly apply the resumable legacy Project→clip migration."""
    if project is None:
        project_ids = list_projects(root)
    else:
        project_ids = [project_path(root, project).name]
    projects = [project_path(root, project_id) for project_id in project_ids]
    assessed = []
    for selected in projects:
        report, _journal = _assess_migration_project(selected, clip_store)
        assessed.append(report)
    active = _active_migration_jobs(project_ids)
    for report in assessed:
        report["active_jobs"] = active[report["project"]]
    if apply:
        blocked = [project_id for project_id in project_ids if active[project_id]]
        if blocked:
            raise ValueError(
                "cannot migrate projects with active jobs: " + ", ".join(blocked))
        assessed = [
            _apply_migration_project(selected, clip_store)
            for selected in projects
        ]
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
