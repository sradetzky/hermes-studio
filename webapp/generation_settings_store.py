from __future__ import annotations

import fcntl
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from webapp.config import Settings


MANIFEST_NAME = "current_generation.json"
MODES = {"t2va", "i2va", "fl2va", "r2v"}
ASPECT_RATIOS = {
    "16:9": 16 / 9,
    "9:16": 9 / 16,
    "1:1": 1.0,
    "4:3": 4 / 3,
    "3:4": 3 / 4,
    "21:9": 21 / 9,
}
UPSCALE_COLORS = {"lab", "wavelet", "adain", "none"}
REF_IMAGE_SIZES = {"match", "max"}
DEFAULT_TURBO_LORA = "minimax_h3_turbo_v4_step600_ema_pruned_comfyui.safetensors"
CANVAS_MULTIPLE = 32
MAX_CANVAS_PIXELS = 1_100_000
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
    return float(value)


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
    if seed < 0 or seed >= 2**63:
        raise GenerationSettingsError(
            "seed must be between 0 and 9223372036854775807")
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
            "duration": 5,
            "aspect": "16:9",
            "mp": 0.4,
            "width": None,
            "height": None,
            "seed": None,
            "steps": 20,
            "accel": False,
            "turbo": False,
            "turbo_lora": DEFAULT_TURBO_LORA,
            "turbo_strength": 1.0,
            "w4a8": False,
            "unet": None,
            "ref_image_size": "match",
            "upscale": False,
            "upscale_scale": 2.0,
            "upscale_color": "lab",
            "upscale_chunk": True,
            "references": [],
        }

    @staticmethod
    def _prompt(project: Path) -> str:
        path = project / "current_prompt.txt"
        if not path.is_file() or path.is_symlink():
            return ""
        return path.read_text(encoding="utf-8")

    @staticmethod
    def prompt_hash(prompt: str) -> str:
        return hashlib.sha256(prompt.encode("utf-8")).hexdigest()

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
        duration = _number(payload["duration"], "duration")
        if duration < 4 or duration > 15 or not duration.is_integer():
            raise GenerationSettingsError(
                "duration must be a whole number from 4 to 15 seconds")
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
        turbo_strength = _number(
            payload["turbo_strength"], "turbo_strength")
        if turbo_strength < 0 or turbo_strength > 2:
            raise GenerationSettingsError(
                "turbo_strength must be between 0 and 2")
        upscale_scale = _number(payload["upscale_scale"], "upscale_scale")
        if upscale_scale < 1 or upscale_scale > 4:
            raise GenerationSettingsError(
                "upscale_scale must be between 1 and 4")
        upscale_color = str(payload["upscale_color"])
        if upscale_color not in UPSCALE_COLORS:
            raise GenerationSettingsError(
                f"upscale_color must be one of: {', '.join(sorted(UPSCALE_COLORS))}")
        ref_image_size = str(payload["ref_image_size"])
        if ref_image_size not in REF_IMAGE_SIZES:
            raise GenerationSettingsError(
                f"ref_image_size must be one of: {', '.join(sorted(REF_IMAGE_SIZES))}")

        references_value = payload["references"]
        if not isinstance(references_value, list):
            raise GenerationSettingsError("references must be a list")
        references = []
        for value in references_value:
            reference = _filename(value, "reference filename")
            assert reference is not None
            references.append(reference)
        if len(set(references)) != len(references):
            raise GenerationSettingsError("references may not contain duplicates")
        if len(references) > 9:
            raise GenerationSettingsError("H3 accepts at most 9 reference images")
        for reference in references:
            if Path(reference).suffix.lower() not in IMAGE_REFERENCE_EXTENSIONS:
                raise GenerationSettingsError(
                    f"unsupported reference type: {reference}")

        turbo_lora = _filename(
            payload["turbo_lora"], "turbo_lora", optional=True)
        unet = _filename(payload["unet"], "unet", optional=True)
        for value, field in ((turbo_lora, "turbo_lora"), (unet, "unet")):
            if value and Path(value).suffix.lower() != ".safetensors":
                raise GenerationSettingsError(
                    f"{field} must be a .safetensors filename")

        return {
            "mode": mode,
            "duration": int(duration),
            "aspect": aspect,
            "mp": round(mp, 3),
            "width": width,
            "height": height,
            "seed": seed,
            "steps": steps,
            "accel": _bool(payload["accel"], "accel"),
            "turbo": _bool(payload["turbo"], "turbo"),
            "turbo_lora": turbo_lora,
            "turbo_strength": round(turbo_strength, 3),
            "w4a8": _bool(payload["w4a8"], "w4a8"),
            "unet": unet,
            "ref_image_size": ref_image_size,
            "upscale": _bool(payload["upscale"], "upscale"),
            "upscale_scale": round(upscale_scale, 3),
            "upscale_color": upscale_color,
            "upscale_chunk": _bool(payload["upscale_chunk"], "upscale_chunk"),
            "references": references,
        }

    @staticmethod
    def _manifest_path(project: Path) -> Path:
        return project / MANIFEST_NAME

    def _read_manifest(self, project: Path) -> tuple[dict | None, str | None]:
        path = self._manifest_path(project)
        if not path.exists():
            return None, None
        if not path.is_file() or path.is_symlink():
            return None, "generation settings manifest is not a regular file"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
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
        directory = project / "references"
        if not directory.is_dir() or directory.is_symlink():
            return []
        return sorted(
            item.name for item in directory.iterdir()
            if item.is_file() and not item.is_symlink()
            and not item.name.startswith(".")
            and item.suffix.lower() in IMAGE_REFERENCE_EXTENSIONS
        )

    def _installed_models(self, directory_name: str, prefix: str) -> list[str]:
        directory = (
            self.app_settings.comfy_output.parent / "models" / directory_name)
        if not directory.is_dir():
            return []
        return sorted(
            item.name for item in directory.iterdir()
            if item.is_file() and not item.is_symlink()
            and item.suffix.lower() == ".safetensors"
            and item.name.lower().startswith(prefix)
        )

    def readiness(self, project: Path, manifest: dict | None,
                  manifest_error: str | None = None) -> dict:
        prompt = self._prompt(project)
        reasons = []
        warnings = []
        if not prompt.strip():
            reasons.append("Current prompt is empty")
        if manifest_error:
            reasons.append(manifest_error)
        elif manifest is None:
            reasons.append("Generation settings have not been saved")
        else:
            if manifest.get("schema_version") != 1:
                reasons.append("Generation settings schema is unsupported")
            if manifest.get("prompt_sha256") != self.prompt_hash(prompt):
                reasons.append("Current prompt changed after settings were saved")
            references = manifest["references"]
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
                reasons.append("R2V requires between 1 and 9 ordered references")
            if manifest["turbo"] and not manifest["turbo_lora"]:
                reasons.append("Turbo requires a LoRA filename")
            if manifest["turbo"] and not 4 <= manifest["steps"] <= 10:
                warnings.append("Turbo is normally used with 4–10 steps")
            if manifest["upscale"]:
                warnings.append(
                    "SeedVR2 upscale is VRAM-heavy; prefer a clean ~1MP H3 pass on this machine")
            if manifest["w4a8"]:
                warnings.append("W4A8 is experimental and needs a quality A/B check")

        settings = manifest or self.defaults()
        if settings["width"] is not None:
            width, height = settings["width"], settings["height"]
            size_mode = "explicit"
        else:
            width, height = resolution_from_mp(
                settings["aspect"], settings["mp"])
            size_mode = "mp"
        frames = duration_to_frames(float(settings["duration"]))
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
                "requested_seconds": settings["duration"],
                "frames": frames,
                "actual_seconds": round(frames / FPS, 3),
                "fps": FPS,
            },
        }

    def describe(self, project: Path, include_options: bool = False) -> dict:
        manifest, error = self._read_manifest(project)
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
            "readiness": self.readiness(project, manifest, error),
        }
        if result["settings"]["seed"] is not None:
            result["settings"]["seed"] = str(result["settings"]["seed"])
        if include_options:
            result["options"] = {
                "modes": sorted(MODES),
                "aspects": list(ASPECT_RATIOS),
                "ref_image_sizes": sorted(REF_IMAGE_SIZES),
                "upscale_colors": sorted(UPSCALE_COLORS),
                "references": self._reference_names(project),
                "turbo_loras": self._installed_models("loras", "minimax_h3"),
                "unets": self._installed_models(
                    "diffusion_models", "minimax_h3"),
            }
        return result

    def save(self, project: Path, payload: dict) -> dict:
        normalized = self.normalize(payload)
        prompt = self._prompt(project)
        manifest = {
            "schema_version": 1,
            "prompt_sha256": self.prompt_hash(prompt),
            "updated_at": datetime.now(timezone.utc).isoformat(),
            **normalized,
        }
        target = self._manifest_path(project)
        lock_path = project / ".generation-settings.lock"
        with lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            temp = project / f".{uuid.uuid4().hex}.generation-settings"
            try:
                with temp.open("x", encoding="utf-8") as handle:
                    json.dump(manifest, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                temp.replace(target)
            finally:
                temp.unlink(missing_ok=True)
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        return self.describe(project, include_options=True)
