from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

from studio_core.generation_archive import parse_generation_job_payload
from studio_core.identifiers import validate_clip_id


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
    contract: dict


@dataclass(frozen=True)
class MovieExportJobPayload:
    contract: dict


JobPayload: TypeAlias = ChatJobPayload | GenerationJobPayload | MovieExportJobPayload


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


def decode_job_payload(kind: JobKind, value: object) -> JobPayload:
    if not isinstance(value, str):
        raise JobContractError("job payload must be text")
    if kind is JobKind.CHAT:
        return ChatJobPayload(value)
    if kind is JobKind.GENERATE:
        try:
            return GenerationJobPayload(parse_generation_job_payload(value))
        except ValueError as exc:
            raise JobContractError(str(exc)) from exc
    try:
        contract = json.loads(value)
    except json.JSONDecodeError as exc:
        raise JobContractError("movie export payload is invalid") from exc
    if (
        not isinstance(contract, dict)
        or set(contract) != {
            "schema_version", "action", "sources", "assembly", "output"}
        or contract.get("action") != "export-selected-takes"
        or contract.get("schema_version") != 1
        or not isinstance(contract.get("sources"), list)
        or not contract["sources"]
        or not isinstance(contract.get("assembly"), dict)
        or not isinstance(contract.get("output"), dict)
        or set(contract["assembly"]) != {"mode", "hard_cuts", "target"}
        or contract["assembly"].get("mode") not in {
            "stream-copy", "normalized"}
        or contract["assembly"].get("hard_cuts") is not True
        or not isinstance(contract["assembly"].get("target"), dict)
        or set(contract["output"]) != {"id", "filename", "provenance"}
        or any(not isinstance(contract["output"].get(key), str)
               or not contract["output"][key]
               for key in ("id", "filename", "provenance"))
    ):
        raise JobContractError("movie export payload is invalid")
    source_keys = {
        "clip_id", "clip_title", "generation", "filename", "size",
        "sha256", "probe",
    }
    if any(
        not isinstance(source, dict)
        or set(source) != source_keys
        or not isinstance(source.get("clip_id"), str)
        or not source["clip_id"]
        or not isinstance(source.get("clip_title"), str)
        or not source["clip_title"]
        or not isinstance(source.get("generation"), str)
        or not source["generation"]
        or not isinstance(source.get("filename"), str)
        or not source["filename"]
        or not isinstance(source.get("size"), int)
        or source["size"] <= 0
        or not isinstance(source.get("sha256"), str)
        or not isinstance(source.get("probe"), dict)
        for source in contract["sources"]
    ):
        raise JobContractError("movie export payload is invalid")
    return MovieExportJobPayload(contract)
