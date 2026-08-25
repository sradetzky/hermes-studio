from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


CONTRACT_SCHEMA_VERSION = 1
SETTINGS_SCHEMA_VERSION = 2
MODES = {"t2va", "i2va", "fl2va", "r2v"}
ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "21:9": 21 / 9,
}
IMAGE_REFERENCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
CANVAS_MULTIPLE = 32
MAX_CANVAS_PIXELS = 1_100_000
MAX_SAFE_SEED = 9_007_199_254_740_991
FPS = 24


def executed_generation_prompt(prompt: str) -> str:
    """Return the exact prompt normalization applied by the H3 graph builder."""
    return prompt.strip()


def executed_generation_prompt_sha256(prompt: str) -> str:
    return hashlib.sha256(
        executed_generation_prompt(prompt).encode("utf-8")).hexdigest()


class GenerationContractError(ValueError):
    pass


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GenerationContractError(f"generation {field} must be an integer")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenerationContractError(f"generation {field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise GenerationContractError(f"generation {field} must be finite")
    return result


def _exact_dict(value: Any, keys: set[str], message: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise GenerationContractError(message)
    return value


def _timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise GenerationContractError(f"generation {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise GenerationContractError(f"generation {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise GenerationContractError(f"generation {field} must include a timezone")
    return value


def _reference(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or Path(value).name != value
        or Path(value).suffix.lower() not in IMAGE_REFERENCE_EXTENSIONS
    ):
        raise GenerationContractError("generation reference must be a safe image filename")
    return value


def resolution_from_mp(aspect: str, mp: float) -> tuple[int, int]:
    ratio = ASPECT_RATIOS[aspect]
    pixels = mp * 1_000_000.0
    height = (pixels / ratio) ** 0.5
    width = ratio * height
    return (
        max(CANVAS_MULTIPLE, int(round(width / CANVAS_MULTIPLE)) * CANVAS_MULTIPLE),
        max(CANVAS_MULTIPLE, int(round(height / CANVAS_MULTIPLE)) * CANVAS_MULTIPLE),
    )


def duration_to_frames(seconds: float) -> int:
    frames = max(5, int(round(seconds * FPS)))
    while frames % 17 != 5:
        frames += 1
    return frames


@dataclass(frozen=True)
class GenerationSettingsContract:
    schema_version: int
    prompt_sha256: str
    updated_at: str
    mode: str
    aspect: str
    mp: float
    width: int | None
    height: int | None
    seed: int | None
    steps: int
    accel: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "prompt_sha256": self.prompt_sha256,
            "updated_at": self.updated_at,
            "mode": self.mode,
            "aspect": self.aspect,
            "mp": self.mp,
            "width": self.width,
            "height": self.height,
            "seed": self.seed,
            "steps": self.steps,
            "accel": self.accel,
        }


@dataclass(frozen=True)
class GenerationResolutionContract:
    mode: str
    width: int
    height: int
    megapixels: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "width": self.width,
            "height": self.height,
            "megapixels": self.megapixels,
        }


@dataclass(frozen=True)
class GenerationTimingContract:
    requested_seconds: float
    frames: int
    actual_seconds: float
    fps: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_seconds": self.requested_seconds,
            "frames": self.frames,
            "actual_seconds": self.actual_seconds,
            "fps": self.fps,
        }


@dataclass(frozen=True)
class GenerationExecutionContract:
    resolution: GenerationResolutionContract
    timing: GenerationTimingContract
    references: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "resolution": self.resolution.to_dict(),
            "timing": self.timing.to_dict(),
            "references": list(self.references),
        }


@dataclass(frozen=True)
class GenerationContract:
    schema_version: int
    action: str
    prompt: str
    prompt_sha256: str
    settings_updated_at: str
    settings_manifest: GenerationSettingsContract
    execution: GenerationExecutionContract
    expected_generation_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "prompt": self.prompt,
            "prompt_sha256": self.prompt_sha256,
            "settings_updated_at": self.settings_updated_at,
            "settings_manifest": self.settings_manifest.to_dict(),
            "execution": self.execution.to_dict(),
            "expected_generation_id": self.expected_generation_id,
        }


def _parse_settings(value: Any, prompt_sha256: str, updated_at: str) -> GenerationSettingsContract:
    settings = _exact_dict(
        value,
        {
            "schema_version", "prompt_sha256", "updated_at", "mode", "aspect",
            "mp", "width", "height", "seed", "steps", "accel",
        },
        "generation settings snapshot is invalid",
    )
    mode = settings["mode"]
    aspect = settings["aspect"]
    mp = _number(settings["mp"], "megapixels")
    width = settings["width"]
    height = settings["height"]
    seed = settings["seed"]
    steps = _integer(settings["steps"], "steps")
    if (
        settings["schema_version"] != SETTINGS_SCHEMA_VERSION
        or settings["prompt_sha256"] != prompt_sha256
        or settings["updated_at"] != updated_at
        or mode not in MODES
        or aspect not in ASPECT_RATIOS
        or not 0.1 <= mp <= 1.1
        or (width is None) != (height is None)
        or not isinstance(settings["accel"], bool)
        or not 1 <= steps <= 50
    ):
        raise GenerationContractError("generation settings snapshot is invalid")
    if width is not None:
        width = _integer(width, "width")
        height = _integer(height, "height")
        if (
            width < 256
            or height < 256
            or width % CANVAS_MULTIPLE
            or height % CANVAS_MULTIPLE
            or width * height > MAX_CANVAS_PIXELS
        ):
            raise GenerationContractError("generation settings canvas is invalid")
    if seed is not None:
        seed = _integer(seed, "seed")
        if not 0 <= seed <= MAX_SAFE_SEED:
            raise GenerationContractError("generation seed is invalid")
    return GenerationSettingsContract(
        schema_version=SETTINGS_SCHEMA_VERSION,
        prompt_sha256=prompt_sha256,
        updated_at=updated_at,
        mode=mode,
        aspect=aspect,
        mp=mp,
        width=width,
        height=height,
        seed=seed,
        steps=steps,
        accel=settings["accel"],
    )


def _parse_resolution(value: Any, settings: GenerationSettingsContract) -> GenerationResolutionContract:
    resolution = _exact_dict(
        value,
        {"mode", "width", "height", "megapixels"},
        "generation resolution snapshot is invalid",
    )
    width = _integer(resolution["width"], "resolution width")
    height = _integer(resolution["height"], "resolution height")
    megapixels = _number(resolution["megapixels"], "resolution megapixels")
    expected_mode = "explicit" if settings.width is not None else "mp"
    expected_size = (
        (settings.width, settings.height)
        if settings.width is not None
        else resolution_from_mp(settings.aspect, settings.mp)
    )
    expected_mp = round(width * height / 1_000_000, 3)
    if (
        resolution["mode"] != expected_mode
        or (width, height) != expected_size
        or width < CANVAS_MULTIPLE
        or height < CANVAS_MULTIPLE
        or width % CANVAS_MULTIPLE
        or height % CANVAS_MULTIPLE
        or width * height > MAX_CANVAS_PIXELS
        or megapixels != expected_mp
    ):
        raise GenerationContractError("generation resolution snapshot is invalid")
    return GenerationResolutionContract(expected_mode, width, height, megapixels)


def _parse_timing(value: Any) -> GenerationTimingContract:
    timing = _exact_dict(
        value,
        {"requested_seconds", "frames", "actual_seconds", "fps"},
        "generation timing snapshot is invalid",
    )
    requested = _number(timing["requested_seconds"], "requested seconds")
    frames = _integer(timing["frames"], "frames")
    actual = _number(timing["actual_seconds"], "actual seconds")
    fps = _integer(timing["fps"], "fps")
    if (
        not 4 <= requested <= 15
        or fps != FPS
        or frames != duration_to_frames(requested)
        or actual != round(frames / FPS, 3)
    ):
        raise GenerationContractError("generation timing snapshot is invalid")
    return GenerationTimingContract(requested, frames, actual, fps)


def parse_generation_contract(value: Any) -> GenerationContract:
    payload = _exact_dict(
        value,
        {
            "schema_version", "action", "prompt", "prompt_sha256",
            "settings_updated_at", "settings_manifest", "execution",
            "expected_generation_id",
        },
        "generation request payload is invalid",
    )
    prompt = payload["prompt"]
    prompt_sha256 = payload["prompt_sha256"]
    updated_at = _timestamp(payload["settings_updated_at"], "settings revision")
    expected_generation_id = payload["expected_generation_id"]
    if (
        payload["schema_version"] != CONTRACT_SCHEMA_VERSION
        or payload["action"] != "generate-current-prompt"
        or not isinstance(prompt, str)
        or not executed_generation_prompt(prompt)
        or not isinstance(prompt_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", prompt_sha256) is None
        or hashlib.sha256(prompt.encode("utf-8")).hexdigest() != prompt_sha256
        or not isinstance(expected_generation_id, str)
        or re.fullmatch(r"[0-9]{3,}", expected_generation_id) is None
    ):
        raise GenerationContractError("generation request payload is invalid")
    settings = _parse_settings(payload["settings_manifest"], prompt_sha256, updated_at)
    execution = _exact_dict(
        payload["execution"],
        {"resolution", "timing", "references"},
        "generation execution snapshot is invalid",
    )
    references_value = execution["references"]
    if not isinstance(references_value, list):
        raise GenerationContractError("generation execution snapshot is invalid")
    references = tuple(_reference(item) for item in references_value)
    if len(references) != len(set(references)):
        raise GenerationContractError("generation references must be unique")
    expected_counts = {
        "t2va": {0},
        "i2va": {1},
        "fl2va": {2},
        "r2v": set(range(1, 10)),
    }
    if len(references) not in expected_counts[settings.mode]:
        raise GenerationContractError("generation reference count does not match mode")
    return GenerationContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        action="generate-current-prompt",
        prompt=prompt,
        prompt_sha256=prompt_sha256,
        settings_updated_at=updated_at,
        settings_manifest=settings,
        execution=GenerationExecutionContract(
            resolution=_parse_resolution(execution["resolution"], settings),
            timing=_parse_timing(execution["timing"]),
            references=references,
        ),
        expected_generation_id=expected_generation_id,
    )


def parse_generation_job_payload(value: str) -> GenerationContract:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as exc:
        raise GenerationContractError("generation request payload is invalid") from exc
    return parse_generation_contract(payload)
