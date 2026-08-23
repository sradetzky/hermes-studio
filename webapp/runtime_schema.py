from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone


CURRENT_SCHEMA_VERSION = 3
LEGACY_CLIP_ERROR = "Legacy active job lacked an exact clip binding"


BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    clip_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    profile TEXT NOT NULL DEFAULT 'studio',
    status TEXT NOT NULL CHECK (
        status IN ('queued', 'running', 'completed', 'failed')
    ),
    message TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    owner_id TEXT NOT NULL DEFAULT '',
    pid INTEGER,
    pid_start_time INTEGER
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

CREATE TABLE IF NOT EXISTS profile_sessions (
    project TEXT NOT NULL,
    profile TEXT NOT NULL,
    session_id TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(project, profile)
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

CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    profile TEXT NOT NULL,
    event_type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS job_events_project_id
ON job_events(project, id);

CREATE INDEX IF NOT EXISTS job_events_job_id
ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS workers (
    owner_id TEXT PRIMARY KEY,
    pid INTEGER NOT NULL,
    heartbeat REAL NOT NULL
);
"""


class RuntimeSchemaError(RuntimeError):
    pass


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in connection.execute(
            f"PRAGMA table_info({table})").fetchall()
    }


def _migration_1_job_columns(connection: sqlite3.Connection) -> None:
    columns = _columns(connection, "jobs")
    if "profile" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN profile TEXT NOT NULL DEFAULT 'studio'")
    if "clip_id" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN clip_id TEXT NOT NULL DEFAULT ''")
    if "pid_start_time" not in columns:
        connection.execute(
            "ALTER TABLE jobs ADD COLUMN pid_start_time INTEGER")


def _migration_2_event_and_session_backfill(
        connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT INTO job_events "
        "(project, job_id, profile, event_type, status, summary, detail, "
        "created_at) "
        "SELECT jobs.project, jobs.id, jobs.profile, 'job.queued', "
        "'queued', jobs.profile || ' queued', '{}', jobs.created_at "
        "FROM jobs WHERE NOT EXISTS ("
        "SELECT 1 FROM job_events WHERE job_events.job_id = jobs.id)"
    )
    connection.execute(
        "INSERT INTO job_events "
        "(project, job_id, profile, event_type, status, summary, detail, "
        "created_at) "
        "SELECT jobs.project, jobs.id, jobs.profile, "
        "CASE jobs.status WHEN 'completed' THEN 'job.completed' "
        "ELSE 'job.failed' END, jobs.status, "
        "CASE jobs.status WHEN 'completed' THEN jobs.profile || ' completed' "
        "ELSE jobs.error END, '{}', jobs.finished_at "
        "FROM jobs WHERE jobs.status IN ('completed', 'failed') "
        "AND NOT EXISTS (SELECT 1 FROM job_events "
        "WHERE job_events.job_id = jobs.id "
        "AND job_events.event_type IN ('job.completed', 'job.failed'))"
    )
    connection.execute(
        "INSERT OR IGNORE INTO profile_sessions "
        "(project, profile, session_id, updated_at) "
        "SELECT project, 'studio', session_id, updated_at FROM sessions"
    )


def _migration_3_exact_active_clips(connection: sqlite3.Connection) -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows = connection.execute(
        "SELECT id, project, profile FROM jobs "
        "WHERE status IN ('queued', 'running') AND trim(clip_id) = ''"
    ).fetchall()
    for row in rows:
        connection.execute(
            "UPDATE jobs SET status = 'failed', error = ?, finished_at = ?, "
            "owner_id = '', pid = NULL, pid_start_time = NULL WHERE id = ?",
            (LEGACY_CLIP_ERROR, now, row["id"]),
        )
        connection.execute(
            "INSERT OR IGNORE INTO chat_events "
            "(project, job_id, role, content, created_at) "
            "VALUES (?, ?, 'system', ?, ?)",
            (row["project"], row["id"],
             f"Studio job failed: {LEGACY_CLIP_ERROR}", now),
        )
        connection.execute(
            "INSERT INTO job_events "
            "(project, job_id, profile, event_type, status, summary, detail, "
            "created_at) SELECT ?, ?, ?, 'job.failed', 'failed', ?, '{}', ? "
            "WHERE NOT EXISTS (SELECT 1 FROM job_events "
            "WHERE job_id = ? AND event_type = 'job.failed')",
            (row["project"], row["id"], row["profile"],
             LEGACY_CLIP_ERROR, now, row["id"]),
        )
    connection.executescript(
        """
        CREATE TRIGGER IF NOT EXISTS jobs_require_active_clip_on_insert
        BEFORE INSERT ON jobs
        WHEN NEW.status IN ('queued', 'running') AND trim(NEW.clip_id) = ''
        BEGIN
            SELECT RAISE(ABORT, 'active jobs require an exact clip binding');
        END;

        CREATE TRIGGER IF NOT EXISTS jobs_require_active_clip_on_update
        BEFORE UPDATE OF status, clip_id ON jobs
        WHEN NEW.status IN ('queued', 'running') AND trim(NEW.clip_id) = ''
        BEGIN
            SELECT RAISE(ABORT, 'active jobs require an exact clip binding');
        END;
        """
    )


Migration = Callable[[sqlite3.Connection], None]
MIGRATIONS: tuple[Migration, ...] = (
    _migration_1_job_columns,
    _migration_2_event_and_session_backfill,
    _migration_3_exact_active_clips,
)


def initialize_runtime_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version > CURRENT_SCHEMA_VERSION:
        raise RuntimeSchemaError(
            f"runtime schema {version} is newer than supported "
            f"version {CURRENT_SCHEMA_VERSION}")
    connection.executescript(BASE_SCHEMA)
    for target_version in range(version + 1, CURRENT_SCHEMA_VERSION + 1):
        MIGRATIONS[target_version - 1](connection)
        connection.execute(f"PRAGMA user_version = {target_version}")
    required = {"profile", "clip_id", "pid_start_time"}
    missing = required - _columns(connection, "jobs")
    if missing:
        raise RuntimeSchemaError(
            f"runtime jobs schema is missing columns: {sorted(missing)}")
    connection.commit()
