from __future__ import annotations

import fcntl
import os
import stat
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, Protocol
from urllib.parse import quote

from webapp.config import Settings
from studio_core.safe_files import (
    OpenedRegularFile,
    SafeFilesystemError,
    atomic_move_no_replace_at,
    atomic_remove_regular_file_at,
    open_directory,
    open_directory_at,
    open_regular_beneath,
    verify_absolute_directory_identity,
)


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
    temp_name: str
    identity: tuple[int, int]
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

    def _stage(self, directory_fd: int, upload: Upload) -> _StagedReference:
        filename = upload.filename or ""
        suffix = self._validate_filename(filename)
        temp_name = f".{uuid.uuid4().hex}.upload"
        size = 0
        upload.file.seek(0)
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=directory_fd,
        )
        details = os.fstat(descriptor)
        identity = (details.st_dev, details.st_ino)
        try:
            while chunk := upload.file.read(1024 * 1024):
                size += len(chunk)
                if size > self.settings.max_reference_bytes:
                    raise ReferenceTooLargeError(
                        f"{filename} exceeds "
                        f"{self.settings.max_reference_bytes // (1024 * 1024)}MB limit",
                    )
                view = memoryview(chunk)
                while view:
                    written = os.write(descriptor, view)
                    view = view[written:]
            os.fsync(descriptor)
            if size == 0:
                raise ReferenceStoreError(f"empty upload: {filename}")
            return _StagedReference(
                filename, suffix, temp_name, identity, size)
        except Exception:
            os.close(descriptor)
            descriptor = -1
            atomic_remove_regular_file_at(
                directory_fd, temp_name, identity,
                label="staged reference upload")
            raise
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            upload.file.close()

    @staticmethod
    def _candidate(filename: str, suffix: str, index: int) -> str:
        if index == 1:
            return filename
        return f"{Path(filename).stem}_{index}{suffix}"

    @staticmethod
    def _publish(directory_fd: int, item: _StagedReference) -> tuple[str, tuple[int, int]]:
        index = 1
        while True:
            target = ReferenceStore._candidate(
                item.requested_name, item.suffix, index)
            try:
                identity = atomic_move_no_replace_at(
                    directory_fd, item.temp_name, target,
                    expected_source_identity=item.identity)
                return target, identity
            except FileExistsError:
                index += 1

    @staticmethod
    def list_references(project: Path) -> list[str]:
        try:
            with open_directory(project / "references") as directory_fd:
                names = []
                for name in os.listdir(directory_fd):
                    if name.startswith(".") or Path(name).suffix.lower() not in REFERENCE_EXTENSIONS:
                        continue
                    details = os.stat(
                        name, dir_fd=directory_fd, follow_symlinks=False)
                    if not stat.S_ISREG(details.st_mode):
                        raise ReferenceStoreError(
                            f"unsafe reference entry: {name}")
                    names.append(name)
                return sorted(names)
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise ReferenceStoreError("references directory is unsafe") from exc

    @staticmethod
    @contextmanager
    def open_media(project: Path, area: str,
                   relative_path: str) -> Iterator[OpenedRegularFile]:
        if area not in {"references", "final"}:
            raise ReferenceStoreError(f"unsupported media area: {area}")
        relative = Path(relative_path)
        if (not relative_path or relative.is_absolute()
                or any(part.startswith(".") for part in relative.parts)):
            raise ReferenceStoreError("invalid media path")
        try:
            with open_regular_beneath(project / area, relative) as opened:
                yield opened
        except FileNotFoundError:
            raise
        except (SafeFilesystemError, OSError) as exc:
            raise ReferenceStoreError("media path is unsafe") from exc

    def save_batch(self, project: Path,
                   uploads: Sequence[Upload]) -> list[SavedReference]:
        if not uploads:
            raise ReferenceStoreError("no files supplied")
        if len(uploads) > self.settings.max_upload_files:
            raise ReferenceStoreError(
                f"maximum {self.settings.max_upload_files} files per upload")

        staged: list[_StagedReference] = []
        published: list[tuple[str, tuple[int, int]]] = []
        try:
            with open_directory(project) as project_fd:
                try:
                    os.mkdir("references", mode=0o700, dir_fd=project_fd)
                except FileExistsError:
                    pass
                with open_directory_at(project_fd, "references") as directory_fd:
                    for upload in uploads:
                        staged.append(self._stage(directory_fd, upload))
                    lock_fd = os.open(
                        ".upload.lock",
                        os.O_RDWR | os.O_CREAT | os.O_APPEND |
                        os.O_NOFOLLOW | os.O_CLOEXEC,
                        0o600,
                        dir_fd=directory_fd,
                    )
                    if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                        os.close(lock_fd)
                        raise ReferenceStoreError("upload lock is unsafe")
                    with os.fdopen(lock_fd, "a+b") as lock:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                        try:
                            for item in staged:
                                published.append(self._publish(directory_fd, item))
                        except Exception:
                            for target, identity in published:
                                atomic_remove_regular_file_at(
                                    directory_fd, target, identity,
                                    label="published reference rollback")
                            raise
                        finally:
                            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                    verify_absolute_directory_identity(
                        project, project_fd, label="reference project")

                    saved = [
                        SavedReference(
                            name=target,
                            size=item.size,
                            url=(
                                f"/media/projects/{quote(project.name)}/references/"
                                f"{quote(target)}"
                            ),
                        )
                        for item, (target, _) in zip(
                            staged, published, strict=True)
                    ]
                    return saved
        except (SafeFilesystemError, OSError) as exc:
            raise ReferenceStoreError("reference publication is unsafe") from exc
        finally:
            for item in staged:
                try:
                    with open_directory(project / "references") as directory_fd:
                        atomic_remove_regular_file_at(
                            directory_fd, item.temp_name, item.identity,
                            label="staged reference cleanup")
                except FileNotFoundError:
                    pass
                except (SafeFilesystemError, OSError):
                    raise
            for upload in uploads:
                try:
                    upload.file.close()
                except OSError:
                    pass
