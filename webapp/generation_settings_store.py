from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from webapp.config import Settings
from webapp.reference_store import ReferenceStore, ReferenceStoreError
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_write_bytes_at,
    open_directory,
    open_regular_file,
    read_opened_text,
)


MANIFEST_NAME = "current_generation.json"
SCHEMA_VERSION = 2
MODES = {"t2va", "i2va", "fl2va", "r2v"}
ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "21:9": 21 / 9,
}
CANVAS_MULTIPLE = 32
MAX_CANVAS_PIXELS = 1_100_000
MAX_SAFE_SEED = 9_007_199_254_740_991
FPS = 24
IMAGE_REFERENCE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}


class GenerationSettingsError(ValueError):
    pass


def _bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise GenerationSettingsError(f"{field} must be true or false")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise GenerationSettingsError(f"{field} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise GenerationSettingsError(f"{field} must be finite")
    return number


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GenerationSettingsError(f"{field} must be an integer")
    return value


def _seed(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        if not value.isascii() or not value.isdigit():
            raise GenerationSettingsError("seed must contain decimal digits only")
        value = int(value)
    seed = _integer(value, "seed")
    if seed < 0 or seed > MAX_SAFE_SEED:
        raise GenerationSettingsError(
            f"seed must be between 0 and {MAX_SAFE_SEED}")
    return seed


def _filename(value: Any, field: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise GenerationSettingsError(f"{field} must be a filename")
    name = value.strip()
    if not name and optional:
        return None
    if (not name or name.startswith(".") or "/" in name or "\\" in name
            or Path(name).name != name):
        raise GenerationSettingsError(f"invalid {field}: {value!r}")
    return name


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


class GenerationSettingsStore:
    def __init__(self, settings: Settings):
        self.app_settings = settings

    @staticmethod
    def defaults() -> dict:
        return {
            "mode": "t2va",
            "aspect": "16:9",
            "mp": 0.4,
            "width": None,
            "height": None,
            "seed": None,
            "steps": 20,
            "accel": False,
        }

    @staticmethod
    def _prompt(clip: Path) -> str:
        path = clip / "current_prompt.txt"
        try:
            with open_regular_file(path) as opened:
                return read_opened_text(opened)
        except FileNotFoundError:
            return ""
        except (SafeFilesystemError, OSError, UnicodeDecodeError) as exc:
            raise GenerationSettingsError("current prompt is unsafe") from exc

    @staticmethod
    def prompt_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

    @staticmethod
    def _prompt_inputs(prompt: str) -> tuple[float | None, list[str], list[str]]:
        """Extract prompt-owned duration and ordered Picture filenames."""
        durations = []
        for match in re.finditer(
                r"\b(\d{1,2}(?:\.\d+)?)\s*(?:-|\s)seconds?\b",
                prompt, flags=re.IGNORECASE):
            value = float(match.group(1))
            if value >= 4:
                durations.append(value)
        for match in re.finditer(r"\b\d{1,2}:(\d{2})(?:\.(\d{1,3}))?\b", prompt):
            milliseconds = (match.group(2) or "0").ljust(3, "0")
            value = float(match.group(1)) + int(milliseconds) / 1000
            if value >= 4:
                durations.append(value)

        errors = []
        duration = max(durations) if durations else None
        if duration is None:
            errors.append("Prompt must state a 4–15 second length")
        elif duration > 15:
            errors.append("Prompt length exceeds the 15-second H3 limit")

        numbered: dict[int, str] = {}
        picture_pattern = re.compile(
            r"<Picture\s+(\d+)>\s*\(([^()\r\n]+\.(?:png|jpe?g|webp))\)",
            flags=re.IGNORECASE,
        )
        for match in picture_pattern.finditer(prompt):
            number = int(match.group(1))
            try:
                filename = _filename(match.group(2).strip(), "prompt reference")
            except GenerationSettingsError as exc:
                errors.append(str(exc))
                continue
            assert filename is not None
            previous = numbered.get(number)
            if previous is not None and previous != filename:
                errors.append(f"Prompt assigns multiple files to <Picture {number}>")
            else:
                numbered[number] = filename
        if numbered and sorted(numbered) != list(range(1, max(numbered) + 1)):
            errors.append("Prompt Picture references must be numbered consecutively from 1")
        references = [numbered[number] for number in sorted(numbered)]
        return duration, references, errors

    def normalize(self, payload: dict) -> dict:
        expected = set(self.defaults())
        unknown = set(payload) - expected
        missing = expected - set(payload)
        if unknown:
            raise GenerationSettingsError(
                f"unknown generation setting: {sorted(unknown)[0]}")
        if missing:
            raise GenerationSettingsError(
                f"missing generation setting: {sorted(missing)[0]}")

        mode = str(payload["mode"]).lower()
        if mode not in MODES:
            raise GenerationSettingsError(
                f"mode must be one of: {', '.join(sorted(MODES))}")
        aspect = str(payload["aspect"])
        if aspect not in ASPECT_RATIOS:
            raise GenerationSettingsError(
                f"aspect must be one of: {', '.join(ASPECT_RATIOS)}")
        mp = _number(payload["mp"], "mp")
        if mp < 0.1 or mp > 1.1:
            raise GenerationSettingsError("mp must be between 0.1 and 1.1")

        width_value = payload["width"]
        height_value = payload["height"]
        if (width_value is None) != (height_value is None):
            raise GenerationSettingsError(
                "width and height must both be set or both be null")
        width = height = None
        if width_value is not None:
            width = _integer(width_value, "width")
            height = _integer(height_value, "height")
            if (width < 256 or height < 256 or width % 32 or height % 32):
                raise GenerationSettingsError(
                    "explicit width and height must be at least 256 and multiples of 32")
            if width * height > MAX_CANVAS_PIXELS:
                raise GenerationSettingsError(
                    "explicit canvas exceeds the safe ~1.1MP ceiling")

        seed = _seed(payload["seed"])
        steps = _integer(payload["steps"], "steps")
        if steps < 1 or steps > 50:
            raise GenerationSettingsError("steps must be between 1 and 50")
        return {
            "mode": mode,
            "aspect": aspect,
            "mp": round(mp, 3),
            "width": width,
            "height": height,
            "seed": seed,
            "steps": steps,
            "accel": _bool(payload["accel"], "accel"),
        }

    @staticmethod
    def _manifest_path(clip: Path) -> Path:
        return clip / MANIFEST_NAME

    def _read_manifest(self, clip: Path) -> tuple[dict | None, str | None]:
        path = self._manifest_path(clip)
        try:
            with open_regular_file(path) as opened:
                value = json.loads(read_opened_text(opened))
        except FileNotFoundError:
            return None, None
        except (SafeFilesystemError, OSError):
            return None, "generation settings manifest is not a regular file"
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None, "generation settings manifest is invalid"
        if not isinstance(value, dict):
            return None, "generation settings manifest is invalid"
        try:
            normalized = self.normalize({
                key: value.get(key) for key in self.defaults()
            })
        except GenerationSettingsError as exc:
            return None, str(exc)
        return {
            **normalized,
            "schema_version": value.get("schema_version"),
            "prompt_sha256": value.get("prompt_sha256"),
            "updated_at": value.get("updated_at"),
        }, None

    @staticmethod
    def _reference_names(project: Path) -> list[str]:
        try:
            return [
                name for name in ReferenceStore.list_references(project)
                if Path(name).suffix.lower() in IMAGE_REFERENCE_EXTENSIONS
            ]
        except ReferenceStoreError as exc:
            raise GenerationSettingsError(
                "references directory is unsafe") from exc

    def readiness(self, project: Path, clip: Path, manifest: dict | None,
                  manifest_error: str | None = None) -> dict:
        prompt = self._prompt(clip)
        duration, references, prompt_errors = self._prompt_inputs(prompt)
        reasons = []
        warnings = []
        if not prompt.strip():
            reasons.append("Current prompt is empty")
        else:
            reasons.extend(prompt_errors)
        if manifest_error:
            reasons.append(manifest_error)
        elif manifest is None:
            reasons.append("Generation settings have not been saved")
        else:
            if manifest.get("schema_version") not in {1, SCHEMA_VERSION}:
                reasons.append("Generation settings schema is unsupported")
            if manifest.get("prompt_sha256") != self.prompt_hash(prompt):
                reasons.append("Current prompt changed after settings were saved")
            available = set(self._reference_names(project))
            missing = [name for name in references if name not in available]
            if missing:
                reasons.append(
                    f"Missing reference: {missing[0]}")
            mode = manifest["mode"]
            if mode == "t2va" and references:
                reasons.append("T2VA must not include reference images")
            elif mode == "i2va" and len(references) != 1:
                reasons.append("I2VA requires exactly one opening-frame image")
            elif mode == "fl2va" and len(references) != 2:
                reasons.append("FL2VA requires exactly two images: first then last")
            elif mode == "r2v" and not 1 <= len(references) <= 9:
                reasons.append(
                    "R2V prompt requires 1–9 ordered <Picture N> (filename.ext) references")

        settings = manifest or self.defaults()
        if settings["width"] is not None:
            width, height = settings["width"], settings["height"]
            size_mode = "explicit"
        else:
            width, height = resolution_from_mp(
                settings["aspect"], settings["mp"])
            size_mode = "mp"
        frames = duration_to_frames(duration) if duration is not None else None
        status = "ready" if not reasons else (
            "empty-prompt" if not prompt.strip() else
            "not-configured" if manifest is None and not manifest_error else
            "stale" if any("changed after" in reason for reason in reasons) else
            "blocked")
        return {
            "ready": not reasons,
            "status": status,
            "reasons": reasons,
            "warnings": warnings,
            "resolution": {
                "mode": size_mode,
                "width": width,
                "height": height,
                "megapixels": round(width * height / 1_000_000, 3),
            },
            "timing": {
                "requested_seconds": duration,
                "frames": frames,
                "actual_seconds": round(frames / FPS, 3) if frames else None,
                "fps": FPS,
            },
            "references": references,
        }

    def describe(self, project: Path, clip: Path,
                 include_options: bool = False) -> dict:
        manifest, error = self._read_manifest(clip)
        settings = {
            key: manifest[key] for key in self.defaults()
        } if manifest else self.defaults()
        result = {
            "exists": manifest is not None,
            "settings": settings,
            "manifest": ({
                "schema_version": manifest.get("schema_version"),
                "prompt_sha256": manifest.get("prompt_sha256"),
                "updated_at": manifest.get("updated_at"),
            } if manifest else None),
            "readiness": self.readiness(project, clip, manifest, error),
        }
        if result["settings"]["seed"] is not None:
            result["settings"]["seed"] = str(result["settings"]["seed"])
        if include_options:
            result["options"] = {
                "modes": sorted(MODES),
                "aspects": list(ASPECT_RATIOS),
                "max_seed": str(MAX_SAFE_SEED),
            }
        return result

    def validate_generation_request(
            self, project: Path, clip: Path, prompt_sha256: str,
            settings_updated_at: str) -> dict:
        contract = self.describe(project, clip)
        manifest = contract["manifest"]
        if manifest is None:
            reason = contract["readiness"]["reasons"][0]
            raise GenerationSettingsError(reason)
        if (manifest["prompt_sha256"] != prompt_sha256
                or manifest["updated_at"] != settings_updated_at):
            raise GenerationSettingsError(
                "Generation request is stale; refresh the current prompt settings")
        if not contract["readiness"]["ready"]:
            reason = contract["readiness"]["reasons"][0]
            raise GenerationSettingsError(reason)
        return contract

    def save(self, project: Path, clip: Path, payload: dict) -> dict:
        normalized = self.normalize(payload)
        prompt = self._prompt(clip)
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "prompt_sha256": self.prompt_hash(prompt),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **normalized,
        }
        target = self._manifest_path(clip)
        data = (json.dumps(
            manifest, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with open_directory(clip) as clip_fd:
                lock_fd = os.open(
                    ".generation-settings.lock",
                    os.O_RDWR | os.O_CREAT | os.O_APPEND |
                    os.O_NOFOLLOW | os.O_CLOEXEC,
                    0o600,
                    dir_fd=clip_fd,
                )
                if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                    os.close(lock_fd)
                    raise GenerationSettingsError(
                        "generation settings lock is unsafe")
                with os.fdopen(lock_fd, "a+b") as lock:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                    try:
                        atomic_write_bytes_at(
                            clip_fd, target.name, data,
                            label="generation settings manifest")
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (SafeFilesystemError, OSError) as exc:
            raise GenerationSettingsError(
                "generation settings publication is unsafe") from exc
        return self.describe(project, clip, include_options=True)
