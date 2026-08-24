from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from webapp.identifiers import validate_clip_id
from webapp.models import ChatEvent, Job, JobEvent, JobStatus
from webapp.runtime_schema import RuntimeSchemaError, initialize_runtime_schema
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_write_bytes_at,
    open_directory,
    open_regular_file,
    read_opened_text,
)


log = logging.getLogger(__name__)


class JobStoreError(RuntimeError):
    pass


class ActiveJobError(JobStoreError):
    pass


class JobNotFoundError(JobStoreError):
    pass


class InvalidTransitionError(JobStoreError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class JobStore:
    def __init__(self, database_path: Path):
        self.database_path = database_path

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self):
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        if self.database_path.parent.is_symlink():
            raise JobStoreError("runtime directory may not be a symlink")
        self.database_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.database_path.parent.chmod(0o700)
        for suffix in ("", "-wal", "-shm"):
            if Path(f"{self.database_path}{suffix}").is_symlink():
                raise JobStoreError("runtime database files may not be symlinks")
        if self.database_path.exists():
            self.database_path.chmod(0o600)
        with self._connection() as connection:
            self.database_path.chmod(0o600)
            connection.execute("PRAGMA journal_mode = WAL")
            try:
                initialize_runtime_schema(connection)
            except RuntimeSchemaError as exc:
                raise JobStoreError(str(exc)) from exc
        for suffix in ("", "-wal", "-shm"):
            path = Path(f"{self.database_path}{suffix}")
            if path.exists():
                path.chmod(0o600)

    def register_worker(self, owner_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO workers(owner_id, pid, heartbeat) VALUES (?, ?, ?) "
                "ON CONFLICT(owner_id) DO UPDATE SET "
                "pid = excluded.pid, heartbeat = excluded.heartbeat",
                (owner_id, os.getpid(), datetime.now().timestamp()),
            )

    def heartbeat_worker(self, owner_id: str) -> None:
        with self._connection() as connection:
            connection.execute(
                "UPDATE workers SET heartbeat = ? WHERE owner_id = ?",
                (datetime.now().timestamp(), owner_id),
            )
            connection.commit()

    def unregister_worker(self, owner_id: str) -> None:
        with self._transaction() as connection:
            connection.execute(
                "DELETE FROM workers WHERE owner_id = ?", (owner_id,)
            )

    @staticmethod
    def _job_from_row(row: sqlite3.Row, reply: str = "") -> Job:
        return Job(
            id=row["id"],
            project=row["project"],
            clip_id=row["clip_id"],
            kind=row["kind"],
            profile=row["profile"],
            status=JobStatus(row["status"]),
            message=row["message"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            owner_id=row["owner_id"],
            pid=row["pid"],
            pid_start_time=row["pid_start_time"],
            reply=reply,
        )

    @staticmethod
    def _reply(connection: sqlite3.Connection, job_id: str) -> str:
        row = connection.execute(
            "SELECT content FROM chat_events "
            "WHERE job_id = ? AND role = 'assistant'", (job_id,)
        ).fetchone()
        return row["content"] if row else ""

    @staticmethod
    def _append_job_event(connection: sqlite3.Connection, *, project: str,
                          job_id: str, profile: str, event_type: str,
                          status: str, summary: str,
                          detail: dict | None = None,
                          created_at: str | None = None) -> None:
        connection.execute(
            "INSERT INTO job_events "
            "(project, job_id, profile, event_type, status, summary, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project, job_id, profile, event_type, status, summary,
                json.dumps(detail or {}, ensure_ascii=False),
                created_at or utc_now(),
            ),
        )

    def _create_job(self, project: str, message: str, profile: str, *,
                    clip_id: str, kind: str, chat_content: str) -> Job:
        try:
            clip_id = validate_clip_id(clip_id)
        except ValueError as exc:
            raise JobStoreError(str(exc)) from exc
        now = utc_now()
        job = Job(
            id=uuid.uuid4().hex,
            project=project,
            clip_id=clip_id,
            kind=kind,
            profile=profile,
            status=JobStatus.QUEUED,
            message=message,
            error="",
            created_at=now,
            started_at="",
            finished_at="",
            owner_id="",
            pid=None,
            pid_start_time=None,
        )
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO jobs "
                    "(id, project, clip_id, kind, profile, status, message, error, "
                    "created_at, started_at, finished_at, owner_id, pid, "
                    "pid_start_time) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id, job.project, job.clip_id, job.kind, job.profile,
                        job.status.value,
                        job.message, job.error, job.created_at, job.started_at,
                        job.finished_at, job.owner_id, job.pid,
                        job.pid_start_time,
                    ),
                )
                connection.execute(
                    "INSERT INTO chat_events "
                    "(project, job_id, role, content, created_at) "
                    "VALUES (?, ?, 'user', ?, ?)",
                    (job.project, job.id, chat_content, now),
                )
                self._append_job_event(
                    connection,
                    project=job.project,
                    job_id=job.id,
                    profile=job.profile,
                    event_type="job.queued",
                    status="queued",
                    summary=(
                        f"{job.profile} generation queued"
                        if kind == "generate" else f"{job.profile} queued"),
                    created_at=now,
                )
        except sqlite3.IntegrityError as exc:
            raise ActiveJobError(
                "project already has an active Studio job") from exc
        return job

    def create_chat_job(self, project: str, message: str,
                        profile: str = "studio", *, clip_id: str) -> Job:
        return self._create_job(
            project, message, profile, clip_id=clip_id, kind="chat",
            chat_content=message)

    def create_generation_job(self, project: str, request: str,
                              profile: str = "studio", *, clip_id: str) -> Job:
        return self._create_job(
            project, request, profile, clip_id=clip_id, kind="generate",
            chat_content="Generate with this prompt")

    def get_job(self, job_id: str) -> Job:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFoundError(job_id)
            return self._job_from_row(row, self._reply(connection, job_id))

    def list_jobs(self, project: str, limit: int = 20) -> list[Job]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE project = ? "
                "ORDER BY created_at DESC LIMIT ?", (project, limit)
            ).fetchall()
            return [
                self._job_from_row(row, self._reply(connection, row["id"]))
                for row in rows
            ]

    def active_jobs(self) -> list[Job]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued', 'running') "
                "ORDER BY created_at"
            ).fetchall()
            return [self._job_from_row(row) for row in rows]

    def claim(self, job_id: str, owner_id: str) -> Job | None:
        started_at = utc_now()
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, "
                "owner_id = ? WHERE id = ? AND status = 'queued' "
                "AND NOT EXISTS (SELECT 1 FROM jobs WHERE status = 'running') "
                "AND id = (SELECT id FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at, id LIMIT 1)",
                (started_at, owner_id, job_id),
            )
            if cursor.rowcount != 1:
                row = connection.execute(
                    "SELECT status FROM jobs WHERE id = ?", (job_id,)
                ).fetchone()
                if not row:
                    raise JobNotFoundError(job_id)
                if row["status"] != "queued":
                    raise InvalidTransitionError(
                        f"job {job_id} is not queued")
                return None
            row = connection.execute(
                "SELECT project, profile FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            self._append_job_event(
                connection,
                project=row["project"],
                job_id=job_id,
                profile=row["profile"],
                event_type="job.started",
                status="running",
                summary=f"{row['profile']} started",
                created_at=started_at,
            )
        return self.get_job(job_id)

    def claim_next(self, owner_id: str) -> Job | None:
        """Atomically claim the oldest queued job when no job is running."""
        started_at = utc_now()
        with self._transaction() as connection:
            if connection.execute(
                    "SELECT 1 FROM jobs WHERE status = 'running'").fetchone():
                return None
            row = connection.execute(
                "SELECT id FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at, id LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, "
                "owner_id = ? WHERE id = ? AND status = 'queued'",
                (started_at, owner_id, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
            job_row = connection.execute(
                "SELECT project, profile FROM jobs WHERE id = ?", (row["id"],)
            ).fetchone()
            self._append_job_event(
                connection,
                project=job_row["project"],
                job_id=row["id"],
                profile=job_row["profile"],
                event_type="job.started",
                status="running",
                summary=f"{job_row['profile']} started",
                created_at=started_at,
            )
        return self.get_job(row["id"])

    def claim_stale_running(self, owner_id: str,
                            stale_before: float) -> Job | None:
        """Take ownership of one running job whose worker lease expired."""
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT jobs.id FROM jobs "
                "LEFT JOIN workers ON workers.owner_id = jobs.owner_id "
                "WHERE jobs.status = 'running' AND "
                "(workers.owner_id IS NULL OR workers.heartbeat < ?) "
                "ORDER BY jobs.started_at, jobs.id LIMIT 1",
                (stale_before,),
            ).fetchone()
            if not row:
                return None
            cursor = connection.execute(
                "UPDATE jobs SET owner_id = ? WHERE id = ? "
                "AND status = 'running'",
                (owner_id, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
        return self.get_job(row["id"])

    def set_process(self, job_id: str, owner_id: str, pid: int,
                    pid_start_time: int) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET pid = ?, pid_start_time = ? "
                "WHERE id = ? AND status = 'running' AND owner_id = ?",
                (pid, pid_start_time, job_id, owner_id)
            )
            if cursor.rowcount != 1:
                raise InvalidTransitionError(
                    f"job {job_id} is not owned by {owner_id}")

    def complete(self, job_id: str, owner_id: str, reply: str,
                 session_id: str | None) -> Job:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM jobs WHERE id = ? AND status = 'running' "
                "AND owner_id = ?", (job_id, owner_id)
            ).fetchone()
            if not row:
                raise InvalidTransitionError(
                    f"job {job_id} is not running for {owner_id}")
            self._insert_turn(
                connection, row["project"], job_id, row["message"],
                "assistant", reply, now,
            )
            if session_id:
                connection.execute(
                    "INSERT INTO profile_sessions "
                    "(project, profile, session_id, updated_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(project, profile) DO UPDATE SET "
                    "session_id = excluded.session_id, "
                    "updated_at = excluded.updated_at",
                    (row["project"], row["profile"], session_id, now),
                )
            connection.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ?, "
                "error = '', pid = NULL, pid_start_time = NULL WHERE id = ?",
                (now, job_id)
            )
            self._append_job_event(
                connection,
                project=row["project"],
                job_id=job_id,
                profile=row["profile"],
                event_type="job.completed",
                status="completed",
                summary=f"{row['profile']} completed",
                created_at=now,
            )
        return self.get_job(job_id)

    def fail(self, job_id: str, error: str, owner_id: str | None = None) -> Job:
        now = utc_now()
        with self._transaction() as connection:
            query = "SELECT * FROM jobs WHERE id = ? AND status IN ('queued', 'running')"
            params: tuple = (job_id,)
            if owner_id is not None:
                query += " AND owner_id = ?"
                params = (job_id, owner_id)
            row = connection.execute(query, params).fetchone()
            if not row:
                return self.get_job(job_id)
            self._insert_turn(
                connection, row["project"], job_id, row["message"],
                "system", f"Studio job failed: {error}", now,
            )
            connection.execute(
                "UPDATE jobs SET status = 'failed', finished_at = ?, "
                "error = ?, pid = NULL, pid_start_time = NULL WHERE id = ?",
                (now, error, job_id)
            )
            self._append_job_event(
                connection,
                project=row["project"],
                job_id=job_id,
                profile=row["profile"],
                event_type="job.failed",
                status="failed",
                summary=error,
                created_at=now,
            )
        return self.get_job(job_id)

    @staticmethod
    def _insert_turn(connection: sqlite3.Connection, project: str,
                     job_id: str, message: str, response_role: str,
                     response: str, created_at: str) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO chat_events "
            "(project, job_id, role, content, created_at) "
            "VALUES (?, ?, 'user', ?, ?)",
            (project, job_id, message, created_at),
        )
        connection.execute(
            "INSERT OR IGNORE INTO chat_events "
            "(project, job_id, role, content, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (project, job_id, response_role, response, created_at),
        )

    def get_session(self, project: str, profile: str = "studio") -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT session_id FROM profile_sessions "
                "WHERE project = ? AND profile = ?", (project, profile)
            ).fetchone()
            return row["session_id"] if row else None

    def import_chat_if_empty(self, project: str, chat_path: Path) -> None:
        try:
            with open_regular_file(chat_path):
                pass
        except FileNotFoundError:
            return
        except (SafeFilesystemError, OSError) as exc:
            raise ValueError("chat export is unsafe") from exc
        try:
            with open_directory(chat_path.parent) as parent_fd:
                lock_fd = os.open(
                    ".chat.lock",
                    os.O_RDWR | os.O_CREAT | os.O_APPEND |
                    os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(lock_fd, "a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
                    try:
                        with open_regular_file(chat_path) as opened:
                            lines = read_opened_text(opened).splitlines()
                        with self._transaction() as connection:
                            count = connection.execute(
                                "SELECT COUNT(*) AS n FROM chat_events "
                                "WHERE project = ?", (project,),
                            ).fetchone()["n"]
                            if count:
                                return
                            for line_number, line in enumerate(lines, start=1):
                                try:
                                    item = json.loads(line)
                                except json.JSONDecodeError as exc:
                                    log.warning(
                                        "Skipping corrupt chat record %s:%d: %s",
                                        chat_path, line_number, exc)
                                    continue
                                role = item.get("role")
                                content = item.get("content")
                                if (role not in {"user", "assistant", "system"}
                                        or not isinstance(content, str)):
                                    continue
                                connection.execute(
                                    "INSERT INTO chat_events "
                                    "(project, job_id, role, content, created_at) "
                                    "VALUES (?, NULL, ?, ?, ?)",
                                    (project, role, content,
                                     item.get("ts") or utc_now()),
                                )
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (SafeFilesystemError, OSError, UnicodeDecodeError) as exc:
            raise ValueError("chat export is unsafe") from exc

    def append_external_event(self, project: str, role: str,
                              content: str) -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"invalid chat role: {role!r}")
        with self._transaction() as connection:
            if role == "user" and connection.execute(
                "SELECT 1 FROM chat_events "
                "JOIN jobs ON jobs.id = chat_events.job_id "
                "WHERE chat_events.project = ? AND chat_events.role = 'user' "
                "AND chat_events.content = ? "
                "AND jobs.status IN ('queued', 'running') LIMIT 1",
                (project, content),
            ).fetchone():
                return
            connection.execute(
                "INSERT INTO chat_events "
                "(project, job_id, role, content, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (project, role, content, utc_now()),
            )

    def append_job_event(self, job_id: str, profile: str, event_type: str,
                         summary: str, status: str = "",
                         detail: dict | None = None) -> None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT project FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not row:
                raise JobNotFoundError(job_id)
            self._append_job_event(
                connection,
                project=row["project"],
                job_id=job_id,
                profile=profile,
                event_type=event_type,
                status=status,
                summary=summary,
                detail=detail,
            )

    def job_events(self, project: str, after: int = 0) -> tuple[int, list[JobEvent]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE project = ? AND id > ? "
                "ORDER BY id", (project, after)
            ).fetchall()
            events = []
            for row in rows:
                try:
                    detail = json.loads(row["detail"] or "{}")
                except (json.JSONDecodeError, TypeError):
                    detail = {}
                events.append(JobEvent(
                    id=row["id"],
                    project=row["project"],
                    job_id=row["job_id"],
                    profile=row["profile"],
                    event_type=row["event_type"],
                    status=row["status"],
                    summary=row["summary"],
                    detail=detail,
                    created_at=row["created_at"],
                ))
            return rows[-1]["id"] if rows else after, events

    def chat_events(self, project: str, after: int = 0) -> tuple[int, list[ChatEvent]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT chat_events.*, COALESCE(jobs.profile, '') AS profile "
                "FROM chat_events LEFT JOIN jobs ON jobs.id = chat_events.job_id "
                "WHERE chat_events.project = ? AND chat_events.id > ? "
                "ORDER BY chat_events.id", (project, after)
            ).fetchall()
            events = [
                ChatEvent(
                    id=row["id"], project=row["project"],
                    job_id=row["job_id"], role=row["role"],
                    content=row["content"], created_at=row["created_at"],
                    profile=row["profile"],
                )
                for row in rows
            ]
            return rows[-1]["id"] if rows else after, events

    def export_chat(self, project: str, chat_path: Path) -> None:
        _, events = self.chat_events(project)
        data = "".join(
            json.dumps(event.to_dict(), ensure_ascii=False) + "\n"
            for event in events
        ).encode("utf-8")
        try:
            with open_directory(chat_path.parent) as parent_fd:
                lock_fd = os.open(
                    ".chat.lock",
                    os.O_RDWR | os.O_CREAT | os.O_APPEND |
                    os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(lock_fd, "a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        atomic_write_bytes_at(
                            parent_fd, chat_path.name, data,
                            label="project chat export")
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (SafeFilesystemError, OSError) as exc:
            raise ValueError("chat export is unsafe") from exc
