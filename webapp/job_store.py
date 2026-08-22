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

from webapp.models import ChatEvent, Job, JobStatus


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
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    project TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (
                        status IN ('queued', 'running', 'completed', 'failed')
                    ),
                    message TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    started_at TEXT NOT NULL DEFAULT '',
                    finished_at TEXT NOT NULL DEFAULT '',
                    owner_id TEXT NOT NULL DEFAULT '',
                    pid INTEGER
                );

                CREATE UNIQUE INDEX IF NOT EXISTS one_active_job_per_project
                ON jobs(project)
                WHERE status IN ('queued', 'running');

                CREATE UNIQUE INDEX IF NOT EXISTS one_running_job_globally
                ON jobs((1))
                WHERE status = 'running';

                CREATE INDEX IF NOT EXISTS jobs_project_created
                ON jobs(project, created_at DESC);

                CREATE TABLE IF NOT EXISTS sessions (
                    project TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chat_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project TEXT NOT NULL,
                    job_id TEXT REFERENCES jobs(id) ON DELETE SET NULL,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(job_id, role)
                );

                CREATE INDEX IF NOT EXISTS chat_project_id
                ON chat_events(project, id);

                CREATE TABLE IF NOT EXISTS workers (
                    owner_id TEXT PRIMARY KEY,
                    pid INTEGER NOT NULL,
                    heartbeat REAL NOT NULL
                );
                """
            )

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
            kind=row["kind"],
            status=JobStatus(row["status"]),
            message=row["message"],
            error=row["error"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
            owner_id=row["owner_id"],
            pid=row["pid"],
            reply=reply,
        )

    @staticmethod
    def _reply(connection: sqlite3.Connection, job_id: str) -> str:
        row = connection.execute(
            "SELECT content FROM chat_events "
            "WHERE job_id = ? AND role = 'assistant'", (job_id,)
        ).fetchone()
        return row["content"] if row else ""

    def create_chat_job(self, project: str, message: str) -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            project=project,
            kind="chat",
            status=JobStatus.QUEUED,
            message=message,
            error="",
            created_at=utc_now(),
            started_at="",
            finished_at="",
            owner_id="",
            pid=None,
        )
        try:
            with self._transaction() as connection:
                connection.execute(
                    "INSERT INTO jobs "
                    "(id, project, kind, status, message, error, created_at, "
                    "started_at, finished_at, owner_id, pid) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.id, job.project, job.kind, job.status.value,
                        job.message, job.error, job.created_at, job.started_at,
                        job.finished_at, job.owner_id, job.pid,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ActiveJobError(
                "project already has an active Studio job") from exc
        return job

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
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET status = 'running', started_at = ?, "
                "owner_id = ? WHERE id = ? AND status = 'queued' "
                "AND NOT EXISTS (SELECT 1 FROM jobs WHERE status = 'running') "
                "AND id = (SELECT id FROM jobs WHERE status = 'queued' "
                "ORDER BY created_at, id LIMIT 1)",
                (utc_now(), owner_id, job_id),
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
        return self.get_job(job_id)

    def claim_next(self, owner_id: str) -> Job | None:
        """Atomically claim the oldest queued job when no job is running."""
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
                (utc_now(), owner_id, row["id"]),
            )
            if cursor.rowcount != 1:
                return None
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

    def set_pid(self, job_id: str, owner_id: str, pid: int) -> None:
        with self._transaction() as connection:
            cursor = connection.execute(
                "UPDATE jobs SET pid = ? WHERE id = ? AND status = 'running' "
                "AND owner_id = ?", (pid, job_id, owner_id)
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
                    "INSERT INTO sessions(project, session_id, updated_at) "
                    "VALUES (?, ?, ?) ON CONFLICT(project) DO UPDATE SET "
                    "session_id = excluded.session_id, "
                    "updated_at = excluded.updated_at",
                    (row["project"], session_id, now),
                )
            connection.execute(
                "UPDATE jobs SET status = 'completed', finished_at = ?, "
                "error = '', pid = NULL WHERE id = ?", (now, job_id)
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
                "error = ?, pid = NULL WHERE id = ?", (now, error, job_id)
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

    def get_session(self, project: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT session_id FROM sessions WHERE project = ?", (project,)
            ).fetchone()
            return row["session_id"] if row else None

    def import_chat_if_empty(self, project: str, chat_path: Path) -> None:
        if not chat_path.is_file():
            return
        lock_path = chat_path.with_name(".chat.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                with self._transaction() as connection:
                    count = connection.execute(
                        "SELECT COUNT(*) AS n FROM chat_events WHERE project = ?",
                        (project,),
                    ).fetchone()["n"]
                    if count:
                        return
                    for line_number, line in enumerate(
                            chat_path.read_text(encoding="utf-8").splitlines(), start=1):
                        try:
                            item = json.loads(line)
                        except json.JSONDecodeError as exc:
                            log.warning(
                                "Skipping corrupt chat record %s:%d: %s",
                                chat_path, line_number, exc)
                            continue
                        role = item.get("role")
                        content = item.get("content")
                        if (role not in {"user", "assistant", "system"} or
                                not isinstance(content, str)):
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

    def append_external_event(self, project: str, role: str,
                              content: str) -> None:
        if role not in {"user", "assistant", "system"}:
            raise ValueError(f"invalid chat role: {role!r}")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO chat_events "
                "(project, job_id, role, content, created_at) "
                "VALUES (?, NULL, ?, ?, ?)",
                (project, role, content, utc_now()),
            )

    def chat_events(self, project: str, after: int = 0) -> tuple[int, list[ChatEvent]]:
        with self._connection() as connection:
            total = connection.execute(
                "SELECT COUNT(*) AS n FROM chat_events WHERE project = ?",
                (project,),
            ).fetchone()["n"]
            rows = connection.execute(
                "SELECT * FROM chat_events WHERE project = ? "
                "ORDER BY id LIMIT -1 OFFSET ?", (project, after)
            ).fetchall()
            events = [
                ChatEvent(
                    id=row["id"], project=row["project"],
                    job_id=row["job_id"], role=row["role"],
                    content=row["content"], created_at=row["created_at"],
                )
                for row in rows
            ]
            return total, events

    def export_chat(self, project: str, chat_path: Path) -> None:
        chat_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = chat_path.with_name(".chat.lock")
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                _, events = self.chat_events(project)
                temp = chat_path.with_name(
                    f".{chat_path.name}.{uuid.uuid4().hex}.tmp")
                with temp.open("w", encoding="utf-8") as handle:
                    for event in events:
                        handle.write(json.dumps(
                            event.to_dict(), ensure_ascii=False) + "\n")
                temp.replace(chat_path)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
