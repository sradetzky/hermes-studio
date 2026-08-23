from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path


PROJECT_MANIFEST = "project.json"
PROJECT_SCHEMA_VERSION = 1
CLIP_ID_RE = re.compile(r"clip-(\d{3,})")
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


class ClipStoreError(ValueError):
    pass


class ClipNotFoundError(ClipStoreError):
    pass


class ClipStore:
    @staticmethod
    def _project(project: Path) -> Path:
        if not project.is_dir() or project.is_symlink():
            raise ClipStoreError("project must be a regular directory")
        return project.resolve()

    @staticmethod
    def _component(value: object, label: str) -> str:
        if (not isinstance(value, str) or not value
                or value.startswith(".") or "/" in value or "\\" in value
                or Path(value).name != value):
            raise ClipStoreError(f"invalid {label}: {value!r}")
        return value

    @staticmethod
    def _title(value: object) -> str:
        if not isinstance(value, str):
            raise ClipStoreError("clip title must be text")
        title = value.strip()
        if not title or len(title) > 120:
            raise ClipStoreError("clip title must contain 1–120 characters")
        return title

    @classmethod
    def _clip_id(cls, value: object) -> str:
        value = cls._component(value, "clip id")
        if not CLIP_ID_RE.fullmatch(value):
            raise ClipStoreError(f"invalid clip id: {value!r}")
        return value

    @staticmethod
    def _clips_directory(project: Path, *, create: bool = False) -> Path:
        directory = project / "clips"
        if directory.is_symlink():
            raise ClipStoreError("clips directory may not be a symlink")
        if create:
            directory.mkdir(exist_ok=True)
        if not directory.is_dir() or directory.resolve().parent != project:
            raise ClipStoreError("clips directory is not a regular project directory")
        return directory

    @contextmanager
    def _lock(self, project: Path):
        lock_path = project / ".project.lock"
        if lock_path.is_symlink():
            raise ClipStoreError("project lock may not be a symlink")
        descriptor = os.open(
            lock_path,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
        )
        with os.fdopen(descriptor, "a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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
        project_title = value.get("title")
        if not isinstance(project_title, str) or not project_title.strip():
            raise ClipStoreError("project title is invalid")
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
            "title": project_title.strip(),
            "clips": normalized,
        }

    def _read_manifest_unlocked(self, project: Path) -> dict:
        path = self._manifest_path(project)
        if not path.is_file() or path.is_symlink():
            raise ClipStoreError("project manifest is missing or unsafe")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ClipStoreError("project manifest is invalid") from exc
        return self._validate_manifest(value)

    def _write_manifest_unlocked(self, project: Path, manifest: dict) -> None:
        manifest = self._validate_manifest(manifest)
        target = self._manifest_path(project)
        if target.is_symlink():
            raise ClipStoreError("project manifest may not be a symlink")
        temp = project / f".{uuid.uuid4().hex}.project-manifest"
        try:
            with temp.open("x", encoding="utf-8") as handle:
                json.dump(manifest, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    @staticmethod
    def _create_clip_tree(directory: Path) -> None:
        directory.mkdir()
        (directory / "current_prompt.txt").touch()
        (directory / "generations").mkdir()

    def initialize(self, project: Path, title: str) -> dict:
        project = self._project(project)
        title = self._title(title)
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
            try:
                staging.replace(target)
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
                shutil.rmtree(target, ignore_errors=True)
                raise
        return manifest

    def describe(self, project: Path) -> dict:
        project = self._project(project)
        manifest = self._read_manifest_unlocked(project)
        for entry in manifest["clips"]:
            self._resolve_clip_directory(project, entry["id"])
        return manifest

    def _resolve_clip_directory(self, project: Path, clip_id: str) -> Path:
        clips = self._clips_directory(project)
        clip = clips / clip_id
        if (not clip.is_dir() or clip.is_symlink()
                or clip.resolve().parent != clips.resolve()):
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
            try:
                staging.replace(target)
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
                shutil.rmtree(target, ignore_errors=True)
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
            if generations.is_symlink() or not generations.is_dir():
                raise ClipStoreError("generations directory is unsafe")
            generation = generations / generation_id
            if (not generation.is_dir() or generation.is_symlink()
                    or generation.resolve().parent != generations.resolve()):
                raise ClipStoreError(f"generation not found: {generation_id}")
            media = generation / filename
            if (not media.is_file() or media.is_symlink()
                    or media.resolve().parent != generation.resolve()):
                raise ClipStoreError(f"generation media not found: {filename}")
            selected = {"generation": generation_id, "filename": filename}
        elif filename is not None:
            raise ClipStoreError("filename requires a generation id")

        with self._lock(project):
            manifest = self._read_manifest_unlocked(project)
            entry = next((item for item in manifest["clips"]
                          if item["id"] == clip_id), None)
            if entry is None:
                raise ClipNotFoundError(f"clip not found: {clip_id}")
            entry["selected_take"] = selected
            self._write_manifest_unlocked(project, manifest)
        return entry
