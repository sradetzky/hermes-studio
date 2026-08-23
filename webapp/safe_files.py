from __future__ import annotations

import ctypes
import errno
import os
import stat
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


_RENAME_NOREPLACE = 1
_RENAME_EXCHANGE = 2
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
_COPY_CHUNK_SIZE = 1024 * 1024


class SafeFilesystemError(ValueError):
    """A path could not be opened without following links or special files."""


class AtomicPublicationUnavailable(RuntimeError):
    """The host cannot provide atomic no-replace directory publication."""


@dataclass(frozen=True)
class OpenedRegularFile:
    descriptor: int
    path: Path
    stat: os.stat_result

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def identity(self) -> tuple[int, int]:
        return (self.stat.st_dev, self.stat.st_ino)


_libc = ctypes.CDLL(None, use_errno=True)
_renameat2 = getattr(_libc, "renameat2", None)
if _renameat2 is not None:
    _renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    _renameat2.restype = ctypes.c_int


def _component(value: str, label: str) -> str:
    if (not value or value in {".", ".."} or "/" in value or "\\" in value
            or Path(value).name != value):
        raise SafeFilesystemError(f"invalid {label}: {value!r}")
    return value


def _open_directory(path: Path | str, *, dir_fd: int | None = None) -> int:
    try:
        descriptor = os.open(path, _DIRECTORY_FLAGS, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeFilesystemError(f"unsafe directory: {path}") from exc
    try:
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise SafeFilesystemError(f"not a regular directory: {path}")
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _absolute_path(path: Path | str) -> Path:
    """Normalize a path lexically without following any filesystem entry."""
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _open_absolute_directory(path: Path | str) -> int:
    """Open an absolute directory by walking from an already-open `/`."""
    absolute = _absolute_path(path)
    descriptor = _open_directory(absolute.anchor)
    try:
        for part in absolute.parts[1:]:
            next_fd = _open_directory(
                _component(part, "directory component"), dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_fd
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


@contextmanager
def open_directory(path: Path | str) -> Iterator[int]:
    """Descriptor-walk and retain a directory without following symlinks."""
    descriptor = _open_absolute_directory(path)
    try:
        verify_absolute_directory_identity(
            path, descriptor, label="directory")
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def open_directory_at(parent_fd: int, name: str) -> Iterator[int]:
    """Open and retain one literal child directory beneath a retained parent."""
    descriptor = _open_directory(
        _component(name, "directory component"), dir_fd=parent_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def _identity(descriptor: int) -> tuple[int, int]:
    details = os.fstat(descriptor)
    return (details.st_dev, details.st_ino)


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def verify_absolute_directory_identity(
        path: Path | str, descriptor: int, *, label: str) -> None:
    """Verify that a retained directory is still named by an absolute path."""
    absolute = _absolute_path(path)
    try:
        current_fd = _open_absolute_directory(absolute)
    except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
        raise SafeFilesystemError(f"{label} changed while opening") from exc
    try:
        if _identity(current_fd) != _identity(descriptor):
            raise SafeFilesystemError(f"{label} changed while opening")
    finally:
        os.close(current_fd)


@contextmanager
def open_regular_file(path: Path) -> Iterator[OpenedRegularFile]:
    """Descriptor-walk a path and retain its final regular-file fd."""
    absolute = _absolute_path(path)
    directory_fd = _open_absolute_directory(absolute.parent)
    try:
        try:
            descriptor = os.open(
                _component(absolute.name, "filename"), _FILE_FLAGS,
                dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SafeFilesystemError(
                f"not a safe regular file: {absolute}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise SafeFilesystemError(
                    f"not a safe regular file: {absolute}")
            verify_absolute_directory_identity(
                absolute.parent, directory_fd, label="file parent directory")
            yield OpenedRegularFile(descriptor, absolute, details)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


@contextmanager
def open_regular_file_at(
        parent_fd: int, name: str, *, path: Path | None = None,
) -> Iterator[OpenedRegularFile]:
    """Open and retain one literal regular file beneath a retained directory."""
    name = _component(name, "filename")
    try:
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise SafeFilesystemError(f"not a safe regular file: {name}") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise SafeFilesystemError(f"not a safe regular file: {name}")
        yield OpenedRegularFile(descriptor, path or Path(name), details)
    finally:
        os.close(descriptor)


def _relative_beneath(trusted_root: Path, candidate: Path | str) -> Path:
    candidate_path = Path(candidate).expanduser()
    if candidate_path.is_absolute():
        normalized = Path(os.path.abspath(candidate_path))
        try:
            relative = normalized.relative_to(trusted_root)
        except ValueError as exc:
            raise SafeFilesystemError(
                f"path escapes trusted directory: {candidate_path}") from exc
    else:
        relative = Path(os.path.normpath(candidate_path))
    if relative == Path(".") or any(part in {"", ".", ".."}
                                     for part in relative.parts):
        raise SafeFilesystemError(
            f"path escapes trusted directory: {candidate_path}")
    return relative


@contextmanager
def open_regular_beneath(
        trusted_root: Path, candidate: Path | str) -> Iterator[OpenedRegularFile]:
    """Walk beneath an opened root and retain the final regular-file fd."""
    root = _absolute_path(trusted_root)
    relative = _relative_beneath(root, candidate)
    directory_fd = _open_absolute_directory(root)
    try:
        for part in relative.parts[:-1]:
            part = _component(part, "path component")
            next_fd = _open_directory(part, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        filename = _component(relative.parts[-1], "filename")
        try:
            descriptor = os.open(filename, _FILE_FLAGS, dir_fd=directory_fd)
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise SafeFilesystemError(
                f"not a safe regular file beneath {root}: {relative}") from exc
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise SafeFilesystemError(
                    f"not a safe regular file beneath {root}: {relative}")
            verify_absolute_directory_identity(
                root.joinpath(*relative.parts[:-1]), directory_fd,
                label="trusted file parent directory")
            yield OpenedRegularFile(descriptor, root / relative, details)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)


def read_opened_bytes(opened: OpenedRegularFile) -> bytes:
    os.lseek(opened.descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(opened.descriptor, _COPY_CHUNK_SIZE)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def read_opened_text(opened: OpenedRegularFile, *, encoding: str = "utf-8") -> str:
    return read_opened_bytes(opened).decode(encoding)


def copy_opened_file_at(
        opened: OpenedRegularFile, parent_fd: int, name: str,
) -> tuple[int, int]:
    """Copy an opened file to one exclusive name beneath a retained directory."""
    name = _component(name, "copy target")
    mode = stat.S_IMODE(opened.stat.st_mode)
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=parent_fd,
    )
    try:
        os.lseek(opened.descriptor, 0, os.SEEK_SET)
        while True:
            chunk = os.read(opened.descriptor, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        details = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    os.utime(
        name,
        ns=(opened.stat.st_atime_ns, opened.stat.st_mtime_ns),
        follow_symlinks=False,
        dir_fd=parent_fd,
    )
    _fsync_directory(parent_fd)
    return (details.st_dev, details.st_ino)


def copy_opened_file(opened: OpenedRegularFile, target: Path) -> None:
    """Copy data and basic copy2 metadata from an already-open descriptor."""
    target = Path(target)
    parent_fd = _open_absolute_directory(target.parent)
    try:
        copy_opened_file_at(opened, parent_fd, target.name)
    finally:
        os.close(parent_fd)


def _atomic_move_no_replace_at(
        source_parent_fd: int, source_name: str,
        destination_parent_fd: int, destination_name: str, *,
        source_label: Path | str, destination_label: Path | str,
        expected_source_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Rename a retained-parent entry without following or replacing names."""
    if _renameat2 is None:
        raise AtomicPublicationUnavailable(
            "renameat2(RENAME_NOREPLACE) is unavailable")

    source_name = _component(source_name, "source entry")
    destination_name = _component(destination_name, "destination entry")
    try:
        details = os.stat(
            source_name, dir_fd=source_parent_fd, follow_symlinks=False)
        source_fd = None
        destination_fd = None
        try:
            if stat.S_ISDIR(details.st_mode):
                source_fd = _open_directory(
                    source_name, dir_fd=source_parent_fd)

                def open_destination() -> int:
                    return _open_directory(
                        destination_name, dir_fd=destination_parent_fd)
            elif stat.S_ISREG(details.st_mode):
                source_fd = os.open(
                    source_name, _FILE_FLAGS, dir_fd=source_parent_fd)
                if not stat.S_ISREG(os.fstat(source_fd).st_mode):
                    raise SafeFilesystemError(
                        f"move source is not regular: {source_label}")

                def open_destination() -> int:
                    descriptor = os.open(
                        destination_name, _FILE_FLAGS,
                        dir_fd=destination_parent_fd)
                    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                        os.close(descriptor)
                        raise SafeFilesystemError(
                            f"move target is not regular: {destination_label}")
                    return descriptor
            else:
                raise SafeFilesystemError(
                    f"move source is not regular: {source_label}")

            source_identity = _identity(source_fd)
            if source_identity != (details.st_dev, details.st_ino):
                raise SafeFilesystemError("move source changed while opening")
            if (expected_source_identity is not None
                    and source_identity != expected_source_identity):
                raise SafeFilesystemError(
                    "move source identity does not match the expected entry")
            if os.fstat(destination_parent_fd).st_dev != details.st_dev:
                raise SafeFilesystemError(
                    "move source and destination must share a filesystem")

            result = _renameat2(
                source_parent_fd,
                os.fsencode(source_name),
                destination_parent_fd,
                os.fsencode(destination_name),
                _RENAME_NOREPLACE,
            )
            if result != 0:
                error = ctypes.get_errno()
                if error in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileExistsError(
                        error, os.strerror(error), destination_label)
                if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
                    raise AtomicPublicationUnavailable(
                        "renameat2(RENAME_NOREPLACE) is unavailable")
                raise OSError(error, os.strerror(error), destination_label)

            destination_fd = open_destination()
            if _identity(destination_fd) != source_identity:
                raise SafeFilesystemError(
                    "move target identity does not match the source")
            source_parent_identity = _identity(source_parent_fd)
            destination_parent_identity = _identity(destination_parent_fd)
            _fsync_directory(source_parent_fd)
            if destination_parent_identity != source_parent_identity:
                _fsync_directory(destination_parent_fd)
            return source_identity
        finally:
            if destination_fd is not None:
                os.close(destination_fd)
            if source_fd is not None:
                os.close(source_fd)
    except (FileNotFoundError, FileExistsError, SafeFilesystemError,
            AtomicPublicationUnavailable):
        raise
    except OSError as exc:
        raise SafeFilesystemError(
            f"move entry is unsafe: {source_label} -> {destination_label}") from exc


def atomic_move_no_replace_at(
        parent_fd: int, source_name: str, destination_name: str, *,
        expected_source_identity: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Atomically move safe components beneath one retained parent descriptor."""
    return _atomic_move_no_replace_at(
        parent_fd, source_name, parent_fd, destination_name,
        source_label=source_name, destination_label=destination_name,
        expected_source_identity=expected_source_identity,
    )


def _named_regular_identity(
        parent_fd: int, name: str, *, label: str) -> tuple[int, int]:
    """Open one retained-parent name and return its regular-file identity."""
    descriptor = None
    try:
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(named.st_mode):
            raise SafeFilesystemError(f"{label} is not a safe regular file")
        descriptor = os.open(name, _FILE_FLAGS, dir_fd=parent_fd)
        opened = os.fstat(descriptor)
        identity = (opened.st_dev, opened.st_ino)
        if (not stat.S_ISREG(opened.st_mode)
                or identity != (named.st_dev, named.st_ino)):
            raise SafeFilesystemError(f"{label} changed while opening")
        return identity
    except (FileNotFoundError, SafeFilesystemError):
        raise
    except OSError as exc:
        raise SafeFilesystemError(f"{label} is not a safe regular file") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _rename_exchange_at(
        parent_fd: int, source: str, target: str, *, label: str) -> None:
    if _renameat2 is None:
        raise AtomicPublicationUnavailable(
            "renameat2(RENAME_EXCHANGE) is unavailable")
    result = _renameat2(
        parent_fd,
        os.fsencode(source),
        parent_fd,
        os.fsencode(target),
        _RENAME_EXCHANGE,
    )
    if result == 0:
        return
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise AtomicPublicationUnavailable(
            "renameat2(RENAME_EXCHANGE) is unavailable")
    raise SafeFilesystemError(f"{label} exchange failed") from OSError(
        error, os.strerror(error), target)


def atomic_exchange_regular_file_at(
        parent_fd: int, source_name: str, target_name: str, *,
        expected_source_identity: tuple[int, int],
        expected_target_identity: tuple[int, int],
        label: str,
) -> tuple[int, int]:
    """CAS-publish a regular temp over an expected regular target.

    ``RENAME_EXCHANGE`` makes the target check atomic with publication. The
    displaced target is verified under the source name. A mismatch is exchanged
    back and both entries are retained; only a verified expected target is
    removed, via the quarantine deletion primitive.
    """
    source_name = _component(source_name, "exchange source entry")
    target_name = _component(target_name, "exchange target entry")
    source_identity = _named_regular_identity(
        parent_fd, source_name, label=f"{label} source")
    if source_identity != expected_source_identity:
        raise SafeFilesystemError(
            f"{label} source identity does not match the expected source")

    # Reject links and special targets here, but check the expected identity
    # only after exchange. A last-instant replacement must be displaced and
    # detected at the atomic boundary, not accidentally blessed.
    _named_regular_identity(parent_fd, target_name, label=f"{label} target")
    exchanged = False
    published_identity = displaced_identity = None
    try:
        _rename_exchange_at(parent_fd, source_name, target_name, label=label)
        exchanged = True
        _fsync_directory(parent_fd)
        published_identity = _named_regular_identity(
            parent_fd, target_name, label=f"{label} published target")
        displaced_identity = _named_regular_identity(
            parent_fd, source_name, label=f"{label} displaced target")
        if (published_identity != source_identity
                or displaced_identity != expected_target_identity):
            reason = (
                "published source identity changed"
                if published_identity != source_identity
                else "expected target identity changed")
            raise SafeFilesystemError(f"{label} {reason} during exchange")
    except BaseException as exc:
        if exchanged:
            try:
                _rename_exchange_at(
                    parent_fd, source_name, target_name,
                    label=f"{label} rollback")
                _fsync_directory(parent_fd)
                rolled_source = _named_regular_identity(
                    parent_fd, source_name, label=f"{label} rolled-back source")
                rolled_target = _named_regular_identity(
                    parent_fd, target_name, label=f"{label} rolled-back target")
                if (rolled_source != published_identity
                        or rolled_target != displaced_identity):
                    raise SafeFilesystemError(
                        f"{label} rollback identity could not be verified")
            except BaseException as rollback_exc:
                raise SafeFilesystemError(
                    f"{label} exchange failed and rollback could not be verified"
                ) from rollback_exc
        raise exc

    assert published_identity == source_identity
    assert displaced_identity == expected_target_identity
    atomic_remove_regular_file_at(
        parent_fd, source_name, expected_target_identity,
        label=f"{label} displaced target")
    return source_identity


def _preserve_quarantined_regular_file(
        parent_fd: int, quarantine: str, canonical: str,
        quarantine_identity: tuple[int, int] | None, *, label: str) -> None:
    """Restore a displaced entry without overwriting a canonical replacement."""
    assert _renameat2 is not None
    result = _renameat2(
        parent_fd,
        os.fsencode(quarantine),
        parent_fd,
        os.fsencode(canonical),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            _fsync_directory(parent_fd)
            return
        if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise AtomicPublicationUnavailable(
                "renameat2(RENAME_NOREPLACE) is unavailable")
        raise SafeFilesystemError(
            f"{label} could not be preserved from quarantine") from OSError(
                error, os.strerror(error), quarantine)

    _fsync_directory(parent_fd)
    if quarantine_identity is None:
        return
    descriptor = None
    try:
        descriptor = os.open(canonical, _FILE_FLAGS, dir_fd=parent_fd)
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode)
                or (details.st_dev, details.st_ino) != quarantine_identity):
            raise SafeFilesystemError(
                f"{label} restored quarantine identity could not be verified")
    except (SafeFilesystemError, FileNotFoundError):
        raise
    except OSError as exc:
        raise SafeFilesystemError(
            f"{label} restored quarantine identity could not be verified") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def atomic_remove_regular_file_at(
        parent_fd: int, canonical: str, expected_identity: tuple[int, int], *,
        label: str) -> None:
    """Quarantine and remove only one expected regular-file identity.

    The no-replace rename is the identity-conditional boundary: it first makes
    the canonical name absent without deleting the entry that occupied it.
    Any unexpected occupant is restored when possible, or left under the
    private quarantine when a new canonical occupant prevents restoration.
    """
    if _renameat2 is None:
        raise AtomicPublicationUnavailable(
            "renameat2(RENAME_NOREPLACE) is unavailable")
    canonical = _component(canonical, label)
    quarantine = f".safe-delete-{uuid.uuid4().hex}.quarantine"

    result = _renameat2(
        parent_fd,
        os.fsencode(canonical),
        parent_fd,
        os.fsencode(quarantine),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error = ctypes.get_errno()
        if error in {errno.EEXIST, errno.ENOTEMPTY}:
            raise SafeFilesystemError(
                f"{label} quarantine name collided")
        if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
            raise AtomicPublicationUnavailable(
                "renameat2(RENAME_NOREPLACE) is unavailable")
        if error == errno.ENOENT:
            raise FileNotFoundError(error, os.strerror(error), canonical)
        raise SafeFilesystemError(
            f"{label} could not be quarantined") from OSError(
                error, os.strerror(error), canonical)

    descriptor = None
    quarantine_identity = None
    try:
        try:
            descriptor = os.open(quarantine, _FILE_FLAGS, dir_fd=parent_fd)
            details = os.fstat(descriptor)
            quarantine_identity = (details.st_dev, details.st_ino)
        except OSError as exc:
            try:
                _preserve_quarantined_regular_file(
                    parent_fd, quarantine, canonical, None, label=label)
            except BaseException as preserve_exc:
                raise SafeFilesystemError(
                    f"{label} quarantine could not be verified or restored"
                ) from preserve_exc
            raise SafeFilesystemError(
                f"{label} quarantine could not be verified") from exc

        try:
            named = os.stat(
                quarantine, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            try:
                _preserve_quarantined_regular_file(
                    parent_fd, quarantine, canonical,
                    quarantine_identity, label=label)
            except BaseException as preserve_exc:
                raise SafeFilesystemError(
                    f"{label} quarantine changed and could not be restored"
                ) from preserve_exc
            raise SafeFilesystemError(
                f"{label} quarantine changed before deletion") from exc

        named_identity = (named.st_dev, named.st_ino)
        if (not stat.S_ISREG(details.st_mode)
                or not stat.S_ISREG(named.st_mode)
                or quarantine_identity != named_identity
                or quarantine_identity != expected_identity):
            try:
                _preserve_quarantined_regular_file(
                    parent_fd, quarantine, canonical,
                    quarantine_identity, label=label)
            except BaseException as preserve_exc:
                raise SafeFilesystemError(
                    f"{label} identity changed; displaced entry remains quarantined"
                ) from preserve_exc
            raise SafeFilesystemError(
                f"{label} identity changed before deletion")

        try:
            os.stat(canonical, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise SafeFilesystemError(
                f"{label} canonical absence could not be verified") from exc
        else:
            _fsync_directory(parent_fd)
            raise SafeFilesystemError(
                f"{label} canonical name was occupied; expected entry remains quarantined")

        try:
            os.unlink(quarantine, dir_fd=parent_fd)
        except OSError as exc:
            try:
                current = os.stat(
                    quarantine, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                raise SafeFilesystemError(
                    f"{label} quarantine cleanup could not be proven") from exc
            except OSError as inspect_exc:
                raise SafeFilesystemError(
                    f"{label} quarantine cleanup could not be proven") from inspect_exc
            if (stat.S_ISREG(current.st_mode)
                    and (current.st_dev, current.st_ino) == expected_identity):
                _preserve_quarantined_regular_file(
                    parent_fd, quarantine, canonical,
                    expected_identity, label=label)
            raise SafeFilesystemError(
                f"{label} quarantine cleanup failed; entry was preserved") from exc

        _fsync_directory(parent_fd)
        try:
            os.stat(canonical, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SafeFilesystemError(
                f"{label} canonical absence could not be revalidated") from exc
        raise SafeFilesystemError(
            f"{label} canonical name reappeared during deletion")
    finally:
        if descriptor is not None:
            os.close(descriptor)


def write_new_regular_file_at(
        parent_fd: int, name: str, data: bytes, *, mode: int = 0o600,
) -> tuple[int, int]:
    """Write, flush, and retain the identity of one exclusive regular file."""
    name = _component(name, "new file")
    descriptor = os.open(
        name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        details = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    _fsync_directory(parent_fd)
    return (details.st_dev, details.st_ino)


def atomic_write_bytes_at(
        parent_fd: int, target_name: str, data: bytes, *,
        label: str, mode: int = 0o600,
) -> tuple[int, int]:
    """CAS-publish complete bytes without following or overwriting a replacement."""
    target_name = _component(target_name, label)
    temp_name = f".safe-write-{uuid.uuid4().hex}.tmp"
    temp_identity = write_new_regular_file_at(
        parent_fd, temp_name, data, mode=mode)
    published = False
    try:
        try:
            target_identity = _named_regular_identity(
                parent_fd, target_name, label=label)
        except FileNotFoundError:
            identity = atomic_move_no_replace_at(
                parent_fd, temp_name, target_name,
                expected_source_identity=temp_identity)
        else:
            identity = atomic_exchange_regular_file_at(
                parent_fd, temp_name, target_name,
                expected_source_identity=temp_identity,
                expected_target_identity=target_identity,
                label=label,
            )
        published = True
        return identity
    finally:
        if not published:
            try:
                atomic_remove_regular_file_at(
                    parent_fd, temp_name, temp_identity,
                    label=f"{label} temporary file")
            except FileNotFoundError:
                pass


def atomic_move_no_replace(
        source: Path, destination: Path) -> tuple[int, int]:
    """Rename one regular file or directory without following or replacing."""
    source = Path(source)
    destination = Path(destination)
    source_parent_fd = _open_absolute_directory(source.parent)
    try:
        destination_parent_fd = _open_absolute_directory(destination.parent)
        try:
            return _atomic_move_no_replace_at(
                source_parent_fd, source.name,
                destination_parent_fd, destination.name,
                source_label=source, destination_label=destination,
            )
        finally:
            os.close(destination_parent_fd)
    finally:
        os.close(source_parent_fd)


def atomic_publish_directory(source: Path, destination: Path) -> tuple[int, int]:
    """Atomically rename a directory while refusing to replace any target."""
    source = Path(source)
    destination = Path(destination)
    if source.parent != destination.parent:
        raise ValueError("atomic directory publication requires one parent")
    source_name = _component(source.name, "source directory")
    destination_name = _component(destination.name, "destination directory")
    if _renameat2 is None:
        raise AtomicPublicationUnavailable(
            "renameat2(RENAME_NOREPLACE) is unavailable")

    parent_fd = _open_absolute_directory(source.parent)
    try:
        try:
            source_fd = _open_directory(source_name, dir_fd=parent_fd)
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise SafeFilesystemError(
                f"publication source is unsafe: {source}") from exc
        try:
            source_identity = _identity(source_fd)
            result = _renameat2(
                parent_fd,
                os.fsencode(source_name),
                parent_fd,
                os.fsencode(destination_name),
                _RENAME_NOREPLACE,
            )
            if result != 0:
                error = ctypes.get_errno()
                if error in {errno.EEXIST, errno.ENOTEMPTY}:
                    raise FileExistsError(error, os.strerror(error), destination)
                if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
                    raise AtomicPublicationUnavailable(
                        "renameat2(RENAME_NOREPLACE) is unavailable")
                raise OSError(error, os.strerror(error), destination)

            try:
                destination_fd = _open_directory(
                    destination_name, dir_fd=parent_fd)
            except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
                raise SafeFilesystemError(
                    "publication identity could not be verified") from exc
            try:
                if _identity(destination_fd) != source_identity:
                    raise SafeFilesystemError(
                        "publication identity does not match the source")
            finally:
                os.close(destination_fd)
            return source_identity
        finally:
            os.close(source_fd)
    finally:
        os.close(parent_fd)


def _remove_contents(directory_fd: int) -> None:
    for name in os.listdir(directory_fd):
        details = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if stat.S_ISDIR(details.st_mode):
            child_fd = _open_directory(name, dir_fd=directory_fd)
            try:
                _remove_contents(child_fd)
            finally:
                os.close(child_fd)
            os.rmdir(name, dir_fd=directory_fd)
        else:
            os.unlink(name, dir_fd=directory_fd)


def remove_published_directory_if_same(
        path: Path, identity: tuple[int, int]) -> bool:
    """Quarantine and remove only the exact directory that was published."""
    path = Path(path)
    name = _component(path.name, "published directory")
    if _renameat2 is None:
        return False
    try:
        parent_fd = _open_absolute_directory(path.parent)
    except (FileNotFoundError, SafeFilesystemError):
        return False
    try:
        try:
            directory_fd = _open_directory(name, dir_fd=parent_fd)
        except (FileNotFoundError, SafeFilesystemError):
            return False
        try:
            if _identity(directory_fd) != identity:
                return False

            quarantine = f".{name}.rollback-{uuid.uuid4().hex}"
            result = _renameat2(
                parent_fd,
                os.fsencode(name),
                parent_fd,
                os.fsencode(quarantine),
                _RENAME_NOREPLACE,
            )
            if result != 0:
                return False

            try:
                quarantine_fd = _open_directory(quarantine, dir_fd=parent_fd)
            except (FileNotFoundError, SafeFilesystemError):
                return False
            try:
                if (_identity(quarantine_fd) != identity
                        or _identity(quarantine_fd) != _identity(directory_fd)):
                    return False
                _remove_contents(quarantine_fd)
                current = os.stat(
                    quarantine, dir_fd=parent_fd, follow_symlinks=False)
                if ((current.st_dev, current.st_ino) != identity
                        or not stat.S_ISDIR(current.st_mode)):
                    return False
                os.rmdir(quarantine, dir_fd=parent_fd)
                return True
            finally:
                os.close(quarantine_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(parent_fd)
