"""Local media probing and evidence extraction."""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_PROTOCOLS = "file,pipe"
_MAX_FRAMES = 12
_COPY_CHUNK_SIZE = 1024 * 1024
_FORMATS = "mov,matroska,avi,mpegts,mpeg,m4v,flv,ogg"
_MANIFEST_SUFFIXES = {".ffconcat", ".m3u", ".m3u8", ".mpd"}
_MANIFEST_FORMATS = {"concat", "dash", "hls", "smoothstreaming"}


class MediaInfo(BaseModel):
    """Properties of the first usable video stream in a local media file."""

    model_config = ConfigDict(extra="forbid")

    duration: float = Field(gt=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    has_audio: bool
    rotation: int
    frame_times: list[float]
    video_start_time: float = 0.0
    audio_start_offset: float | None = None

    @field_validator("duration", "video_start_time")
    @classmethod
    def _finite_scalar(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeline values must be finite")
        return value

    @field_validator("audio_start_offset")
    @classmethod
    def _finite_optional_scalar(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("audio_start_offset must be finite when present")
        return value

    @field_validator("frame_times")
    @classmethod
    def _valid_frame_times(cls, values: list[float]) -> list[float]:
        if not values:
            raise ValueError("frame_times must not be empty")
        if any(not math.isfinite(value) or value < 0 for value in values):
            raise ValueError("frame_times must be finite and non-negative")
        if any(current <= previous for previous, current in zip(values, values[1:], strict=False)):
            raise ValueError("frame_times must be strictly increasing")
        return values

    @model_validator(mode="after")
    def _duration_contains_frames(self) -> MediaInfo:
        if self.frame_times[-1] >= self.duration:
            raise ValueError("duration must be greater than the final frame timestamp")
        return self


@dataclass(frozen=True)
class _ProbeResult:
    info: MediaInfo
    video_index: int
    audio_index: int | None


def _local_file(path: Path) -> Path:
    try:
        candidate = Path(path).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"media source is not an existing local file: {path}") from exc
    if not candidate.is_file():
        raise ValueError(f"media source is not a regular file: {path}")
    return candidate


def _reject_manifest(path: Path) -> None:
    if path.suffix.lower() in _MANIFEST_SUFFIXES:
        raise ValueError("playlist and manifest inputs are not supported")
    try:
        with path.open("rb") as stream:
            prefix = stream.read(4096)
    except OSError as exc:
        raise ValueError(f"unable to read media source: {path}") from exc
    stripped = prefix.lstrip(b"\xef\xbb\xbf\x00\t\r\n ")
    if (
        stripped.startswith(b"#EXTM3U")
        or stripped.startswith(b"ffconcat version")
        or b"<MPD" in stripped
        or b"<SmoothStreamingMedia" in stripped
    ):
        raise ValueError("playlist and manifest inputs are not supported")


def _run(command: list[str], purpose: str) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise RuntimeError(f"unable to run {command[0]} for {purpose}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or "unknown ffmpeg error"
        raise RuntimeError(f"{purpose} failed: {detail}")
    return result


def _as_finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags")
    if isinstance(tags, dict):
        value = _as_finite_float(tags.get("rotate"))
        if value is not None:
            return int(round(value))

    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for item in side_data:
            if isinstance(item, dict):
                value = _as_finite_float(item.get("rotation"))
                if value is not None:
                    return int(round(value))
    return 0


def _metadata(
    path: Path,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            _PROTOCOLS,
            "-format_whitelist",
            _FORMATS,
            "-show_entries",
            (
                "format=duration,start_time,format_name:"
                "stream=index,codec_type,width,height,duration,start_time:"
                "stream_tags=rotate:stream_side_data=rotation:"
                "stream_disposition=attached_pic"
            ),
            "-of",
            "json",
            str(path),
        ],
        "media probe",
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe returned invalid JSON") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise ValueError("media contains no streams")

    video_stream: dict[str, Any] | None = None
    audio_stream: dict[str, Any] | None = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        stream_index = stream.get("index")
        if not isinstance(stream_index, int):
            continue
        codec_type = stream.get("codec_type")
        if codec_type == "audio" and audio_stream is None:
            audio_stream = stream
        elif codec_type == "video" and video_stream is None:
            disposition = stream.get("disposition")
            attached = isinstance(disposition, dict) and disposition.get("attached_pic") == 1
            if not attached:
                video_stream = stream

    if video_stream is None:
        raise ValueError("media contains no usable video stream")

    format_data = payload.get("format")
    if not isinstance(format_data, dict):
        format_data = {}
    format_names = str(format_data.get("format_name", "")).lower().split(",")
    if _MANIFEST_FORMATS.intersection(format_names):
        raise ValueError("playlist and manifest inputs are not supported")
    return format_data, video_stream, audio_stream


def _frame_timestamps(path: Path, video_index: int) -> tuple[list[float], float]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-protocol_whitelist",
        _PROTOCOLS,
        "-format_whitelist",
        _FORMATS,
        "-select_streams",
        str(video_index),
        "-show_frames",
        "-show_entries",
        "frame=best_effort_timestamp_time",
        "-of",
        "csv=p=0",
        str(path),
    ]

    raw_times: list[float] = []
    try:
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as stderr:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            assert process.stdout is not None
            for line in process.stdout:
                value = _as_finite_float(line.strip().split(",", 1)[0])
                if value is not None:
                    raw_times.append(value)
            return_code = process.wait()
            if return_code != 0:
                stderr.seek(0)
                detail = stderr.read().strip() or "unknown ffprobe error"
                raise RuntimeError(f"frame timestamp probe failed: {detail}")
    except OSError as exc:
        raise RuntimeError("unable to run ffprobe for frame timestamp probe") from exc

    if not raw_times:
        raise ValueError("video contains no decodable frames")

    # best_effort_timestamp_time is on the media timeline.  Basing the public
    # timeline on the first displayed frame removes a non-zero container/stream
    # start time while retaining genuine VFR gaps.
    origin = min(raw_times)
    normalized = sorted({round(value - origin, 9) for value in raw_times if value >= origin})
    if not normalized:
        raise ValueError("video contains no valid frame timestamps")
    return normalized, origin


def _inspect(path: Path) -> _ProbeResult:
    source = _local_file(path)
    _reject_manifest(source)
    format_data, video_stream, audio_stream = _metadata(source)

    # A stream duration describes the normalized video timeline directly.  The
    # container duration is only a fallback because it may include other streams
    # or timeline offsets.
    duration = _as_finite_float(video_stream.get("duration"))
    if duration is None or duration <= 0:
        duration = _as_finite_float(format_data.get("duration"))
    if duration is None or duration <= 0:
        raise ValueError("video has no valid positive duration")

    width = video_stream.get("width")
    height = video_stream.get("height")
    video_index = video_stream.get("index")
    if not isinstance(width, int) or width <= 0 or not isinstance(height, int) or height <= 0:
        raise ValueError("video has invalid dimensions")
    if not isinstance(video_index, int):
        raise ValueError("video stream has no valid index")

    frame_times, video_start_time = _frame_timestamps(source, video_index)
    audio_index = audio_stream.get("index") if audio_stream is not None else None
    if not isinstance(audio_index, int):
        audio_index = None
    audio_start_time = (
        _as_finite_float(audio_stream.get("start_time")) if audio_stream is not None else None
    )
    audio_start_offset = (
        round(audio_start_time - video_start_time, 9) if audio_start_time is not None else None
    )
    info = MediaInfo(
        duration=duration,
        width=width,
        height=height,
        has_audio=audio_index is not None,
        rotation=_rotation(video_stream),
        frame_times=frame_times,
        video_start_time=video_start_time,
        audio_start_offset=audio_start_offset,
    )
    return _ProbeResult(info=info, video_index=video_index, audio_index=audio_index)


def probe(path: Path) -> MediaInfo:
    """Probe a regular local video using ffprobe and return its real frame PTSs."""

    return _inspect(path).info


def _representative_times(frame_times: list[float], count: int = _MAX_FRAMES) -> list[float]:
    if len(frame_times) <= count:
        return list(frame_times)

    first = frame_times[0]
    last = frame_times[-1]
    selected: list[float] = []
    for position in range(count):
        target = first + (last - first) * position / (count - 1)
        right = bisect.bisect_left(frame_times, target)
        candidates = []
        if right < len(frame_times):
            candidates.append(frame_times[right])
        if right > 0:
            candidates.append(frame_times[right - 1])
        chosen = min(candidates, key=lambda value: (abs(value - target), value))
        if not selected or chosen != selected[-1]:
            selected.append(chosen)

    if selected[0] != first:
        selected.insert(0, first)
    if selected[-1] != last:
        selected.append(last)

    # Extremely uneven timelines can map adjacent targets to one frame. Fill
    # remaining slots from the largest temporal gaps, still using actual PTSs.
    while len(selected) < count:
        available = [value for value in frame_times if value not in set(selected)]
        if not available:
            break
        chosen = max(available, key=lambda value: min(abs(value - item) for item in selected))
        bisect.insort(selected, chosen)
    return selected[:count] if selected[-1] == last else selected[: count - 1] + [last]


def _selection_filter(all_times: list[float], selected: list[float]) -> str:
    clauses: list[str] = []
    for value in selected:
        index = bisect.bisect_left(all_times, value)
        previous = all_times[index - 1] if index > 0 else value - 1.0
        following = all_times[index + 1] if index + 1 < len(all_times) else value + 1.0
        lower = (previous + value) / 2.0
        upper = (value + following) / 2.0
        clauses.append(f"between(t\\,{lower:.12f}\\,{upper:.12f})")
    return "setpts=PTS-STARTPTS,select=" + "+".join(clauses)


def _extract_frames(
    source: Path,
    destination: Path,
    video_index: int,
    all_times: list[float],
    selected: list[float],
) -> list[Path]:
    frames_dir = destination / "frames"
    frames_dir.mkdir()
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-protocol_whitelist",
            _PROTOCOLS,
            "-format_whitelist",
            _FORMATS,
            "-i",
            str(source),
            "-map",
            f"0:{video_index}",
            "-an",
            "-vf",
            _selection_filter(all_times, selected),
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(len(selected)),
            "-q:v",
            "2",
            str(frames_dir / "%03d.jpg"),
        ],
        "frame extraction",
    )
    files = sorted(frames_dir.glob("*.jpg"))
    if len(files) != len(selected):
        raise RuntimeError(
            f"frame extraction produced {len(files)} files; expected {len(selected)}"
        )
    return files


def _extract_audio(source: Path, output: Path, audio_index: int) -> None:
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-protocol_whitelist",
            _PROTOCOLS,
            "-format_whitelist",
            _FORMATS,
            "-i",
            str(source),
            "-map",
            f"0:{audio_index}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        "audio extraction",
    )
    if not output.is_file() or output.stat().st_size == 0:
        raise RuntimeError("audio extraction did not produce a non-empty WAV file")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(_COPY_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_and_hash(source: Path, output: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with source.open("rb") as reader, output.open("xb") as writer:
        while chunk := reader.read(_COPY_CHUNK_SIZE):
            writer.write(chunk)
            digest.update(chunk)
            size += len(chunk)
    copied_digest = _sha256(output)
    if copied_digest != digest.hexdigest():
        raise RuntimeError("copied source hash does not match input")
    return copied_digest, size


def _file_record(
    path: Path,
    destination: Path,
    kind: str,
    time: float | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path.relative_to(destination).as_posix(),
        "sha256": _sha256(path),
        "size": path.stat().st_size,
        "kind": kind,
    }
    if time is not None:
        record["time"] = time
    return record


def prepare(source: Path, destination: Path) -> dict[str, Any]:
    """Copy, probe, hash, and extract bounded evidence from a local video."""

    source_path = _local_file(source)
    destination_path = Path(destination).expanduser().resolve(strict=True)
    if not destination_path.is_dir():
        raise ValueError("destination must be a directory")
    if any(destination_path.iterdir()):
        raise ValueError("destination must be empty")

    try:
        copied_source = destination_path / f"source{source_path.suffix}"
        source_hash, source_size = _copy_and_hash(source_path, copied_source)
        inspected = _inspect(copied_source)
        selected = _representative_times(inspected.info.frame_times)
        files: list[dict[str, Any]] = [
            {
                "path": copied_source.relative_to(destination_path).as_posix(),
                "sha256": source_hash,
                "size": source_size,
                "kind": "source",
            }
        ]

        frame_files = _extract_frames(
            copied_source,
            destination_path,
            inspected.video_index,
            inspected.info.frame_times,
            selected,
        )
        files.extend(
            _file_record(path, destination_path, "frame", frame_time)
            for path, frame_time in zip(frame_files, selected, strict=True)
        )

        if inspected.audio_index is not None:
            audio_path = destination_path / "audio.wav"
            _extract_audio(copied_source, audio_path, inspected.audio_index)
            files.append(_file_record(audio_path, destination_path, "audio"))

        return {"media": inspected.info.model_dump(), "files": files}
    except Exception:
        for child in destination_path.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        raise
