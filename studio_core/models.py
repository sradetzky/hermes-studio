from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any, Literal

from studio_core.job_contracts import (
    ChatScope,
    JobEventType,
    JobKind,
    JobPayload,
    JobPhase,
)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


ChatRole = Literal["user", "assistant", "system"]


@dataclass(frozen=True)
class Job:
    id: str
    project: str
    clip_id: str
    chat_scope: ChatScope
    kind: JobKind
    profile: str
    status: JobStatus
    message: str
    error: str
    created_at: str
    started_at: str
    finished_at: str
    owner_id: str
    pid: int | None
    pid_start_time: int | None
    payload: JobPayload
    reply: str = ""

    def to_dict(self) -> dict:
        result = asdict(self)
        result.pop("payload")
        result["chat_scope"] = self.chat_scope.value
        result["kind"] = self.kind.value
        result["status"] = self.status.value
        return result


@dataclass(frozen=True)
class ChatEvent:
    id: int
    project: str
    clip_id: str
    job_id: str | None
    role: ChatRole
    content: str
    created_at: str
    profile: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "clip_id": self.clip_id,
            "role": self.role,
            "content": self.content,
            "ts": self.created_at,
            "job_id": self.job_id,
            "profile": self.profile,
        }


@dataclass(frozen=True)
class JobEvent:
    id: int
    project: str
    job_id: str
    profile: str
    event_type: JobEventType
    phase: JobPhase
    summary: str
    detail: dict[str, Any]
    created_at: str

    @property
    def status(self) -> str:
        """Compatibility alias for the existing HTTP event field."""
        return self.phase.value

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project": self.project,
            "job_id": self.job_id,
            "profile": self.profile,
            "event_type": self.event_type.value,
            "status": self.phase.value,
            "summary": self.summary,
            "detail": self.detail,
            "ts": self.created_at,
        }
