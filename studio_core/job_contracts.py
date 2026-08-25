from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from studio_core.generation_contracts import (
    GenerationContract,
    parse_generation_job_payload,
)
from studio_core.identifiers import validate_clip_id
from studio_core.movie_contracts import MovieContract, parse_movie_contract


class JobContractError(ValueError):
    pass


class JobKind(StrEnum):
    CHAT = "chat"
    GENERATE = "generate"
    EXPORT_MOVIE = "export_movie"


class ChatScope(StrEnum):
    PROJECT = "project"
    CLIP = "clip"


class JobPhase(StrEnum):
    NONE = ""
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class JobEventType(StrEnum):
    JOB_QUEUED = "job.queued"
    JOB_STARTED = "job.started"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"
    JOB_PREPARE = "job.prepare"
    JOB_TIMEOUT = "job.timeout"
    JOB_RECOVERY_BLOCKED = "job.recovery_blocked"
    PROFILE_CONNECTED = "profile.connected"
    REASONING = "reasoning"
    COMMENTARY = "commentary"
    TOOL_STARTED = "tool.started"
    TOOL_COMPLETED = "tool.completed"
    HANDOFF_STARTED = "handoff.started"
    HANDOFF_COMPLETED = "handoff.completed"
    HANDOFF_FAILED = "handoff.failed"
    GENERATION_VALIDATION = "generation.validation"
    GENERATION_GRAPH = "generation.graph"
    GENERATION_SUBMIT = "generation.submit"
    GENERATION_WAIT = "generation.wait"
    GENERATION_ARCHIVE = "generation.archive"
    COMFYUI_CLEANUP = "comfyui.cleanup"
    MOVIE_EXPORT = "movie.export"


@dataclass(frozen=True)
class ChatJobPayload:
    message: str


@dataclass(frozen=True)
class GenerationJobPayload:
    contract: GenerationContract


@dataclass(frozen=True)
class LegacyGenerationJobPayload:
    """Validated pre-contract metadata retained for a terminal generation."""

    prompt_sha256: str
    settings_updated_at: str


@dataclass(frozen=True)
class MovieExportJobPayload:
    contract: MovieContract


JobPayload: TypeAlias = (
    ChatJobPayload
    | GenerationJobPayload
    | LegacyGenerationJobPayload
    | MovieExportJobPayload
)


def parse_job_kind(value: object) -> JobKind:
    try:
        return JobKind(value)
    except (TypeError, ValueError) as exc:
        raise JobContractError(f"invalid job kind: {value!r}") from exc


def parse_chat_scope(value: object) -> ChatScope:
    try:
        return ChatScope(value)
    except (TypeError, ValueError) as exc:
        raise JobContractError(f"invalid chat scope: {value!r}") from exc


def validate_job_binding(
    kind: JobKind,
    scope: ChatScope,
    clip_id: str,
) -> str:
    if not isinstance(clip_id, str):
        raise JobContractError("job clip binding is invalid")
    if clip_id:
        try:
            clip_id = validate_clip_id(clip_id)
        except ValueError as exc:
            raise JobContractError(str(exc)) from exc
    if scope is ChatScope.CLIP and not clip_id:
        raise JobContractError("clip jobs require an exact clip binding")
    if kind is JobKind.GENERATE and scope is not ChatScope.CLIP:
        raise JobContractError("generation jobs require clip scope")
    if kind is JobKind.EXPORT_MOVIE and (
        scope is not ChatScope.PROJECT or clip_id
    ):
        raise JobContractError("movie export jobs require project scope")
    if scope is ChatScope.PROJECT and kind not in {
        JobKind.CHAT,
        JobKind.EXPORT_MOVIE,
    }:
        raise JobContractError("project scope is invalid for this job kind")
    return clip_id


def _decode_legacy_terminal_generation_payload(
    value: str,
) -> LegacyGenerationJobPayload:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise JobContractError("generation request payload is invalid") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {
            "action", "prompt_sha256", "settings_updated_at"}
        or payload.get("action") != "generate-current-prompt"
        or not isinstance(payload.get("prompt_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", payload["prompt_sha256"]) is None
        or not isinstance(payload.get("settings_updated_at"), str)
        or not payload["settings_updated_at"]
    ):
        raise JobContractError("generation request payload is invalid")
    return LegacyGenerationJobPayload(
        prompt_sha256=payload["prompt_sha256"],
        settings_updated_at=payload["settings_updated_at"],
    )


def decode_job_payload(
    kind: JobKind,
    value: object,
    *,
    terminal: bool = False,
) -> JobPayload:
    if not isinstance(value, str):
        raise JobContractError("job payload must be text")
    if kind is JobKind.CHAT:
        return ChatJobPayload(value)
    if kind is JobKind.GENERATE:
        try:
            return GenerationJobPayload(parse_generation_job_payload(value))
        except ValueError as exc:
            if terminal:
                return _decode_legacy_terminal_generation_payload(value)
            raise JobContractError(str(exc)) from exc
    try:
        contract = json.loads(value)
        return MovieExportJobPayload(parse_movie_contract(contract))
    except (json.JSONDecodeError, ValueError) as exc:
        raise JobContractError(str(exc)) from exc
