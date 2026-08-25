from __future__ import annotations

import math
import re
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from studio_core.identifiers import validate_clip_id


MOVIE_CONTRACT_VERSION = 1
MOVIE_DIRECTORY_RE = re.compile(r"movie-(\d{3,})")
MOVIE_FILENAME = "movie.mp4"
MOVIE_PROVENANCE = "provenance.json"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}
COPY_VIDEO_CODECS = {"h264", "hevc", "av1"}
COPY_AUDIO_CODECS = {"aac", "mp3", "alac", "ac3", "eac3"}


class MovieContractError(ValueError):
    pass


def _exact_dict(value: Any, keys: set[str], message: str) -> dict:
    if not isinstance(value, dict) or set(value) != keys:
        raise MovieContractError(message)
    return value


def _component(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith(".")
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise MovieContractError(f"movie {field} is invalid")
    return value


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise MovieContractError(f"movie {field} is invalid")
    return value


def _integer(value: Any, field: str, *, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise MovieContractError(f"movie {field} is invalid")
    return value


def _fraction(value: Any, field: str) -> str:
    text = _string(value, field)
    try:
        if Fraction(text) <= 0:
            raise ValueError
    except (ValueError, ZeroDivisionError) as exc:
        raise MovieContractError(f"movie {field} is invalid") from exc
    return text


@dataclass(frozen=True)
class MovieVideoProbe:
    codec_name: str
    width: int
    height: int
    pix_fmt: str
    r_frame_rate: str
    time_base: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "codec_name": self.codec_name,
            "width": self.width,
            "height": self.height,
            "pix_fmt": self.pix_fmt,
            "r_frame_rate": self.r_frame_rate,
            "time_base": self.time_base,
        }


@dataclass(frozen=True)
class MovieAudioProbe:
    codec_name: str
    sample_rate: str
    channels: int
    channel_layout: str | None
    time_base: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "codec_name": self.codec_name,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "channel_layout": self.channel_layout,
            "time_base": self.time_base,
        }


@dataclass(frozen=True)
class MovieProbe:
    duration_seconds: float
    video: MovieVideoProbe
    audio: MovieAudioProbe | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "duration_seconds": self.duration_seconds,
            "video": self.video.to_dict(),
            "audio": self.audio.to_dict() if self.audio is not None else None,
        }


@dataclass(frozen=True)
class MovieSource:
    clip_id: str
    clip_title: str
    generation: str
    filename: str
    size: int
    sha256: str
    probe: MovieProbe

    def to_dict(self) -> dict[str, Any]:
        return {
            "clip_id": self.clip_id,
            "clip_title": self.clip_title,
            "generation": self.generation,
            "filename": self.filename,
            "size": self.size,
            "sha256": self.sha256,
            "probe": self.probe.to_dict(),
        }


@dataclass(frozen=True)
class MovieTarget:
    width: int
    height: int
    fps: str
    sample_rate: int
    channels: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }


@dataclass(frozen=True)
class MovieAssembly:
    mode: str
    hard_cuts: bool
    target: MovieTarget

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "hard_cuts": self.hard_cuts,
            "target": self.target.to_dict(),
        }


@dataclass(frozen=True)
class MovieOutput:
    id: str
    filename: str
    provenance: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "filename": self.filename,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class MovieContract:
    schema_version: int
    action: str
    sources: tuple[MovieSource, ...]
    assembly: MovieAssembly
    output: MovieOutput

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action": self.action,
            "sources": [source.to_dict() for source in self.sources],
            "assembly": self.assembly.to_dict(),
            "output": self.output.to_dict(),
        }


def _parse_video(value: Any) -> MovieVideoProbe:
    video = _exact_dict(
        value,
        {"codec_name", "width", "height", "pix_fmt", "r_frame_rate", "time_base"},
        "movie video probe is invalid",
    )
    return MovieVideoProbe(
        codec_name=_string(video["codec_name"], "video codec"),
        width=_integer(video["width"], "video width"),
        height=_integer(video["height"], "video height"),
        pix_fmt=_string(video["pix_fmt"], "video pixel format"),
        r_frame_rate=_fraction(video["r_frame_rate"], "video frame rate"),
        time_base=_fraction(video["time_base"], "video time base"),
    )


def _parse_audio(value: Any) -> MovieAudioProbe | None:
    if value is None:
        return None
    audio = _exact_dict(
        value,
        {"codec_name", "sample_rate", "channels", "channel_layout", "time_base"},
        "movie audio probe is invalid",
    )
    sample_rate = _string(audio["sample_rate"], "audio sample rate")
    if not sample_rate.isascii() or not sample_rate.isdigit() or int(sample_rate) <= 0:
        raise MovieContractError("movie audio sample rate is invalid")
    layout = audio["channel_layout"]
    if layout is not None and not isinstance(layout, str):
        raise MovieContractError("movie audio channel layout is invalid")
    return MovieAudioProbe(
        codec_name=_string(audio["codec_name"], "audio codec"),
        sample_rate=sample_rate,
        channels=_integer(audio["channels"], "audio channels"),
        channel_layout=layout,
        time_base=_fraction(audio["time_base"], "audio time base"),
    )


def _parse_probe(value: Any) -> MovieProbe:
    probe = _exact_dict(
        value,
        {"duration_seconds", "video", "audio"},
        "movie source probe is invalid",
    )
    duration = probe["duration_seconds"]
    if (
        isinstance(duration, bool)
        or not isinstance(duration, (int, float))
        or not math.isfinite(duration)
        or duration <= 0
    ):
        raise MovieContractError("movie source duration is invalid")
    return MovieProbe(float(duration), _parse_video(probe["video"]), _parse_audio(probe["audio"]))


def _copy_compatible(sources: tuple[MovieSource, ...]) -> bool:
    first = sources[0]
    signature = (first.probe.video, first.probe.audio)
    for source in sources:
        if Path(source.filename).suffix.lower() not in {".mp4", ".mov"}:
            return False
        if source.probe.video.codec_name not in COPY_VIDEO_CODECS:
            return False
        if source.probe.audio is not None and source.probe.audio.codec_name not in COPY_AUDIO_CODECS:
            return False
        if (source.probe.video, source.probe.audio) != signature:
            return False
    return True


def _target_for_sources(sources: tuple[MovieSource, ...]) -> MovieTarget:
    video = sources[0].probe.video
    return MovieTarget(
        width=max(2, video.width - video.width % 2),
        height=max(2, video.height - video.height % 2),
        fps=video.r_frame_rate,
        sample_rate=48_000,
        channels=2,
    )


def parse_movie_contract(value: Any) -> MovieContract:
    contract = _exact_dict(
        value,
        {"schema_version", "action", "sources", "assembly", "output"},
        "movie export payload is invalid",
    )
    if (
        contract["schema_version"] != MOVIE_CONTRACT_VERSION
        or contract["action"] != "export-selected-takes"
        or not isinstance(contract["sources"], list)
        or not contract["sources"]
    ):
        raise MovieContractError("movie export payload is invalid")

    sources: list[MovieSource] = []
    clip_ids: set[str] = set()
    for value_source in contract["sources"]:
        source = _exact_dict(
            value_source,
            {"clip_id", "clip_title", "generation", "filename", "size", "sha256", "probe"},
            "movie export source contract is invalid",
        )
        try:
            clip_id = validate_clip_id(source["clip_id"])
        except ValueError as exc:
            raise MovieContractError(str(exc)) from exc
        filename = _component(source["filename"], "source filename")
        generation = _component(source["generation"], "source generation")
        if (
            clip_id in clip_ids
            or not isinstance(source["clip_title"], str)
            or not source["clip_title"].strip()
            or re.fullmatch(r"[0-9]{3,}", generation) is None
            or Path(filename).suffix.lower() not in VIDEO_EXTENSIONS
            or not isinstance(source["sha256"], str)
            or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
        ):
            raise MovieContractError("movie export source contract is invalid")
        clip_ids.add(clip_id)
        sources.append(MovieSource(
            clip_id=clip_id,
            clip_title=source["clip_title"],
            generation=generation,
            filename=filename,
            size=_integer(source["size"], "source size"),
            sha256=source["sha256"],
            probe=_parse_probe(source["probe"]),
        ))
    typed_sources = tuple(sources)

    assembly_value = _exact_dict(
        contract["assembly"],
        {"mode", "hard_cuts", "target"},
        "movie export assembly contract is invalid",
    )
    target_value = _exact_dict(
        assembly_value["target"],
        {"width", "height", "fps", "sample_rate", "channels"},
        "movie export target contract is invalid",
    )
    target = MovieTarget(
        width=_integer(target_value["width"], "target width", minimum=2),
        height=_integer(target_value["height"], "target height", minimum=2),
        fps=_fraction(target_value["fps"], "target frame rate"),
        sample_rate=_integer(target_value["sample_rate"], "target sample rate"),
        channels=_integer(target_value["channels"], "target channels"),
    )
    expected_mode = "stream-copy" if _copy_compatible(typed_sources) else "normalized"
    if (
        assembly_value["mode"] != expected_mode
        or assembly_value["hard_cuts"] is not True
        or target != _target_for_sources(typed_sources)
    ):
        raise MovieContractError("movie export assembly contract is invalid")

    output_value = _exact_dict(
        contract["output"],
        {"id", "filename", "provenance"},
        "movie export output contract is invalid",
    )
    movie_id = output_value["id"]
    if (
        not isinstance(movie_id, str)
        or MOVIE_DIRECTORY_RE.fullmatch(movie_id) is None
        or output_value["filename"] != MOVIE_FILENAME
        or output_value["provenance"] != MOVIE_PROVENANCE
    ):
        raise MovieContractError("movie export output contract is invalid")

    return MovieContract(
        schema_version=MOVIE_CONTRACT_VERSION,
        action="export-selected-takes",
        sources=typed_sources,
        assembly=MovieAssembly(expected_mode, True, target),
        output=MovieOutput(movie_id, MOVIE_FILENAME, MOVIE_PROVENANCE),
    )
