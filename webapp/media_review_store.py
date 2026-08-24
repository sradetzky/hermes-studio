from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from webapp.reference_store import REFERENCE_EXTENSIONS
from webapp.safe_files import (
    OpenedRegularFile,
    SafeFilesystemError,
    atomic_move_no_replace_at,
    atomic_remove_regular_file_at,
    atomic_write_bytes_at,
    copy_opened_file_at,
    open_directory,
    open_directory_at,
    open_regular_file_at,
    read_opened_text,
    verify_absolute_directory_identity,
)


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".flac", ".m4a"}
REVIEW_FILE = ".review.json"


class MediaReviewError(ValueError):
    pass


class MediaNotFoundError(MediaReviewError):
    pass


class UnsupportedMediaError(MediaReviewError):
    pass


@dataclass(frozen=True)
class MediaItem:
    name: str
    kind: str
    size: int
    url: str
    promoted: bool
    reference: bool

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class SavedMediaAction:
    action: str
    source: str
    target: str
    area: str
    size: int
    url: str
    created_at: str

    def to_dict(self) -> dict:
        return asdict(self)


class MediaReviewStore:
    @staticmethod
    def _component(value: str, label: str) -> str:
        if (not value or value.startswith(".") or "/" in value or "\\" in value
                or Path(value).name != value):
            raise MediaReviewError(f"invalid {label}: {value!r}")
        return value

    @staticmethod
    def media_kind(filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix not in REFERENCE_EXTENSIONS:
            raise UnsupportedMediaError(
                f"unsupported generation media type: {suffix or 'none'}")
        if suffix in IMAGE_EXTENSIONS:
            return "image"
        if suffix in VIDEO_EXTENSIONS:
            return "video"
        if suffix in AUDIO_EXTENSIONS:
            return "audio"
        raise UnsupportedMediaError(f"unsupported generation media type: {suffix}")

    @contextmanager
    def _open_generation(self, clip: Path,
                         generation_id: str) -> Iterator[tuple[Path, int]]:
        generation_id = self._component(generation_id, "generation id")
        generation = clip / "generations" / generation_id
        directory = open_directory(generation)
        try:
            generation_fd = directory.__enter__()
        except FileNotFoundError as exc:
            raise MediaNotFoundError(
                f"generation not found: {generation_id}") from exc
        except (SafeFilesystemError, OSError) as exc:
            raise MediaNotFoundError(
                f"generation is unsafe: {generation_id}") from exc
        try:
            yield generation, generation_fd
        finally:
            directory.__exit__(None, None, None)

    @contextmanager
    def open_media(self, clip: Path, generation_id: str,
                   filename: str) -> Iterator[OpenedRegularFile]:
        filename = self._component(filename, "generation filename")
        self.media_kind(filename)
        with self._open_generation(clip, generation_id) as (generation, generation_fd):
            try:
                with open_regular_file_at(
                        generation_fd, filename,
                        path=generation / filename) as opened:
                    yield opened
            except FileNotFoundError as exc:
                raise MediaNotFoundError(
                    f"generation media not found: {filename}") from exc
            except (SafeFilesystemError, OSError) as exc:
                raise MediaNotFoundError(
                    f"generation media is unsafe: {filename}") from exc

    @staticmethod
    def _read_json(generation_fd: int, name: str) -> dict:
        try:
            with open_regular_file_at(generation_fd, name) as opened:
                value = json.loads(read_opened_text(opened))
        except FileNotFoundError:
            return {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        except (SafeFilesystemError, OSError) as exc:
            raise MediaReviewError(f"unsafe generation metadata: {name}") from exc
        return value if isinstance(value, dict) else {}

    def _review(self, generation_fd: int) -> dict:
        value = self._read_json(generation_fd, REVIEW_FILE)
        actions = value.get("actions")
        if not isinstance(actions, list):
            actions = []
        return {"version": 1, "actions": [
            action for action in actions if isinstance(action, dict)
        ]}

    @staticmethod
    def _action_sources(review: dict, action_name: str) -> set[str]:
        return {
            str(action.get("source"))
            for action in review.get("actions", [])
            if action.get("action") == action_name and action.get("source")
        }

    def _describe_open_generation(
            self, project: Path, clip: Path, generation: Path,
            generation_fd: int, include_prompt: bool,
    ) -> dict:
        review = self._review(generation_fd)
        promoted = self._action_sources(review, "promote")
        references = self._action_sources(review, "reference")
        media = []
        for name in sorted(os.listdir(generation_fd)):
            if name.startswith("."):
                continue
            try:
                kind = self.media_kind(name)
            except UnsupportedMediaError:
                continue
            try:
                with open_regular_file_at(
                        generation_fd, name,
                        path=generation / name) as opened:
                    media.append(MediaItem(
                        name=name,
                        kind=kind,
                        size=opened.stat.st_size,
                        url=(
                            f"/media/projects/{quote(project.name)}/clips/"
                            f"{quote(clip.name)}/generations/"
                            f"{quote(generation.name)}/{quote(name)}"
                        ),
                        promoted=name in promoted,
                        reference=name in references,
                    ).to_dict())
            except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
                raise MediaReviewError(
                    f"generation media changed while listing: {name}") from exc
        meta = self._read_json(generation_fd, "meta.json")
        result = {
            "gen": generation.name,
            "files": [item["name"] for item in media],
            "media": media,
            "meta": meta,
            "review": {
                "promoted": sorted(promoted),
                "references": sorted(references),
            },
        }
        if include_prompt:
            try:
                with open_regular_file_at(
                        generation_fd, "prompt.txt") as opened:
                    result["prompt"] = read_opened_text(opened)
            except FileNotFoundError:
                result["prompt"] = ""
            except (SafeFilesystemError, OSError, UnicodeDecodeError) as exc:
                raise MediaReviewError("generation prompt is unsafe") from exc
            result["actions"] = review["actions"]
        return result

    def describe_generation(self, project: Path, clip: Path, generation_id: str,
                            include_prompt: bool = True) -> dict:
        with self._open_generation(clip, generation_id) as (generation, generation_fd):
            return self._describe_open_generation(
                project, clip, generation, generation_fd, include_prompt)

    def list_generations(self, project: Path, clip: Path) -> list[dict]:
        directory = clip / "generations"
        try:
            with open_directory(directory) as directory_fd:
                result = []
                for generation_id in sorted(os.listdir(directory_fd), reverse=True):
                    if generation_id.startswith("."):
                        continue
                    details = os.stat(
                        generation_id, dir_fd=directory_fd,
                        follow_symlinks=False)
                    if not stat.S_ISDIR(details.st_mode):
                        raise MediaReviewError(
                            f"unsafe generation entry: {generation_id}")
                    with open_directory_at(
                            directory_fd, generation_id) as generation_fd:
                        opened = os.fstat(generation_fd)
                        if (opened.st_dev, opened.st_ino) != (
                                details.st_dev, details.st_ino):
                            raise MediaReviewError(
                                f"generation changed while listing: {generation_id}")
                        result.append(self._describe_open_generation(
                            project, clip, directory / generation_id,
                            generation_fd, False))
                return result
        except FileNotFoundError as exc:
            raise MediaReviewError("generations directory is missing") from exc
        except (SafeFilesystemError, OSError) as exc:
            raise MediaReviewError("generations directory is unsafe") from exc

    @staticmethod
    def _area(action: str) -> str:
        areas = {"promote": "final", "reference": "references"}
        area = areas.get(action)
        if not area:
            raise MediaReviewError(f"unsupported media action: {action}")
        return area

    @staticmethod
    def _candidate(filename: str, index: int) -> str:
        if index == 1:
            return filename
        path = Path(filename)
        return f"{path.stem}_{index}{path.suffix}"

    @staticmethod
    def _publish(directory_fd: int, temp_name: str,
                 temp_identity: tuple[int, int],
                 requested_name: str) -> tuple[str, tuple[int, int]]:
        index = 1
        while True:
            target = MediaReviewStore._candidate(requested_name, index)
            try:
                identity = atomic_move_no_replace_at(
                    directory_fd, temp_name, target,
                    expected_source_identity=temp_identity)
                return target, identity
            except FileExistsError:
                index += 1

    @staticmethod
    def _write_review(generation_fd: int, review: dict) -> None:
        data = (json.dumps(
            review, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        atomic_write_bytes_at(
            generation_fd, REVIEW_FILE, data,
            label="generation review manifest")

    @staticmethod
    def _sha256(opened: OpenedRegularFile) -> str:
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(opened.descriptor, 1024 * 1024, offset)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            offset += len(chunk)

    @staticmethod
    def _saved_action(project: Path, area: str, action: str,
                      source_name: str, target_name: str, size: int,
                      created_at: str) -> SavedMediaAction:
        return SavedMediaAction(
            action=action,
            source=source_name,
            target=target_name,
            area=area,
            size=size,
            url=(
                f"/media/projects/{quote(project.name)}/{area}/"
                f"{quote(target_name)}"
            ),
            created_at=created_at,
        )

    def publish(self, project: Path, clip: Path, generation_id: str,
                filename: str, action: str) -> SavedMediaAction:
        filename = self._component(filename, "generation filename")
        self.media_kind(filename)
        area = self._area(action)
        try:
            with open_directory(project) as project_fd:
                try:
                    os.mkdir(area, mode=0o700, dir_fd=project_fd)
                except FileExistsError:
                    pass
                with open_directory_at(project_fd, area) as directory_fd:
                    lock_fd = os.open(
                        ".media-review.lock",
                        os.O_RDWR | os.O_CREAT | os.O_APPEND |
                        os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=project_fd,
                    )
                    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                        os.close(lock_fd)
                        raise MediaReviewError("media review lock is unsafe")
                    with os.fdopen(lock_fd, "a+b") as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                        try:
                            with self._open_generation(
                                    clip, generation_id) as (
                                        generation, generation_fd):
                                try:
                                    with open_regular_file_at(
                                            generation_fd, filename,
                                            path=generation / filename) as source:
                                        return self._publish_opened(
                                            project, clip, generation,
                                            generation_fd, source, area,
                                            project_fd, directory_fd, action)
                                except FileNotFoundError as exc:
                                    raise MediaNotFoundError(
                                        f"generation media not found: {filename}") from exc
                        finally:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (MediaReviewError, MediaNotFoundError, UnsupportedMediaError):
            raise
        except (SafeFilesystemError, OSError) as exc:
            raise MediaReviewError("media publication is unsafe") from exc

    def _publish_opened(
            self, project: Path, clip: Path, generation: Path,
            generation_fd: int, source: OpenedRegularFile, area: str,
            project_fd: int, directory_fd: int, action: str,
    ) -> SavedMediaAction:
        review = self._review(generation_fd)
        source_sha256 = self._sha256(source)
        for existing in review["actions"]:
            if (existing.get("action") == action
                    and existing.get("source") == source.name):
                target_name = self._component(
                    str(existing.get("target") or ""), "published media target")
                try:
                    with open_regular_file_at(
                            directory_fd, target_name) as target:
                        target_sha256 = self._sha256(target)
                        if (
                            existing.get("source_sha256") == source_sha256
                            and existing.get("target_sha256") == target_sha256
                            and source_sha256 == target_sha256
                        ):
                            return self._saved_action(
                                project, area, action, source.name, target_name,
                                target.stat.st_size,
                                str(existing.get("created_at") or ""))
                except FileNotFoundError:
                    pass

        requested_name = f"{clip.name}_{generation.name}_{source.name}"
        temp_name = f".{uuid.uuid4().hex}.review"
        temp_identity = copy_opened_file_at(source, directory_fd, temp_name)
        target_name = ""
        target_identity = None
        try:
            target_name, target_identity = self._publish(
                directory_fd, temp_name, temp_identity, requested_name)
            with open_regular_file_at(directory_fd, target_name) as target:
                target_sha256 = self._sha256(target)
                if (target.stat.st_size != source.stat.st_size
                        or target_sha256 != source_sha256):
                    raise MediaReviewError(
                        "published media does not match archived source")
                target_size = target.stat.st_size
            verify_absolute_directory_identity(
                project, project_fd, label="media review project")
            created_at = datetime.now(timezone.utc).isoformat()
            review["actions"].append({
                "action": action,
                "source": source.name,
                "target": target_name,
                "area": area,
                "source_sha256": source_sha256,
                "target_sha256": target_sha256,
                "created_at": created_at,
            })
            try:
                self._write_review(generation_fd, review)
            except Exception:
                atomic_remove_regular_file_at(
                    directory_fd, target_name, target_identity,
                    label="published media rollback")
                raise
            return self._saved_action(
                project, area, action, source.name, target_name,
                target_size, created_at)
        finally:
            if target_identity is None:
                try:
                    atomic_remove_regular_file_at(
                        directory_fd, temp_name, temp_identity,
                        label="media publication temporary file")
                except FileNotFoundError:
                    pass
