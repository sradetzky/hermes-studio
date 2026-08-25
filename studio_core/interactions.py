from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


MAX_QUESTIONS = 5
MAX_CHOICES = 4
MAX_TEXT_CHARS = 10_000
WIRE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


class InteractionContractError(ValueError):
    pass


class InteractionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    RESOLVED = "resolved"
    CLOSED = "closed"


@dataclass(frozen=True)
class InteractionQuestion:
    id: str
    question: str
    choices: tuple[str, ...]
    multi_select: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "choices": list(self.choices),
            "multi_select": self.multi_select,
        }


@dataclass(frozen=True)
class InteractionPayload:
    questions: tuple[InteractionQuestion, ...]
    batch: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch": self.batch,
            "questions": [question.to_dict() for question in self.questions],
        }


@dataclass(frozen=True)
class InteractionRequest:
    id: str
    revision: int
    job_id: str
    hermes_request_id: str
    hermes_session_id: str
    project: str
    clip_id: str
    chat_scope: str
    profile: str
    payload: InteractionPayload
    status: InteractionStatus
    answers: dict[str, str | list[str]] | None
    created_at: str
    answered_at: str
    resolved_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "revision": self.revision,
            "job_id": self.job_id,
            "project": self.project,
            "clip_id": self.clip_id,
            "chat_scope": self.chat_scope,
            "profile": self.profile,
            "status": self.status.value,
            "questions": [
                question.to_dict() for question in self.payload.questions
            ],
            "batch": self.payload.batch,
            "answers": self.answers,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
        }


def _text(value: object, label: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        raise InteractionContractError(f"{label} must be text")
    text = value.strip()
    if not text:
        raise InteractionContractError(f"{label} must not be empty")
    if len(text) > max_chars:
        raise InteractionContractError(f"{label} is too long")
    return text


def _wire_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not WIRE_ID_RE.fullmatch(value):
        raise InteractionContractError(f"{label} is invalid")
    return value


def _choices(value: object, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > MAX_CHOICES:
        raise InteractionContractError(
            f"{label} must contain at most {MAX_CHOICES} choices"
        )
    choices = tuple(_text(item, f"{label} choice", max_chars=500) for item in value)
    if len(set(choices)) != len(choices):
        raise InteractionContractError(f"{label} choices must be unique")
    return choices


def parse_gateway_clarify(payload: object) -> tuple[str, InteractionPayload]:
    if not isinstance(payload, dict):
        raise InteractionContractError("clarify payload must be an object")
    request_id = _wire_id(payload.get("request_id"), "clarify request id")
    batch_value = payload.get("questions")
    if batch_value:
        if not isinstance(batch_value, list) or len(batch_value) > MAX_QUESTIONS:
            raise InteractionContractError(
                f"clarify batch must contain at most {MAX_QUESTIONS} questions"
            )
        questions: list[InteractionQuestion] = []
        for index, item in enumerate(batch_value):
            if not isinstance(item, dict):
                raise InteractionContractError(
                    f"clarify question {index + 1} must be an object"
                )
            choices = _choices(item.get("choices"), f"clarify question {index + 1}")
            questions.append(InteractionQuestion(
                id=_wire_id(item.get("qid"), f"clarify question {index + 1} id"),
                question=_text(
                    item.get("question"), f"clarify question {index + 1}"
                ),
                choices=choices,
                multi_select=bool(item.get("multi_select")) and bool(choices),
            ))
        if len({question.id for question in questions}) != len(questions):
            raise InteractionContractError("clarify question ids must be unique")
        return request_id, InteractionPayload(tuple(questions), batch=True)

    choices = _choices(payload.get("choices"), "clarify question")
    question = InteractionQuestion(
        id="q0",
        question=_text(payload.get("question"), "clarify question"),
        choices=choices,
        multi_select=bool(payload.get("multi_select")) and bool(choices),
    )
    return request_id, InteractionPayload((question,), batch=False)


def parse_interaction_payload(value: object) -> InteractionPayload:
    if not isinstance(value, dict) or set(value) != {"batch", "questions"}:
        raise InteractionContractError("persisted interaction payload is invalid")
    questions_value = value.get("questions")
    if not isinstance(questions_value, list) or not questions_value:
        raise InteractionContractError("persisted interaction questions are invalid")
    if len(questions_value) > MAX_QUESTIONS:
        raise InteractionContractError("persisted interaction has too many questions")
    questions = []
    for index, item in enumerate(questions_value):
        if not isinstance(item, dict) or set(item) != {
            "id", "question", "choices", "multi_select"
        }:
            raise InteractionContractError("persisted interaction question is invalid")
        choices = _choices(item["choices"], f"interaction question {index + 1}")
        if not isinstance(item["multi_select"], bool):
            raise InteractionContractError("interaction multi-select flag is invalid")
        questions.append(InteractionQuestion(
            id=_wire_id(item["id"], f"interaction question {index + 1} id"),
            question=_text(item["question"], f"interaction question {index + 1}"),
            choices=choices,
            multi_select=item["multi_select"] and bool(choices),
        ))
    if len({question.id for question in questions}) != len(questions):
        raise InteractionContractError("interaction question ids must be unique")
    batch = value["batch"]
    if not isinstance(batch, bool):
        raise InteractionContractError("interaction batch flag is invalid")
    return InteractionPayload(tuple(questions), batch)


def parse_persisted_answers(
    value: str | None,
    payload: InteractionPayload,
) -> dict[str, str | list[str]] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise InteractionContractError("persisted interaction answer is invalid") from exc
    return validate_interaction_answers(payload, decoded)


def validate_interaction_answers(
    payload: InteractionPayload,
    answers: object,
) -> dict[str, str | list[str]]:
    if not isinstance(answers, dict):
        raise InteractionContractError("interaction answers must be an object")
    expected = {question.id for question in payload.questions}
    if set(answers) != expected:
        raise InteractionContractError("answer every interaction question exactly once")
    normalized: dict[str, str | list[str]] = {}
    for question in payload.questions:
        answer = answers[question.id]
        if question.multi_select:
            if not isinstance(answer, list) or not answer:
                raise InteractionContractError(
                    f"answer for {question.id} must be a non-empty list"
                )
            values = [
                _text(item, f"answer for {question.id}") for item in answer
            ]
            custom = [value for value in values if value not in question.choices]
            if (
                len(values) > len(question.choices) + 1
                or len(set(values)) != len(values)
                or len(custom) > 1
            ):
                raise InteractionContractError(
                    f"answer for {question.id} has invalid selections"
                )
            normalized[question.id] = values
        else:
            if isinstance(answer, list):
                raise InteractionContractError(
                    f"answer for {question.id} must be text"
                )
            normalized[question.id] = _text(answer, f"answer for {question.id}")
    return normalized


def gateway_answer(value: str | list[str]) -> str:
    if isinstance(value, list):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return value
