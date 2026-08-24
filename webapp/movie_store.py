from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import quote

from webapp.clip_store import ClipStore, ClipStoreError
from webapp.media_review_store import (
    MediaReviewError,
    MediaReviewStore,
    UnsupportedMediaError,
)
from webapp.safe_files import (
    SafeFilesystemError,
    open_directory,
    open_directory_at,
    open_regular_file_at,
    read_opened_text,
)


MOVIE_DIRECTORY_RE = re.compile(r"movie-(\d{3,})")
MOVIE_FILENAME = "movie.mp4"
MOVIE_PROVENANCE = "provenance.json"


class MovieStoreError(ValueError):
    pass


class MovieNotReadyError(MovieStoreError):
    pass


class MovieStore:
    def __init__(self, clips: ClipStore | None = None,
                 media: MediaReviewStore | None = None):
        self.clips = clips or ClipStore()
        self.media = media or MediaReviewStore()

    def readiness(self, project: Path) -> dict:
        try:
            manifest = self.clips.describe(project)
        except ClipStoreError as exc:
            raise MovieStoreError(str(exc)) from exc

        clips = []
        blocking = []
        for entry in manifest["clips"]:
            if not entry["enabled"]:
                continue
            selected = entry["selected_take"]
            reason = ""
            if selected is None:
                reason = "Select a video take"
            else:
                try:
                    clip = self.clips.resolve_clip(project, entry["id"])
                    if self.media.media_kind(selected["filename"]) != "video":
                        reason = "Selected take is not a video"
                    else:
                        with self.media.open_media(
                                clip,
                                selected["generation"],
                                selected["filename"]):
                            pass
                except UnsupportedMediaError:
                    reason = "Selected take is not a video"
                except (ClipStoreError, MediaReviewError, OSError):
                    reason = "Selected video is missing or unsafe"
            item = {
                "id": entry["id"],
                "title": entry["title"],
                "ready": not reason,
                "reason": reason,
                "selected_take": selected,
            }
            clips.append(item)
            if reason:
                blocking.append({
                    "id": entry["id"],
                    "title": entry["title"],
                    "reason": reason,
                })

        if not clips:
            blocking.append({
                "id": "",
                "title": manifest["title"],
                "reason": "Enable at least one clip",
            })
        return {
            "ready": bool(clips) and not blocking,
            "enabled_clip_count": len(clips),
            "clips": clips,
            "blocking": blocking,
        }

    @staticmethod
    def _movie_url(project: Path, movie_id: str) -> str:
        return (
            f"/media/projects/{quote(project.name)}/final/"
            f"{quote(movie_id)}/{quote(MOVIE_FILENAME)}"
        )

    @staticmethod
    def _movie_number(movie_id: str) -> int:
        match = MOVIE_DIRECTORY_RE.fullmatch(movie_id)
        if match is None:
            raise MovieStoreError(f"invalid movie export id: {movie_id}")
        return int(match.group(1))

    def list_movies(self, project: Path) -> list[dict]:
        final = project / "final"
        try:
            with open_directory(final) as final_fd:
                result = []
                for name in os.listdir(final_fd):
                    match = MOVIE_DIRECTORY_RE.fullmatch(name)
                    if not match:
                        continue
                    details = os.stat(name, dir_fd=final_fd, follow_symlinks=False)
                    if not stat.S_ISDIR(details.st_mode):
                        raise MovieStoreError(f"movie export is unsafe: {name}")
                    with open_directory_at(final_fd, name) as movie_fd:
                        opened = os.fstat(movie_fd)
                        if (opened.st_dev, opened.st_ino) != (
                                details.st_dev, details.st_ino):
                            raise MovieStoreError(
                                f"movie export changed while listing: {name}")
                        try:
                            with open_regular_file_at(
                                    movie_fd, MOVIE_FILENAME) as media:
                                size = media.stat.st_size
                            with open_regular_file_at(
                                    movie_fd, MOVIE_PROVENANCE) as provenance_file:
                                provenance = json.loads(
                                    read_opened_text(provenance_file))
                        except FileNotFoundError as exc:
                            raise MovieStoreError(
                                f"movie export is incomplete: {name}") from exc
                        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                            raise MovieStoreError(
                                f"movie provenance is invalid: {name}") from exc
                        if not isinstance(provenance, dict):
                            raise MovieStoreError(
                                f"movie provenance is invalid: {name}")
                        result.append({
                            "id": name,
                            "name": MOVIE_FILENAME,
                            "size": size,
                            "url": self._movie_url(project, name),
                            "created_at": str(provenance.get("created_at") or ""),
                            "clip_count": len(provenance.get("sources") or []),
                            "duration_seconds": provenance.get("output", {}).get(
                                "duration_seconds"),
                            "assembly_mode": provenance.get("assembly", {}).get(
                                "mode", ""),
                            "sha256": provenance.get("output", {}).get(
                                "sha256", ""),
                        })
                return sorted(
                    result,
                    key=lambda item: self._movie_number(item["id"]),
                    reverse=True,
                )
        except MovieStoreError:
            raise
        except (FileNotFoundError, SafeFilesystemError, OSError) as exc:
            raise MovieStoreError("movie export area is unsafe") from exc
