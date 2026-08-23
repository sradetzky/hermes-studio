from __future__ import annotations

import fcntl
import json
import os
import shutil
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from webapp.reference_store import REFERENCE_EXTENSIONS


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

    def resolve_generation(self, project: Path, generation_id: str) -> Path:
        generation_id = self._component(generation_id, "generation id")
        directory = project / "generations"
        if directory.is_symlink():
            raise MediaNotFoundError("generations directory may not be a symlink")
        base = directory.resolve()
        if base.parent != project.resolve():
            raise MediaNotFoundError("generations directory escapes project")
        generation = directory / generation_id
        if (not generation.is_dir() or generation.is_symlink()
                or generation.resolve().parent != base):
            raise MediaNotFoundError(f"generation not found: {generation_id}")
        return generation

    def resolve_media(self, project: Path, generation_id: str,
                      filename: str) -> tuple[Path, Path]:
        filename = self._component(filename, "generation filename")
        self.media_kind(filename)
        generation = self.resolve_generation(project, generation_id)
        source = generation / filename
        if (not source.is_file() or source.is_symlink()
                or source.resolve().parent != generation.resolve()):
            raise MediaNotFoundError(f"generation media not found: {filename}")
        return generation, source

    @staticmethod
    def _read_json(path: Path) -> dict:
        if not path.is_file() or path.is_symlink():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def _review(self, generation: Path) -> dict:
        value = self._read_json(generation / REVIEW_FILE)
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

    def describe_generation(self, project: Path, generation_id: str,
                            include_prompt: bool = True) -> dict:
        generation = self.resolve_generation(project, generation_id)
        review = self._review(generation)
        promoted = self._action_sources(review, "promote")
        references = self._action_sources(review, "reference")
        media = []
        for item in sorted(generation.iterdir()):
            if not item.is_file() or item.is_symlink() or item.name.startswith("."):
                continue
            try:
                kind = self.media_kind(item.name)
            except UnsupportedMediaError:
                continue
            media.append(MediaItem(
                name=item.name,
                kind=kind,
                size=item.stat().st_size,
                url=(
                    f"/media/projects/{quote(project.name)}/generations/"
                    f"{quote(generation.name)}/{quote(item.name)}"
                ),
                promoted=item.name in promoted,
                reference=item.name in references,
            ).to_dict())
        meta = self._read_json(generation / "meta.json")
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
            prompt = generation / "prompt.txt"
            result["prompt"] = (
                prompt.read_text(encoding="utf-8") if prompt.is_file()
                and not prompt.is_symlink() else ""
            )
            result["actions"] = review["actions"]
        return result

    @staticmethod
    def _target_directory(project: Path, action: str) -> tuple[str, Path]:
        areas = {"promote": "final", "reference": "references"}
        area = areas.get(action)
        if not area:
            raise MediaReviewError(f"unsupported media action: {action}")
        directory = project / area
        if directory.is_symlink():
            raise MediaReviewError(f"{area} directory may not be a symlink")
        directory.mkdir(exist_ok=True)
        if directory.resolve().parent != project.resolve():
            raise MediaReviewError(f"{area} directory escapes project")
        return area, directory

    @staticmethod
    def _candidate(directory: Path, filename: str, index: int) -> Path:
        if index == 1:
            return directory / filename
        path = Path(filename)
        return directory / f"{path.stem}_{index}{path.suffix}"

    @staticmethod
    def _publish(temp: Path, directory: Path, requested_name: str) -> Path:
        index = 1
        while True:
            target = MediaReviewStore._candidate(
                directory, requested_name, index)
            try:
                os.link(temp, target)
                return target
            except FileExistsError:
                index += 1

    @staticmethod
    def _write_review(generation: Path, review: dict) -> None:
        target = generation / REVIEW_FILE
        temp = generation / f".{uuid.uuid4().hex}.review"
        try:
            with temp.open("x", encoding="utf-8") as handle:
                json.dump(review, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temp.replace(target)
        finally:
            temp.unlink(missing_ok=True)

    def publish(self, project: Path, generation_id: str, filename: str,
                action: str) -> SavedMediaAction:
        generation, source = self.resolve_media(
            project, generation_id, filename)
        area, directory = self._target_directory(project, action)
        lock_path = project / ".media-review.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                review = self._review(generation)
                for existing in review["actions"]:
                    if (existing.get("action") == action
                            and existing.get("source") == source.name):
                        target_name = str(existing.get("target") or "")
                        target = directory / target_name
                        if (target_name and target.is_file()
                                and not target.is_symlink()
                                and target.resolve().parent == directory.resolve()):
                            return SavedMediaAction(
                                action=action,
                                source=source.name,
                                target=target.name,
                                area=area,
                                size=target.stat().st_size,
                                url=(
                                    f"/media/projects/{quote(project.name)}/"
                                    f"{area}/{quote(target.name)}"
                                ),
                                created_at=str(existing.get("created_at") or ""),
                            )

                requested_name = f"{generation.name}_{source.name}"
                temp = directory / f".{uuid.uuid4().hex}.review"
                try:
                    with source.open("rb") as input_handle, temp.open("xb") as output:
                        shutil.copyfileobj(input_handle, output, 1024 * 1024)
                        output.flush()
                        os.fsync(output.fileno())
                    target = self._publish(temp, directory, requested_name)
                finally:
                    temp.unlink(missing_ok=True)

                created_at = datetime.now(timezone.utc).isoformat()
                entry = {
                    "action": action,
                    "source": source.name,
                    "target": target.name,
                    "area": area,
                    "created_at": created_at,
                }
                review["actions"].append(entry)
                try:
                    self._write_review(generation, review)
                except Exception:
                    target.unlink(missing_ok=True)
                    raise
                return SavedMediaAction(
                    action=action,
                    source=source.name,
                    target=target.name,
                    area=area,
                    size=target.stat().st_size,
                    url=(
                        f"/media/projects/{quote(project.name)}/{area}/"
                        f"{quote(target.name)}"
                    ),
                    created_at=created_at,
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
