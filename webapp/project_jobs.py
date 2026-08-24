from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from webapp.safe_files import SafeFilesystemError, open_directory


class ProjectJobGuardError(ValueError):
    pass


@contextmanager
def project_job_guard(project: Path) -> Iterator[None]:
    """Serialize job creation with project inputs that jobs depend on."""
    try:
        with open_directory(project) as project_fd:
            descriptor = os.open(
                ".project-jobs.lock",
                os.O_RDWR | os.O_CREAT | os.O_APPEND |
                os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
                dir_fd=project_fd,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                os.close(descriptor)
                raise ProjectJobGuardError("project job lock is unsafe")
            with os.fdopen(descriptor, "a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except ProjectJobGuardError:
        raise
    except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
        raise ProjectJobGuardError("project job lock is unsafe") from exc
