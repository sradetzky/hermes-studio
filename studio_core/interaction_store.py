from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from studio_core.identifiers import validate_clip_id
from studio_core.interactions import (
    InteractionContractError,
    InteractionPayload,
    InteractionRequest,
    InteractionStatus,
    parse_gateway_clarify,
    parse_interaction_payload,
    parse_persisted_answers,
    validate_interaction_answers,
)
from studio_core.job_contracts import (
    ChatScope,
    JobEventType,
    JobKind,
    JobPhase,
)


class InteractionStoreError(RuntimeError):
    pass


class InteractionNotFoundError(InteractionStoreError):
    pass


class InteractionConflictError(InteractionStoreError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def close_job_interactions(
    connection: sqlite3.Connection,
    job_id: str,
    resolved_at: str,
) -> None:
    connection.execute(
        "UPDATE interaction_requests SET status = 'closed', revision = revision + 1, "
        "resolved_at = ? WHERE job_id = ? AND status IN ('pending', 'answered')",
        (resolved_at, job_id),
    )


class InteractionStore:
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

    @staticmethod
    def _from_row(row: sqlite3.Row) -> InteractionRequest:
        try:
            payload = parse_interaction_payload(json.loads(row["payload"]))
            status = InteractionStatus(row["status"])
            answers = parse_persisted_answers(row["answer"], payload)
        except (
            InteractionContractError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            raise InteractionStoreError(
                f"persisted interaction {row['id']} is invalid"
            ) from exc
        if (status is InteractionStatus.PENDING) != (answers is None):
            if status is not InteractionStatus.CLOSED or answers is not None:
                raise InteractionStoreError(
                    f"persisted interaction {row['id']} has invalid answer state"
                )
        return InteractionRequest(
            id=row["id"],
            revision=row["revision"],
            job_id=row["job_id"],
            hermes_request_id=row["hermes_request_id"],
            hermes_session_id=row["hermes_session_id"],
            project=row["project"],
            clip_id=row["clip_id"],
            chat_scope=row["chat_scope"],
            profile=row["profile"],
            payload=payload,
            status=status,
            answers=answers,
            created_at=row["created_at"],
            answered_at=row["answered_at"],
            resolved_at=row["resolved_at"],
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        event_type: JobEventType,
        phase: JobPhase,
        summary: str,
        detail: dict | None = None,
        *,
        created_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO job_events "
            "(project, job_id, profile, event_type, status, summary, detail, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row["project"], row["id"], row["profile"], event_type.value,
                phase.value, summary,
                json.dumps(detail or {}, ensure_ascii=False), created_at,
            ),
        )

    @staticmethod
    def _scope(scope: ChatScope | str, clip_id: str) -> tuple[ChatScope, str]:
        try:
            scope = ChatScope(scope)
        except ValueError as exc:
            raise InteractionStoreError("interaction scope is invalid") from exc
        if scope is ChatScope.PROJECT:
            if clip_id:
                raise InteractionStoreError("project interaction may not bind a clip")
            return scope, ""
        try:
            return scope, validate_clip_id(clip_id)
        except ValueError as exc:
            raise InteractionStoreError(str(exc)) from exc

    def create(
        self,
        job_id: str,
        hermes_session_id: str,
        gateway_payload: object,
    ) -> InteractionRequest:
        try:
            hermes_request_id, payload = parse_gateway_clarify(gateway_payload)
        except InteractionContractError as exc:
            raise InteractionStoreError(str(exc)) from exc
        if (
            not isinstance(hermes_session_id, str)
            or not hermes_session_id.strip()
            or len(hermes_session_id) > 128
        ):
            raise InteractionStoreError("Hermes session id is invalid")
        payload_json = json.dumps(
            payload.to_dict(), ensure_ascii=False, separators=(",", ":")
        )
        now = utc_now()
        with self._transaction() as connection:
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            if not job:
                raise InteractionNotFoundError("job not found")
            if job["status"] != "running" or job["kind"] != JobKind.CHAT.value:
                raise InteractionConflictError(
                    "only a running chat job may request an interaction"
                )
            existing = connection.execute(
                "SELECT * FROM interaction_requests "
                "WHERE job_id = ? AND hermes_request_id = ?",
                (job_id, hermes_request_id),
            ).fetchone()
            if existing:
                request = self._from_row(existing)
                if (
                    request.hermes_session_id == hermes_session_id
                    and request.payload == payload
                    and request.status in {
                        InteractionStatus.PENDING,
                        InteractionStatus.ANSWERED,
                    }
                ):
                    return request
                raise InteractionConflictError("clarify request identity was reused")
            interaction_id = uuid.uuid4().hex
            try:
                connection.execute(
                    "INSERT INTO interaction_requests "
                    "(id, revision, job_id, hermes_request_id, hermes_session_id, "
                    "project, clip_id, chat_scope, profile, payload, status, answer, "
                    "created_at, answered_at, resolved_at) "
                    "VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, '', '')",
                    (
                        interaction_id, job_id, hermes_request_id,
                        hermes_session_id, job["project"], job["clip_id"],
                        job["chat_scope"], job["profile"], payload_json, now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InteractionConflictError(
                    "job already has a pending interaction"
                ) from exc
            self._append_event(
                connection, job, JobEventType.INTERACTION_REQUESTED,
                JobPhase.WAITING_FOR_USER, "Waiting for your answer",
                {"interaction_id": interaction_id, "question_count": len(payload.questions)},
                created_at=now,
            )
            row = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            return self._from_row(row)

    def get(self, interaction_id: str) -> InteractionRequest:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ?",
                (interaction_id,),
            ).fetchone()
        if not row:
            raise InteractionNotFoundError("interaction not found")
        return self._from_row(row)

    def open_for_scope(
        self,
        project: str,
        scope: ChatScope | str,
        clip_id: str = "",
    ) -> InteractionRequest | None:
        scope, clip_id = self._scope(scope, clip_id)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT interaction_requests.* FROM interaction_requests "
                "JOIN jobs ON jobs.id = interaction_requests.job_id "
                "WHERE interaction_requests.project = ? "
                "AND interaction_requests.chat_scope = ? "
                "AND interaction_requests.clip_id = ? "
                "AND interaction_requests.status IN ('pending', 'answered') "
                "AND jobs.status = 'running' "
                "ORDER BY interaction_requests.created_at DESC LIMIT 1",
                (project, scope.value, clip_id),
            ).fetchone()
        return self._from_row(row) if row else None

    def answer(
        self,
        interaction_id: str,
        revision: int,
        project: str,
        scope: ChatScope | str,
        clip_id: str,
        answers: object,
    ) -> InteractionRequest:
        scope, clip_id = self._scope(scope, clip_id)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT interaction_requests.*, jobs.status AS job_status "
                "FROM interaction_requests JOIN jobs "
                "ON jobs.id = interaction_requests.job_id "
                "WHERE interaction_requests.id = ?",
                (interaction_id,),
            ).fetchone()
            if not row or (
                row["project"] != project
                or row["chat_scope"] != scope.value
                or row["clip_id"] != clip_id
            ):
                raise InteractionNotFoundError("interaction not found in this scope")
            request = self._from_row(row)
            if row["job_status"] != "running":
                raise InteractionConflictError("interaction job is no longer running")
            if request.status is not InteractionStatus.PENDING:
                raise InteractionConflictError("interaction was already answered")
            if not isinstance(revision, int) or isinstance(revision, bool):
                raise InteractionConflictError("interaction revision is invalid")
            if request.revision != revision:
                raise InteractionConflictError("interaction revision is stale")
            try:
                normalized = validate_interaction_answers(request.payload, answers)
            except InteractionContractError as exc:
                raise InteractionStoreError(str(exc)) from exc
            now = utc_now()
            cursor = connection.execute(
                "UPDATE interaction_requests SET status = 'answered', "
                "revision = revision + 1, answer = ?, answered_at = ? "
                "WHERE id = ? AND status = 'pending' AND revision = ?",
                (
                    json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
                    now, interaction_id, revision,
                ),
            )
            if cursor.rowcount != 1:
                raise InteractionConflictError("interaction answer lost its revision")
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (request.job_id,)
            ).fetchone()
            self._append_event(
                connection, job, JobEventType.INTERACTION_ANSWERED,
                JobPhase.WAITING_FOR_USER, "Answer received",
                {"interaction_id": interaction_id}, created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            return self._from_row(updated)

    def answered(self, interaction_id: str, job_id: str) -> InteractionRequest | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ? AND job_id = ?",
                (interaction_id, job_id),
            ).fetchone()
        if not row:
            raise InteractionNotFoundError("interaction not found for this job")
        request = self._from_row(row)
        if request.status is InteractionStatus.ANSWERED:
            return request
        if request.status is InteractionStatus.PENDING:
            return None
        raise InteractionConflictError("interaction is no longer answerable")

    def resolve(self, interaction_id: str, job_id: str) -> InteractionRequest:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ? AND job_id = ?",
                (interaction_id, job_id),
            ).fetchone()
            if not row:
                raise InteractionNotFoundError("interaction not found for this job")
            request = self._from_row(row)
            if request.status is not InteractionStatus.ANSWERED:
                raise InteractionConflictError("interaction is not ready to resume")
            cursor = connection.execute(
                "UPDATE interaction_requests SET status = 'resolved', "
                "revision = revision + 1, resolved_at = ? "
                "WHERE id = ? AND status = 'answered' AND revision = ?",
                (now, interaction_id, request.revision),
            )
            if cursor.rowcount != 1:
                raise InteractionConflictError("interaction resolution lost its revision")
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            self._append_event(
                connection, job, JobEventType.INTERACTION_RESUMED,
                JobPhase.RUNNING, "Studio resumed",
                {"interaction_id": interaction_id}, created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            return self._from_row(updated)

    def expire(
        self,
        interaction_id: str,
        job_id: str,
        hermes_request_id: str,
    ) -> InteractionRequest:
        now = utc_now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_requests "
                "WHERE id = ? AND job_id = ? AND hermes_request_id = ?",
                (interaction_id, job_id, hermes_request_id),
            ).fetchone()
            if not row:
                raise InteractionNotFoundError(
                    "interaction not found for this Hermes request")
            request = self._from_row(row)
            if request.status not in {
                InteractionStatus.PENDING,
                InteractionStatus.ANSWERED,
            }:
                raise InteractionConflictError("interaction is no longer open")
            cursor = connection.execute(
                "UPDATE interaction_requests SET status = 'closed', "
                "revision = revision + 1, resolved_at = ? "
                "WHERE id = ? AND revision = ? "
                "AND status IN ('pending', 'answered')",
                (now, interaction_id, request.revision),
            )
            if cursor.rowcount != 1:
                raise InteractionConflictError(
                    "interaction expiration lost its revision")
            job = connection.execute(
                "SELECT * FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            self._append_event(
                connection, job, JobEventType.INTERACTION_EXPIRED,
                JobPhase.FAILED,
                "Clarification expired before an answer was delivered",
                {"interaction_id": interaction_id}, created_at=now,
            )
            updated = connection.execute(
                "SELECT * FROM interaction_requests WHERE id = ?",
                (interaction_id,),
            ).fetchone()
            return self._from_row(updated)
