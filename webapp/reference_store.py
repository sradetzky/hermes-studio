from __future__ import annotations

import fcntl
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote

from webapp.config import Settings


REFERENCE_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif",
    ".mp4", ".mov", ".webm", ".wav", ".mp3", ".flac", ".m4a",
}


class ReferenceStoreError(ValueError):
    pass


class UnsupportedReferenceError(ReferenceStoreError):
    pass


class ReferenceTooLargeError(ReferenceStoreError):
    pass


class Upload(Protocol):
    filename: str | None
    file: BinaryIO


@dataclass(frozen=True)
class SavedReference:
    name: str
    size: int
    url: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _StagedReference:
    requested_name: str
    suffix: str
    temp: Path
    size: int


class ReferenceStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _validate_filename(filename: str) -> str:
        if (not filename or "/" in filename or "\\" in filename or
                Path(filename).name != filename):
            raise ReferenceStoreError(
                f"invalid upload filename: {filename!r}")
        suffix = Path(filename).suffix.lower()
        if suffix not in REFERENCE_EXTENSIONS:
            raise UnsupportedReferenceError(
                f"unsupported reference type: {suffix or 'none'}")
        return suffix

    def _stage(self, directory: Path, upload: Upload) -> _StagedReference:
        filename = upload.filename or ""
        suffix = self._validate_filename(filename)
        temp = directory / f".{uuid.uuid4().hex}.upload"
        size = 0
        upload.file.seek(0)
        try:
            with temp.open("xb") as handle:
                while chunk := upload.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.settings.max_reference_bytes:
                        raise ReferenceTooLargeError(
                            f"{filename} exceeds "
                            f"{self.settings.max_reference_bytes // (1024 * 1024)}MB limit",
                        )
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            if size == 0:
                raise ReferenceStoreError(f"empty upload: {filename}")
            return _StagedReference(filename, suffix, temp, size)
        except Exception:
            temp.unlink(missing_ok=True)
            raise
        finally:
            upload.file.close()

    @staticmethod
    def _candidate(directory: Path, filename: str, suffix: str, index: int) -> Path:
        if index == 1:
            return directory / filename
        return directory / f"{Path(filename).stem}_{index}{suffix}"

    @staticmethod
    def _publish(directory: Path, item: _StagedReference) -> Path:
        index = 1
        while True:
            target = ReferenceStore._candidate(
                directory, item.requested_name, item.suffix, index)
            try:
                os.link(item.temp, target)
                return target
            except FileExistsError:
                index += 1

    def save_batch(self, project: Path,
                   uploads: Sequence[Upload]) -> list[SavedReference]:
        if not uploads:
            raise ReferenceStoreError("no files supplied")
        if len(uploads) > self.settings.max_upload_files:
            raise ReferenceStoreError(
                f"maximum {self.settings.max_upload_files} files per upload")

        directory = project / "references"
        if directory.is_symlink():
            raise ReferenceStoreError(
                "references directory may not be a symlink")
        directory.mkdir(exist_ok=True)
        if directory.resolve().parent != project.resolve():
            raise ReferenceStoreError("references directory escapes project")
        staged: list[_StagedReference] = []
        published: list[Path] = []
        try:
            for upload in uploads:
                staged.append(self._stage(directory, upload))
            lock_path = directory / ".upload.lock"
            with lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    for item in staged:
                        published.append(self._publish(directory, item))
                except Exception:
                    for target in published:
                        target.unlink(missing_ok=True)
                    raise
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

            saved = [
                SavedReference(
                    name=target.name,
                    size=item.size,
                    url=(
                        f"/media/projects/{quote(project.name)}/references/"
                        f"{quote(target.name)}"
                    ),
                )
                for item, target in zip(staged, published, strict=True)
            ]
            return saved
        finally:
            for item in staged:
                item.temp.unlink(missing_ok=True)
            for upload in uploads:
                try:
                    upload.file.close()
                except OSError:
                    pass
