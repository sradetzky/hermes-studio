from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from webapp.identifiers import CLIP_ID_RE, validate_clip_id
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_publish_directory,
    atomic_write_bytes_at,
    open_directory,
    open_directory_at,
    open_regular_file,
    open_regular_file_at,
    read_opened_bytes,
    read_opened_text,
    remove_published_directory_if_same,
)


PROJECT_MANIFEST = "project.json"
PROJECT_BRIEF = "brief.md"
PROJECT_SCHEMA_VERSION = 1
PROJECT_BRIEF_MAX_CHARS = 100_000
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


class ClipStoreError(ValueError):
    pass


class ClipNotFoundError(ClipStoreError):
    pass


class TakeNotFoundError(ClipStoreError):
    pass


class ClipStore:
    @staticmethod
    def _project(project: Path) -> Path:
        try:
            with open_directory(project):
                pass
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise ClipStoreError("project must be a regular directory")
        return Path(os.path.abspath(project))

    @staticmethod
    def _component(value: object, label: str) -> str:
        if (not isinstance(value, str) or not value
                or value.startswith(".") or "/" in value or "\\" in value
                or Path(value).name != value):
            raise ClipStoreError(f"invalid {label}: {value!r}")
        return value

    @staticmethod
    def _title(value: object, label: str = "clip title") -> str:
        if not isinstance(value, str):
            raise ClipStoreError(f"{label} must be text")
        title = value.strip()
        if not title or len(title) > 120:
            raise ClipStoreError(f"{label} must contain 1–120 characters")
        return title

    @staticmethod
    def _brief(value: object) -> str:
        if not isinstance(value, str):
            raise ClipStoreError("project brief must be text")
        if len(value) > PROJECT_BRIEF_MAX_CHARS:
            raise ClipStoreError(
                f"project brief must not exceed {PROJECT_BRIEF_MAX_CHARS} characters")
        return value

    @classmethod
    def _clip_id(cls, value: object) -> str:
        try:
            return validate_clip_id(value)
        except ValueError as exc:
            raise ClipStoreError(str(exc)) from exc

    @staticmethod
    def _clips_directory(project: Path, *, create: bool = False) -> Path:
        directory = project / "clips"
        try:
            with open_directory(project) as project_fd:
                if create:
                    try:
                        os.mkdir("clips", mode=0o700, dir_fd=project_fd)
                    except FileExistsError:
                        pass
                with open_directory_at(project_fd, "clips"):
                    pass
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise ClipStoreError(
                "clips directory is not a regular project directory") from exc
        return directory

    @contextmanager
    def _lock(self, project: Path):
        directory = open_directory(project)
        entered = False
        try:
            project_fd = directory.__enter__()
            entered = True
            descriptor = os.open(
                ".project.lock",
                os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                0o600,
                dir_fd=project_fd,
            )
        except (SafeFilesystemError, OSError) as exc:
            if entered:
                directory.__exit__(None, None, None)
            raise ClipStoreError("project lock is unsafe") from exc
        try:
            with os.fdopen(descriptor, "a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            directory.__exit__(None, None, None)

    @contextmanager
    def locked_project(self, project: Path):
        """Hold the canonical project lock for a multi-step operation."""
        project = self._project(project)
        with self._lock(project):
            yield project

    @staticmethod
    def _manifest_path(project: Path) -> Path:
        return project / PROJECT_MANIFEST

    def _validate_manifest(self, value: object) -> dict:
        if not isinstance(value, dict):
            raise ClipStoreError("project manifest must be a JSON object")
        if set(value) != {"schema_version", "title", "clips"}:
            raise ClipStoreError("project manifest has unsupported fields")
        if value.get("schema_version") != PROJECT_SCHEMA_VERSION:
            raise ClipStoreError("project manifest schema is unsupported")
        project_title = self._title(value.get("title"), "project title")
        clips = value.get("clips")
        if not isinstance(clips, list) or not clips:
            raise ClipStoreError("project must contain at least one clip")

        normalized = []
        seen = set()
        for entry in clips:
            if not isinstance(entry, dict) or set(entry) != {
                    "id", "title", "enabled", "selected_take"}:
                raise ClipStoreError("project clip entry is invalid")
            clip_id = self._clip_id(entry.get("id"))
            if clip_id in seen:
                raise ClipStoreError(f"duplicate clip id: {clip_id}")
            seen.add(clip_id)
            enabled = entry.get("enabled")
            if not isinstance(enabled, bool):
                raise ClipStoreError("clip enabled state must be true or false")
            selected = entry.get("selected_take")
            if selected is not None:
                if not isinstance(selected, dict) or set(selected) != {
                        "generation", "filename"}:
                    raise ClipStoreError("selected take is invalid")
                selected = {
                    "generation": self._component(
                        selected.get("generation"), "generation id"),
                    "filename": self._component(
                        selected.get("filename"), "generation filename"),
                }
            normalized.append({
                "id": clip_id,
                "title": self._title(entry.get("title")),
                "enabled": enabled,
                "selected_take": selected,
            })
        return {
            "schema_version": PROJECT_SCHEMA_VERSION,
            "title": project_title,
            "clips": normalized,
        }

    def _read_manifest_unlocked(self, project: Path) -> dict:
        path = self._manifest_path(project)
        try:
            with open_regular_file(path) as opened:
                value = json.loads(read_opened_text(opened))
        except (FileNotFoundError, SafeFilesystemError) as exc:
            raise ClipStoreError("project manifest is missing or unsafe") from exc
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            raise ClipStoreError("project manifest is invalid") from exc
        return self._validate_manifest(value)

    def _write_manifest_unlocked(self, project: Path, manifest: dict) -> None:
        manifest = self._validate_manifest(manifest)
        data = (json.dumps(
            manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with open_directory(project) as project_fd:
                atomic_write_bytes_at(
                    project_fd, PROJECT_MANIFEST, data,
                    label="project manifest")
        except (SafeFilesystemError, OSError) as exc:
            raise ClipStoreError("project manifest publication is unsafe") from exc

    @staticmethod
    def _read_brief_unlocked(project: Path) -> str:
        try:
            with open_regular_file(project / PROJECT_BRIEF) as opened:
                return read_opened_text(opened)
        except (FileNotFoundError, SafeFilesystemError, OSError,
                UnicodeDecodeError):
            return ""

    @staticmethod
    def _read_brief_bytes_unlocked(project: Path) -> bytes:
        try:
            with open_regular_file(project / PROJECT_BRIEF) as opened:
                return read_opened_bytes(opened)
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise ClipStoreError("project brief is missing or unsafe") from exc

    @staticmethod
    def _write_brief_unlocked(project: Path, data: bytes) -> None:
        try:
            with open_directory(project) as project_fd:
                atomic_write_bytes_at(
                    project_fd, PROJECT_BRIEF, data, label="project brief")
        except (SafeFilesystemError, OSError) as exc:
            raise ClipStoreError("project brief publication is unsafe") from exc

    @staticmethod
    def _create_clip_tree(directory: Path) -> None:
        directory.mkdir()
        (directory / "current_prompt.txt").touch()
        (directory / "chat.jsonl").touch()
        (directory / "generations").mkdir()

    def initialize(self, project: Path, title: str) -> dict:
        project = self._project(project)
        title = self._title(title, "project title")
        with self._lock(project):
            target_manifest = self._manifest_path(project)
            if os.path.lexists(target_manifest):
                raise ClipStoreError("project manifest already exists")
            clips = self._clips_directory(project, create=True)
            target = clips / "clip-001"
            if os.path.lexists(target):
                raise ClipStoreError("default clip already exists")
            staging = clips / f".creating-{uuid.uuid4().hex}"
            self._create_clip_tree(staging)
            published_identity = None
            try:
                try:
                    published_identity = atomic_publish_directory(staging, target)
                except FileExistsError as exc:
                    raise ClipStoreError("default clip already exists") from exc
                manifest = {
                    "schema_version": PROJECT_SCHEMA_VERSION,
                    "title": title,
                    "clips": [{
                        "id": "clip-001",
                        "title": "Main clip",
                        "enabled": True,
                        "selected_take": None,
                    }],
                }
                self._write_manifest_unlocked(project, manifest)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                if published_identity is not None:
                    remove_published_directory_if_same(
                        target, published_identity)
                raise
        return manifest

    def _describe_unlocked(self, project: Path) -> dict:
        manifest = self._read_manifest_unlocked(project)
        for entry in manifest["clips"]:
            self._resolve_clip_directory(project, entry["id"])
        return manifest

    def describe(self, project: Path) -> dict:
        project = self._project(project)
        return self._describe_unlocked(project)

    def describe_project(self, project: Path) -> dict:
        project = self._project(project)
        with self._lock(project):
            manifest = self._describe_unlocked(project)
            brief = self._read_brief_unlocked(project)
        return {
            "title": manifest["title"],
            "brief": brief,
            "clips": manifest["clips"],
        }

    def update_project_metadata(
            self, project: Path, *, title: str, brief: str) -> dict:
        project = self._project(project)
        title = self._title(title, "project title")
        brief = self._brief(brief)
        brief_data = brief.encode("utf-8")
        with self._lock(project):
            manifest = self._describe_unlocked(project)
            original_brief = self._read_brief_bytes_unlocked(project)
            title_changed = manifest["title"] != title
            brief_changed = original_brief != brief_data
            if brief_changed:
                self._write_brief_unlocked(project, brief_data)
            try:
                if title_changed:
                    manifest["title"] = title
                    self._write_manifest_unlocked(project, manifest)
            except Exception:
                if brief_changed:
                    try:
                        self._write_brief_unlocked(project, original_brief)
                    except Exception as rollback_exc:
                        raise ClipStoreError(
                            "project metadata failed and brief rollback failed"
                        ) from rollback_exc
                raise
        return {
            "title": manifest["title"],
            "brief": brief,
            "clips": manifest["clips"],
        }

    def _resolve_clip_directory(self, project: Path, clip_id: str) -> Path:
        clips = self._clips_directory(project)
        clip = clips / clip_id
        try:
            with open_directory(clips) as clips_fd:
                with open_directory_at(clips_fd, clip_id):
                    pass
        except (FileNotFoundError, SafeFilesystemError, OSError):
            raise ClipNotFoundError(f"clip not found: {clip_id}")
        return clip

    def resolve_clip(self, project: Path, clip_id: str) -> Path:
        project = self._project(project)
        clip_id = self._clip_id(clip_id)
        manifest = self._read_manifest_unlocked(project)
        if not any(entry["id"] == clip_id for entry in manifest["clips"]):
            raise ClipNotFoundError(f"clip not found: {clip_id}")
        return self._resolve_clip_directory(project, clip_id)

    def create_clip(self, project: Path, title: str) -> dict:
        project = self._project(project)
        title = self._title(title)
        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            numbers = []
            for entry in manifest["clips"]:
                match = CLIP_ID_RE.fullmatch(entry["id"])
                assert match is not None
                numbers.append(int(match.group(1)))
            clip_id = f"clip-{max(numbers, default=0) + 1:03d}"
            clips = self._clips_directory(project)
            target = clips / clip_id
            staging = clips / f".creating-{uuid.uuid4().hex}"
            self._create_clip_tree(staging)
            published_identity = None
            try:
                try:
                    published_identity = atomic_publish_directory(staging, target)
                except FileExistsError as exc:
                    raise ClipStoreError(
                        f"clip target already exists: {clip_id}") from exc
                entry = {
                    "id": clip_id,
                    "title": title,
                    "enabled": True,
                    "selected_take": None,
                }
                manifest["clips"].append(entry)
                self._write_manifest_unlocked(project, manifest)
            except Exception:
                shutil.rmtree(staging, ignore_errors=True)
                if published_identity is not None:
                    remove_published_directory_if_same(
                        target, published_identity)
                raise
        return entry

    def update_clip(self, project: Path, clip_id: str, *,
                    title: str | None = None,
                    enabled: bool | None = None) -> dict:
        project = self._project(project)
        clip_id = self._clip_id(clip_id)
        if title is None and enabled is None:
            raise ClipStoreError("clip update is empty")
        if title is not None:
            title = self._title(title)
        if enabled is not None and not isinstance(enabled, bool):
            raise ClipStoreError("clip enabled state must be true or false")
        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            entry = next((item for item in manifest["clips"]
                          if item["id"] == clip_id), None)
            if entry is None:
                raise ClipNotFoundError(f"clip not found: {clip_id}")
            self.resolve_clip(project, clip_id)
            if title is not None:
                entry["title"] = title
            if enabled is not None:
                entry["enabled"] = enabled
            self._write_manifest_unlocked(project, manifest)
        return entry

    def reorder(self, project: Path, clip_ids: list[str]) -> dict:
        project = self._project(project)
        if not isinstance(clip_ids, list):
            raise ClipStoreError("clip order must be a list")
        normalized = [self._clip_id(value) for value in clip_ids]
        if len(normalized) != len(set(normalized)):
            raise ClipStoreError("clip order contains duplicates")
        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            existing = {entry["id"]: entry for entry in manifest["clips"]}
            if set(normalized) != set(existing):
                raise ClipStoreError("clip order must contain every clip exactly once")
            manifest["clips"] = [existing[clip_id] for clip_id in normalized]
            self._write_manifest_unlocked(project, manifest)
        return manifest

    def select_take(self, project: Path, clip_id: str,
                    generation_id: str | None,
                    filename: str | None = None) -> dict:
        project = self._project(project)
        clip_id = self._clip_id(clip_id)
        selected = None
        if generation_id is not None:
            generation_id = self._component(generation_id, "generation id")
            filename = self._component(filename, "generation filename")
            if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
                raise ClipStoreError("selected take must be a video")
            clip = self.resolve_clip(project, clip_id)
            generations = clip / "generations"
            try:
                with open_directory(generations) as generations_fd:
                    with open_directory_at(
                            generations_fd, generation_id) as generation_fd:
                        with open_regular_file_at(generation_fd, filename):
                            pass
            except FileNotFoundError as exc:
                raise ClipStoreError(
                    f"generation media not found: {filename}") from exc
            except (SafeFilesystemError, OSError) as exc:
                raise ClipStoreError("generations directory is unsafe")
            selected = {"generation": generation_id, "filename": filename}
        elif filename is not None:
            raise ClipStoreError("filename requires a generation id")

        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            entry = next((item for item in manifest["clips"]
                          if item["id"] == clip_id), None)
            if entry is None:
                raise ClipNotFoundError(f"clip not found: {clip_id}")
            if selected is not None and not entry["enabled"]:
                raise ClipStoreError("cannot select a take for a disabled clip")
            entry["selected_take"] = selected
            self._write_manifest_unlocked(project, manifest)
        return entry

    def delete_take(self, project: Path, clip_id: str,
                    generation_id: str) -> dict:
        project = self._project(project)
        clip_id = self._clip_id(clip_id)
        generation_id = self._component(generation_id, "generation id")
        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            entry = next((item for item in manifest["clips"]
                          if item["id"] == clip_id), None)
            if entry is None:
                raise ClipNotFoundError(f"clip not found: {clip_id}")
            clip = self._resolve_clip_directory(project, clip_id)
            generations = clip / "generations"
            generation = generations / generation_id
            try:
                with open_directory(generations) as generations_fd:
                    with open_directory_at(
                            generations_fd, generation_id) as generation_fd:
                        details = os.fstat(generation_fd)
                        identity = (details.st_dev, details.st_ino)
            except FileNotFoundError as exc:
                raise TakeNotFoundError(
                    f"generation not found: {generation_id}") from exc
            except (SafeFilesystemError, OSError) as exc:
                raise ClipStoreError(
                    f"generation is unsafe: {generation_id}") from exc

            previous_selection = entry["selected_take"]
            cleared_selection = bool(
                previous_selection
                and previous_selection["generation"] == generation_id)
            if cleared_selection:
                entry["selected_take"] = None
                self._write_manifest_unlocked(project, manifest)
            try:
                if not remove_published_directory_if_same(generation, identity):
                    raise ClipStoreError(
                        f"generation could not be safely deleted: {generation_id}")
            except Exception as exc:
                if cleared_selection:
                    entry["selected_take"] = previous_selection
                    try:
                        self._write_manifest_unlocked(project, manifest)
                    except Exception as restore_exc:
                        raise ClipStoreError(
                            "take deletion failed and selected take could not "
                            "be restored") from restore_exc
                if isinstance(exc, ClipStoreError):
                    raise
                raise ClipStoreError(
                    f"generation deletion failed: {generation_id}") from exc
        return entry
