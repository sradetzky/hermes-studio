from __future__ import annotations

import json
import hashlib
import math
import os
import re
import stat
import subprocess
import uuid
from contextlib import ExitStack
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

from webapp.clip_store import ClipStore, ClipStoreError
from webapp.identifiers import CLIP_ID_RE
from webapp.media_review_store import (
    MediaReviewError,
    MediaReviewStore,
    UnsupportedMediaError,
    VIDEO_EXTENSIONS,
)
from webapp.safe_files import (
    SafeFilesystemError,
    atomic_move_no_replace_at,
    open_directory,
    open_directory_at,
    open_regular_file_at,
    read_opened_text,
    remove_published_directory_if_same,
    write_new_regular_file_at,
)


MOVIE_DIRECTORY_RE = re.compile(r"movie-(\d{3,})")
MOVIE_FILENAME = "movie.mp4"
MOVIE_PROVENANCE = "provenance.json"
MOVIE_CONTRACT_VERSION = 1
COPY_VIDEO_CODECS = {"h264", "hevc", "av1"}
COPY_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3"}


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

    @staticmethod
    def _sha256(opened) -> str:
        digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(opened.descriptor, 1024 * 1024, offset)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)
            offset += len(chunk)

    @staticmethod
    def _probe_descriptor(descriptor: int) -> dict:
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_streams", "-show_format",
            "-of", "json", f"/proc/self/fd/{descriptor}",
        ], pass_fds=(descriptor,), capture_output=True, text=True, check=False)
        if result.returncode:
            raise MovieStoreError(
                f"selected video could not be inspected: {result.stderr.strip()}")
        try:
            payload = json.loads(result.stdout)
            streams = payload["streams"]
            video = next(
                stream for stream in streams if stream.get("codec_type") == "video")
            audio = next(
                (stream for stream in streams if stream.get("codec_type") == "audio"),
                None,
            )
            duration = float(payload["format"]["duration"])
            fps = str(video["r_frame_rate"])
            if Fraction(fps) <= 0 or duration <= 0:
                raise ValueError
            normalized = {
                "duration_seconds": duration,
                "video": {
                    key: video.get(key) for key in (
                        "codec_name", "width", "height", "pix_fmt",
                        "r_frame_rate", "time_base",
                    )
                },
                "audio": None,
            }
            if audio is not None:
                normalized["audio"] = {
                    key: audio.get(key) for key in (
                        "codec_name", "sample_rate", "channels",
                        "channel_layout", "time_base",
                    )
                }
            if (not isinstance(normalized["video"]["width"], int)
                    or not isinstance(normalized["video"]["height"], int)):
                raise ValueError
            return normalized
        except (KeyError, StopIteration, TypeError, ValueError, ZeroDivisionError) as exc:
            raise MovieStoreError("selected video metadata is incomplete") from exc

    @staticmethod
    def _copy_compatible(sources: list[dict]) -> bool:
        first = sources[0]
        signature = (first["probe"]["video"], first["probe"]["audio"])
        for source in sources:
            video = source["probe"]["video"]
            audio = source["probe"]["audio"]
            if Path(source["filename"]).suffix.lower() not in {".mp4", ".mov"}:
                return False
            if video.get("codec_name") not in COPY_VIDEO_CODECS:
                return False
            if audio is not None and audio.get("codec_name") not in COPY_AUDIO_CODECS:
                return False
            if (video, audio) != signature:
                return False
        return True

    def _next_movie_id(self, project: Path) -> str:
        movies = self.list_movies(project)
        number = max(
            (self._movie_number(movie["id"]) for movie in movies),
            default=0,
        ) + 1
        return f"movie-{number:03d}"

    def build_contract(self, project: Path) -> dict:
        readiness = self.readiness(project)
        if not readiness["ready"]:
            reasons = "; ".join(
                f"{item['title']}: {item['reason']}"
                for item in readiness["blocking"])
            raise MovieNotReadyError(f"project movie is not ready: {reasons}")

        sources = []
        for item in readiness["clips"]:
            selected = item["selected_take"]
            assert selected is not None
            try:
                clip = self.clips.resolve_clip(project, item["id"])
                with self.media.open_media(
                        clip, selected["generation"],
                        selected["filename"]) as opened:
                    sources.append({
                        "clip_id": item["id"],
                        "clip_title": item["title"],
                        "generation": selected["generation"],
                        "filename": selected["filename"],
                        "size": opened.stat.st_size,
                        "sha256": self._sha256(opened),
                        "probe": self._probe_descriptor(opened.descriptor),
                    })
            except (ClipStoreError, MediaReviewError, OSError) as exc:
                raise MovieNotReadyError(
                    f"project movie source changed: {item['title']}") from exc

        mode = "stream-copy" if self._copy_compatible(sources) else "normalized"
        target = self._target_for_sources(sources)
        contract = {
            "schema_version": MOVIE_CONTRACT_VERSION,
            "action": "export-selected-takes",
            "output": {
                "id": self._next_movie_id(project),
                "filename": MOVIE_FILENAME,
                "provenance": MOVIE_PROVENANCE,
            },
            "assembly": {
                "mode": mode,
                "hard_cuts": True,
                "target": target,
            },
            "sources": sources,
        }
        self._validate_contract(contract)
        return contract

    @staticmethod
    def _target_for_sources(sources: list[dict]) -> dict:
        first_video = sources[0]["probe"]["video"]
        width = int(first_video["width"])
        height = int(first_video["height"])
        return {
            "width": max(2, width - width % 2),
            "height": max(2, height - height % 2),
            "fps": first_video["r_frame_rate"],
            "sample_rate": 48000,
            "channels": 2,
        }

    @staticmethod
    def _valid_component(value: object) -> bool:
        return (
            isinstance(value, str) and bool(value)
            and not value.startswith(".")
            and "/" not in value and "\\" not in value
            and Path(value).name == value
        )

    @staticmethod
    def _validate_probe(probe: object) -> None:
        if not isinstance(probe, dict) or set(probe) != {
                "duration_seconds", "video", "audio"}:
            raise MovieStoreError("movie export contract is invalid")
        duration = probe["duration_seconds"]
        video = probe["video"]
        audio = probe["audio"]
        video_keys = {
            "codec_name", "width", "height", "pix_fmt", "r_frame_rate",
            "time_base",
        }
        if (isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0
                or not isinstance(video, dict) or set(video) != video_keys
                or not isinstance(video["width"], int) or video["width"] <= 0
                or not isinstance(video["height"], int) or video["height"] <= 0
                or any(not isinstance(video[key], str) or not video[key]
                       for key in (
                           "codec_name", "pix_fmt", "r_frame_rate",
                           "time_base"))):
            raise MovieStoreError("movie export contract is invalid")
        try:
            if Fraction(video["r_frame_rate"]) <= 0:
                raise MovieStoreError("movie export contract is invalid")
        except (ValueError, ZeroDivisionError) as exc:
            raise MovieStoreError("movie export contract is invalid") from exc
        if audio is None:
            return
        audio_keys = {
            "codec_name", "sample_rate", "channels", "channel_layout",
            "time_base",
        }
        if (not isinstance(audio, dict) or set(audio) != audio_keys
                or not isinstance(audio["codec_name"], str)
                or not audio["codec_name"]
                or not isinstance(audio["sample_rate"], str)
                or not audio["sample_rate"].isdigit()
                or int(audio["sample_rate"]) <= 0
                or not isinstance(audio["channels"], int)
                or audio["channels"] <= 0
                or not isinstance(audio["time_base"], str)
                or not audio["time_base"]
                or (audio["channel_layout"] is not None
                    and not isinstance(audio["channel_layout"], str))):
            raise MovieStoreError("movie export contract is invalid")

    @staticmethod
    def _validate_contract(contract: dict) -> None:
        if (not isinstance(contract, dict)
                or contract.get("schema_version") != MOVIE_CONTRACT_VERSION
                or contract.get("action") != "export-selected-takes"):
            raise MovieStoreError("movie export contract is invalid")
        output = contract.get("output")
        sources = contract.get("sources")
        assembly = contract.get("assembly")
        if (not isinstance(output, dict) or set(output) != {
                    "id", "filename", "provenance"}
                or not isinstance(output.get("id"), str)
                or MOVIE_DIRECTORY_RE.fullmatch(str(output.get("id") or "")) is None
                or output.get("filename") != MOVIE_FILENAME
                or output.get("provenance") != MOVIE_PROVENANCE
                or not isinstance(sources, list) or not sources
                or not isinstance(assembly, dict) or set(assembly) != {
                    "mode", "hard_cuts", "target"}
                or assembly.get("mode") not in {"stream-copy", "normalized"}
                or assembly.get("hard_cuts") is not True):
            raise MovieStoreError("movie export contract is invalid")
        required = {
            "clip_id", "clip_title", "generation", "filename", "size",
            "sha256", "probe",
        }
        clip_ids = set()
        for source in sources:
            if (not isinstance(source, dict) or set(source) != required
                    or CLIP_ID_RE.fullmatch(str(source.get("clip_id") or "")) is None
                    or source.get("clip_id") in clip_ids
                    or not isinstance(source.get("clip_title"), str)
                    or not source["clip_title"].strip()
                    or not MovieStore._valid_component(source.get("generation"))
                    or not MovieStore._valid_component(source.get("filename"))
                    or Path(source["filename"]).suffix.lower() not in VIDEO_EXTENSIONS
                    or not isinstance(source["size"], int)
                    or source["size"] <= 0
                    or not re.fullmatch(r"[0-9a-f]{64}", str(source["sha256"]))):
                raise MovieStoreError("movie export source contract is invalid")
            clip_ids.add(source["clip_id"])
            MovieStore._validate_probe(source["probe"])
        if (assembly["target"] != MovieStore._target_for_sources(sources)
                or assembly["mode"] != (
                    "stream-copy" if MovieStore._copy_compatible(sources)
                    else "normalized")):
            raise MovieStoreError("movie export contract is invalid")

    @staticmethod
    def _concat_bytes(descriptors: list[int]) -> bytes:
        return "".join(
            f"file '/proc/self/fd/{descriptor}'\n"
            for descriptor in descriptors
        ).encode("utf-8")

    @staticmethod
    def _normalized_command(sources: list[dict], descriptors: list[int],
                            staging_fd: int, target: dict) -> tuple[list[str], tuple[int, ...]]:
        command = ["ffmpeg", "-v", "error", "-nostdin"]
        for descriptor in descriptors:
            command += ["-i", f"/proc/self/fd/{descriptor}"]
        any_audio = any(source["probe"]["audio"] is not None for source in sources)
        audio_inputs = {}
        next_input = len(descriptors)
        if any_audio:
            for index, source in enumerate(sources):
                if source["probe"]["audio"] is None:
                    duration = source["probe"]["duration_seconds"]
                    command += [
                        "-f", "lavfi", "-i",
                        f"anullsrc=r=48000:cl=stereo:d={duration:.9f}",
                    ]
                    audio_inputs[index] = next_input
                    next_input += 1

        filters = []
        concat_inputs = []
        width = target["width"]
        height = target["height"]
        fps = target["fps"]
        for index, source in enumerate(sources):
            filters.append(
                f"[{index}:v:0]scale={width}:{height}:"
                "force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1,fps={fps},format=yuv420p,settb=AVTB,"
                f"setpts=PTS-STARTPTS[v{index}]"
            )
            concat_inputs.append(f"[v{index}]")
            if any_audio:
                audio_index = audio_inputs.get(index, index)
                filters.append(
                    f"[{audio_index}:a:0]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
                concat_inputs.append(f"[a{index}]")
        if any_audio:
            filters.append(
                "".join(concat_inputs) +
                f"concat=n={len(sources)}:v=1:a=1[vout][aout]")
        else:
            filters.append(
                "".join(concat_inputs) +
                f"concat=n={len(sources)}:v=1:a=0[vout]")
        command += ["-filter_complex", ";".join(filters), "-map", "[vout]"]
        if any_audio:
            command += ["-map", "[aout]"]
        command += [
            "-c:v", "libx264", "-preset", "medium", "-crf", "18",
            "-pix_fmt", "yuv420p",
        ]
        if any_audio:
            command += ["-c:a", "aac", "-b:a", "192k"]
        command += [
            "-movflags", "+faststart",
            f"/proc/self/fd/{staging_fd}/{MOVIE_FILENAME}",
        ]
        return command, tuple(descriptors + [staging_fd])

    def _ffmpeg_command(self, contract: dict, descriptors: list[int],
                        staging_fd: int) -> tuple[list[str], tuple[int, ...]]:
        if contract["assembly"]["mode"] == "normalized":
            return self._normalized_command(
                contract["sources"], descriptors, staging_fd,
                contract["assembly"]["target"])
        write_new_regular_file_at(
            staging_fd, "inputs.txt", self._concat_bytes(descriptors))
        return ([
            "ffmpeg", "-v", "error", "-nostdin", "-f", "concat",
            "-safe", "0", "-i", f"/proc/self/fd/{staging_fd}/inputs.txt",
            "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
            "-movflags", "+faststart",
            f"/proc/self/fd/{staging_fd}/{MOVIE_FILENAME}",
        ], tuple(descriptors + [staging_fd]))

    def export(self, project: Path, contract: dict, job_id: str) -> dict:
        self._validate_contract(contract)
        final = project / "final"
        staging_name = f".movie-{uuid.uuid4().hex}"
        staging_identity = None
        published = False
        with ExitStack() as stack:
            opened_sources = []
            for source in contract["sources"]:
                try:
                    clip = self.clips.resolve_clip(project, source["clip_id"])
                    opened = stack.enter_context(self.media.open_media(
                        clip, source["generation"], source["filename"]))
                except (ClipStoreError, MediaReviewError, OSError) as exc:
                    raise MovieStoreError(
                        f"movie source changed after enqueue: {source['clip_id']}") from exc
                if (opened.stat.st_size != source["size"]
                        or self._sha256(opened) != source["sha256"]
                        or self._probe_descriptor(opened.descriptor) != source["probe"]):
                    raise MovieStoreError(
                        f"movie source changed after enqueue: {source['clip_id']}")
                opened_sources.append(opened)

            try:
                with open_directory(final) as final_fd:
                    os.mkdir(staging_name, mode=0o700, dir_fd=final_fd)
                    with open_directory_at(final_fd, staging_name) as staging_fd:
                        details = os.fstat(staging_fd)
                        staging_identity = (details.st_dev, details.st_ino)
                        descriptors = [source.descriptor for source in opened_sources]
                        command, pass_fds = self._ffmpeg_command(
                            contract, descriptors, staging_fd)
                        result = subprocess.run(
                            command, pass_fds=pass_fds,
                            capture_output=True, text=True, check=False)
                        if result.returncode:
                            raise MovieStoreError(
                                "ffmpeg movie assembly failed: " +
                                result.stderr.strip()[-2000:])
                        with open_regular_file_at(
                                staging_fd, MOVIE_FILENAME) as output:
                            os.fsync(output.descriptor)
                            output_probe = self._probe_descriptor(output.descriptor)
                            output_sha256 = self._sha256(output)
                            output_size = output.stat.st_size
                        expected_duration = sum(
                            source["probe"]["duration_seconds"]
                            for source in contract["sources"])
                        actual_duration = output_probe["duration_seconds"]
                        if (output_size <= 0 or actual_duration <= 0
                                or abs(actual_duration - expected_duration) > max(
                                    0.5, expected_duration * 0.15)):
                            raise MovieStoreError(
                                "assembled movie failed duration validation")
                        provenance = {
                            "schema_version": MOVIE_CONTRACT_VERSION,
                            "job_id": job_id,
                            "created_at": datetime.now(timezone.utc).isoformat(),
                            "assembly": contract["assembly"],
                            "sources": contract["sources"],
                            "output": {
                                "id": contract["output"]["id"],
                                "filename": MOVIE_FILENAME,
                                "size": output_size,
                                "sha256": output_sha256,
                                "duration_seconds": actual_duration,
                                "probe": output_probe,
                            },
                        }
                        write_new_regular_file_at(
                            staging_fd, MOVIE_PROVENANCE,
                            (json.dumps(
                                provenance, indent=2,
                                ensure_ascii=False) + "\n").encode("utf-8"))
                        atomic_move_no_replace_at(
                            final_fd, staging_name, contract["output"]["id"],
                            expected_source_identity=staging_identity)
                        published = True
            except (SafeFilesystemError, OSError) as exc:
                raise MovieStoreError(
                    "movie publication failed safely") from exc
            finally:
                if staging_identity is not None and not published:
                    remove_published_directory_if_same(
                        final / staging_name, staging_identity)
        return self.verify_export(project, contract, job_id)

    def verify_export(self, project: Path, contract: dict, job_id: str) -> dict:
        self._validate_contract(contract)
        movie_id = contract["output"]["id"]
        movie = project / "final" / movie_id
        try:
            with open_directory(movie) as movie_fd:
                with open_regular_file_at(
                        movie_fd, MOVIE_PROVENANCE) as provenance_file:
                    provenance = json.loads(read_opened_text(provenance_file))
                with open_regular_file_at(movie_fd, MOVIE_FILENAME) as output:
                    output_sha256 = self._sha256(output)
                    output_size = output.stat.st_size
        except (FileNotFoundError, SafeFilesystemError, OSError,
                json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MovieStoreError(
                "movie export postcondition was not satisfied") from exc
        if (provenance.get("job_id") != job_id
                or provenance.get("assembly") != contract["assembly"]
                or provenance.get("sources") != contract["sources"]
                or provenance.get("output", {}).get("id") != movie_id
                or provenance.get("output", {}).get("filename") != MOVIE_FILENAME
                or provenance.get("output", {}).get("size") != output_size
                or provenance.get("output", {}).get("sha256") != output_sha256):
            raise MovieStoreError(
                "movie export does not match its immutable job contract")
        return next(
            movie for movie in self.list_movies(project)
            if movie["id"] == movie_id)
