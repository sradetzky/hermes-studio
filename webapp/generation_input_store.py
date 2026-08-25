from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

from studio_core.projects import ClipStore
from studio_core.safe_files import (
    SafeFilesystemError,
    open_directory,
    open_directory_at,
    open_regular_file,
    open_regular_file_at,
)


DERIVED_INPUT_DIRECTORY = "generation-inputs"
EXTRACTION_OFFSET_SECONDS = 0.25


class GenerationInputError(ValueError):
    pass


def _sha256_descriptor(descriptor: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while chunk := os.pread(descriptor, 1024 * 1024, offset):
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


class GenerationInputStore:
    """Resolve, materialize, and revalidate immutable generation inputs."""

    def __init__(self, *, command_runner=subprocess.run):
        self.command_runner = command_runner
        self.clips = ClipStore()

    def _previous_selected_take(self, project: Path, clip_id: str) -> dict[str, Any]:
        try:
            manifest = self.clips.describe(project)
        except Exception as exc:
            raise GenerationInputError("project clip manifest is unsafe") from exc
        entries = manifest["clips"]
        target_index = next(
            (index for index, entry in enumerate(entries)
             if entry["id"] == clip_id),
            None,
        )
        if target_index is None:
            raise GenerationInputError(f"clip not found: {clip_id}")
        target = entries[target_index]
        if not target["enabled"]:
            raise GenerationInputError("clip is disabled")
        previous = next(
            (entry for entry in reversed(entries[:target_index]) if entry["enabled"]),
            None,
        )
        if previous is None or previous["selected_take"] is None:
            raise GenerationInputError(
                "previous selected take changed after enqueue")
        selected = previous["selected_take"]
        try:
            source_clip = self.clips.resolve_clip(project, previous["id"])
            source_path = (
                source_clip / "generations" / selected["generation"] /
                selected["filename"])
            with open_regular_file(source_path):
                pass
        except (FileNotFoundError, OSError, SafeFilesystemError, ValueError) as exc:
            raise GenerationInputError(
                "previous selected take changed after enqueue") from exc
        return {
            "source_clip_id": previous["id"],
            "source_generation_id": selected["generation"],
            "source_filename": selected["filename"],
            "source_path": source_path,
        }

    def describe_previous_selected_take(
            self, project: Path, clip_id: str, *, mode: str,
            project_reference_count: int) -> dict[str, Any]:
        if mode != "r2v" or not 1 <= project_reference_count <= 8:
            return {"eligible": False}
        try:
            source = self._previous_selected_take(project, clip_id)
        except GenerationInputError:
            return {"eligible": False}
        return {
            "eligible": True,
            "source_clip_id": source["source_clip_id"],
            "source_generation_id": source["source_generation_id"],
            "source_filename": source["source_filename"],
            "picture_number": project_reference_count + 1,
        }

    @staticmethod
    def snapshot_project_reference(
            project: Path, filename: str, *, slot: int) -> dict[str, Any]:
        path = project / "references" / filename
        try:
            with open_regular_file(path) as opened:
                digest = _sha256_descriptor(opened.descriptor)
        except (FileNotFoundError, OSError, SafeFilesystemError) as exc:
            raise GenerationInputError(f"project reference changed: {filename}") from exc
        return {
            "type": "project_reference",
            "slot": slot,
            "filename": filename,
            "sha256": digest,
        }

    def materialize_previous_selected_take(
            self, project: Path, clip_id: str, *,
            project_reference_count: int) -> dict[str, Any]:
        if not 1 <= project_reference_count <= 8:
            raise GenerationInputError(
                "previous selected take requires 1–8 project references")
        source = self._previous_selected_take(project, clip_id)
        clip = self.clips.resolve_clip(project, clip_id)
        derived_filename = f"previous-selected-take-{uuid.uuid4().hex}.png"
        try:
            with open_regular_file(source["source_path"]) as opened_source:
                source_sha256 = _sha256_descriptor(opened_source.descriptor)
                with open_directory(clip) as clip_fd:
                    try:
                        os.mkdir(
                            DERIVED_INPUT_DIRECTORY, mode=0o700, dir_fd=clip_fd)
                    except FileExistsError:
                        pass
                    with open_directory_at(
                            clip_fd, DERIVED_INPUT_DIRECTORY) as derived_fd:
                        command = [
                            "ffmpeg", "-v", "error", "-nostdin", "-n",
                            "-sseof", f"-{EXTRACTION_OFFSET_SECONDS:.3f}",
                            "-i", f"/proc/self/fd/{opened_source.descriptor}",
                            "-frames:v", "1",
                            f"/proc/self/fd/{derived_fd}/{derived_filename}",
                        ]
                        result = self.command_runner(
                            command,
                            pass_fds=(opened_source.descriptor, derived_fd),
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        if result.returncode:
                            try:
                                os.unlink(derived_filename, dir_fd=derived_fd)
                            except OSError:
                                pass
                            raise GenerationInputError(
                                "could not extract the previous selected take's "
                                f"last frame: {result.stderr.strip()[-1000:]}")
                        try:
                            with open_regular_file_at(
                                    derived_fd, derived_filename) as derived:
                                if derived.stat.st_size <= 0:
                                    raise GenerationInputError(
                                        "derived previous-take frame is empty")
                                derived_sha256 = _sha256_descriptor(
                                    derived.descriptor)
                        except Exception:
                            try:
                                os.unlink(derived_filename, dir_fd=derived_fd)
                            except OSError:
                                pass
                            raise
        except GenerationInputError:
            raise
        except (FileNotFoundError, OSError, SafeFilesystemError) as exc:
            raise GenerationInputError(
                "previous selected take could not be materialized safely") from exc
        return {
            "type": "previous_selected_take_last_frame",
            "slot": project_reference_count + 1,
            "source_clip_id": source["source_clip_id"],
            "source_generation_id": source["source_generation_id"],
            "source_filename": source["source_filename"],
            "source_video_sha256": source_sha256,
            "extraction_offset_seconds": EXTRACTION_OFFSET_SECONDS,
            "derived_filename": derived_filename,
            "derived_frame_sha256": derived_sha256,
        }

    def validate(
            self, project: Path, clip_id: str,
            inputs: list[dict[str, Any]]) -> list[Path]:
        paths: list[Path] = []
        previous_inputs = [
            item for item in inputs
            if item.get("type") == "previous_selected_take_last_frame"
        ]
        if len(previous_inputs) > 1:
            raise GenerationInputError(
                "generation inputs contain multiple previous selected takes")
        for expected_slot, item in enumerate(inputs, 1):
            if item.get("slot") != expected_slot:
                raise GenerationInputError("generation input order changed")
            if item.get("type") == "project_reference":
                path = project / "references" / item["filename"]
                changed_message = f"project reference changed: {item['filename']}"
                try:
                    with open_regular_file(path) as opened:
                        if _sha256_descriptor(opened.descriptor) != item["sha256"]:
                            raise GenerationInputError(changed_message)
                except GenerationInputError:
                    raise
                except (FileNotFoundError, OSError, SafeFilesystemError) as exc:
                    raise GenerationInputError(changed_message) from exc
                paths.append(path)
                continue
            if item.get("type") != "previous_selected_take_last_frame":
                raise GenerationInputError("generation input type is unsupported")
            source = self._previous_selected_take(project, clip_id)
            if any((
                source["source_clip_id"] != item["source_clip_id"],
                source["source_generation_id"] != item["source_generation_id"],
                source["source_filename"] != item["source_filename"],
            )):
                raise GenerationInputError(
                    "previous selected take changed after enqueue")
            try:
                with open_regular_file(source["source_path"]) as opened_source:
                    if (_sha256_descriptor(opened_source.descriptor)
                            != item["source_video_sha256"]):
                        raise GenerationInputError(
                            "previous selected take source video changed after enqueue")
                derived = (
                    project / "clips" / clip_id / DERIVED_INPUT_DIRECTORY /
                    item["derived_filename"])
                with open_regular_file(derived) as opened_derived:
                    if (_sha256_descriptor(opened_derived.descriptor)
                            != item["derived_frame_sha256"]):
                        raise GenerationInputError(
                            "previous selected take derived frame changed after enqueue")
            except GenerationInputError:
                raise
            except (FileNotFoundError, OSError, SafeFilesystemError) as exc:
                raise GenerationInputError(
                    "previous selected take derived frame changed after enqueue") from exc
            paths.append(derived)
        return paths
