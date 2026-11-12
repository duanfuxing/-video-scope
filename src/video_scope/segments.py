"""Prepare bounded, explicitly observed media ranges for analysis."""

from __future__ import annotations

import bisect
import importlib
import math
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Literal

from video_scope.evidence import EvidenceManifest, digest, validate_evidence
from video_scope.media import MediaInfo, probe

_FULL_INLINE_BYTES = 12 * 1024 * 1024
_MAX_DENSE_FRAMES = 24
_MAX_SHOT_CUTS = 8
_PROTOCOLS = "file,pipe"


def _run(command: list[str], purpose: str) -> None:
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


def _empty_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink():
        raise ValueError("analysis destination may not be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not path.is_dir() or any(path.iterdir()):
        raise ValueError("analysis destination must be empty")
    return path


def _mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    if guessed is not None and guessed.startswith("video/"):
        return guessed
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-protocol_whitelist",
            _PROTOCOLS,
            "-show_entries",
            "format=format_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError("unable to determine validated media MIME type")
    formats = set(result.stdout.strip().lower().split(","))
    if formats.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2", "m4v"}):
        return "video/mp4"
    if "webm" in formats:
        return "video/webm"
    mapping = {
        "matroska": "video/x-matroska",
        "avi": "video/x-msvideo",
        "mpegts": "video/mp2t",
        "mpeg": "video/mpeg",
        "flv": "video/x-flv",
        "ogg": "video/ogg",
    }
    for format_name, mime_type in mapping.items():
        if format_name in formats:
            return mime_type
    raise ValueError("validated source uses an unsupported model MIME type")


def _source(manifest: EvidenceManifest, evidence_dir: Path) -> Path:
    record = next(item for item in manifest.files if item.kind == "source")
    return (evidence_dir.resolve() / record.path).resolve(strict=True)


def _nearest_frame(frame_times: list[float], requested: float) -> float:
    index = bisect.bisect_left(frame_times, requested)
    choices: list[float] = []
    if index < len(frame_times):
        choices.append(frame_times[index])
    if index:
        choices.append(frame_times[index - 1])
    return min(choices, key=lambda value: (abs(value - requested), value))


def _selection_filter(frame_times: list[float], selected: list[float]) -> str:
    clauses: list[str] = []
    for value in selected:
        index = bisect.bisect_left(frame_times, value)
        previous = frame_times[index - 1] if index else value - 1.0
        following = frame_times[index + 1] if index + 1 < len(frame_times) else value + 1.0
        lower = (previous + value) / 2
        upper = (value + following) / 2
        clauses.append(rf"between(t\,{lower:.12f}\,{upper:.12f})")
    return "setpts=PTS-STARTPTS,select=" + "+".join(clauses)


def extract_frames(
    source: Path,
    destination: Path,
    times: list[float],
    *,
    frame_times: list[float] | None = None,
) -> list[dict[str, Any]]:
    """Extract JPEGs at the actual source PTS nearest each requested time.

    ``destination`` must be empty. The returned timestamps are observed PTSs,
    never synthetic frame-number/FPS estimates.
    """

    source = source.expanduser().resolve(strict=True)
    info = probe(source) if frame_times is None else None
    actual_times = list(info.frame_times if info is not None else frame_times or [])
    if not actual_times:
        raise ValueError("source has no observed frame timestamps")
    requested: list[float] = []
    for value in times:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("frame request times must be finite and non-negative")
        requested.append(float(value))
    selected = sorted({_nearest_frame(actual_times, value) for value in requested})
    destination = _empty_directory(destination)
    if not selected:
        return []
    _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-protocol_whitelist",
            _PROTOCOLS,
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-an",
            "-vf",
            _selection_filter(actual_times, selected),
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(len(selected)),
            "-q:v",
            "2",
            str(destination / "%03d.jpg"),
        ],
        "analysis frame extraction",
    )
    paths = sorted(destination.glob("*.jpg"))
    if len(paths) != len(selected):
        raise RuntimeError(
            f"frame extraction produced {len(paths)} files; expected {len(selected)}"
        )
    return [
        {"path": str(path.resolve()), "time": time}
        for path, time in zip(paths, selected, strict=True)
    ]


def extract_clip(
    source: Path,
    destination: Path,
    start: float,
    end: float,
    *,
    media: MediaInfo | None = None,
) -> dict[str, Any]:
    """Create one continuous bounded MP4 clip while retaining source audio."""

    source = source.expanduser().resolve(strict=True)
    info = probe(source) if media is None else media
    if (
        not math.isfinite(start)
        or not math.isfinite(end)
        or start < 0
        or end <= start
        or end > info.duration + 1e-6
    ):
        raise ValueError("clip interval is outside the source timeline")
    destination = destination.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("clip destination already exists")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.stem}-", suffix=".mp4", dir=destination.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    temporary.unlink()
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-protocol_whitelist",
        _PROTOCOLS,
        "-i",
        str(source),
        "-ss",
        f"{start:.9f}",
        "-t",
        f"{end - start:.9f}",
        "-map",
        "0:v:0",
    ]
    if info.has_audio:
        command.extend(["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"])
    else:
        command.append("-an")
    command.extend(
        [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-fps_mode",
            "passthrough",
            "-movflags",
            "+faststart",
            str(temporary),
        ]
    )
    try:
        _run(command, "analysis clip extraction")
        clipped = probe(temporary)
        if info.has_audio and not clipped.has_audio:
            raise RuntimeError("clip extraction unexpectedly removed the source audio track")
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise ValueError("clip destination already exists") from exc
        return {
            "path": str(destination.resolve()),
            "mime_type": "video/mp4",
            "start": float(start),
            "end": float(end),
            "kind": "clip",
        }
    finally:
        temporary.unlink(missing_ok=True)


def _dense_times(frame_times: list[float], end: float) -> list[float]:
    available = [value for value in frame_times if value < end and value <= 3.0]
    if len(available) <= _MAX_DENSE_FRAMES:
        return available
    selected: list[float] = []
    for index in range(_MAX_DENSE_FRAMES):
        target = index / 8
        choice = _nearest_frame(available, target)
        if choice not in selected:
            selected.append(choice)
    if len(selected) < _MAX_DENSE_FRAMES:
        for value in available:
            if value not in selected:
                selected.append(value)
                if len(selected) == _MAX_DENSE_FRAMES:
                    break
    return sorted(selected)


def _shot_candidates(
    source: Path, frame_times: list[float], observed_end: float
) -> tuple[list[dict[str, Any]], list[float]]:
    try:
        cv2 = importlib.import_module("cv2")
        numpy = importlib.import_module("numpy")
    except ImportError as exc:
        raise RuntimeError("OpenCV and NumPy are required for shot candidate detection") from exc

    observed_times = [time for time in frame_times if time < observed_end]
    frame_size = 64 * 36 * 3
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-protocol_whitelist",
        _PROTOCOLS,
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "scale=64:36",
        "-pix_fmt",
        "bgr24",
        "-fps_mode",
        "passthrough",
        "-frames:v",
        str(len(observed_times)),
        "-f",
        "rawvideo",
        "pipe:1",
    ]
    previous: Any | None = None
    scored: list[tuple[float, float]] = []
    try:
        with tempfile.TemporaryFile(mode="w+b") as stderr:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr)
            assert process.stdout is not None
            decoded = 0
            for time in observed_times:
                raw = bytearray()
                while len(raw) < frame_size:
                    chunk = process.stdout.read(frame_size - len(raw))
                    if not chunk:
                        break
                    raw.extend(chunk)
                if len(raw) != frame_size:
                    break
                frame = numpy.frombuffer(raw, dtype=numpy.uint8).reshape((36, 64, 3))
                if previous is not None:
                    channels = cv2.mean(cv2.absdiff(frame, previous))[:3]
                    score = float(sum(channels)) / (3 * 255.0)
                    if score >= 0.18:
                        scored.append((time, min(1.0, score)))
                previous = frame
                decoded += 1
            return_code = process.wait()
            if return_code != 0 or decoded != len(observed_times):
                stderr.seek(0)
                detail = stderr.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    "shot candidate decoding failed" + (f": {detail}" if detail else "")
                )
    except OSError as exc:
        raise RuntimeError("unable to run ffmpeg for shot candidate detection") from exc

    strongest = sorted(scored, key=lambda item: (-item[1], item[0]))[:_MAX_SHOT_CUTS]
    cuts: list[tuple[float, float]] = []
    for time, score in sorted(strongest):
        if not cuts or time - cuts[-1][0] >= 0.25:
            cuts.append((time, score))
    score_by_cut = {time: score for time, score in cuts}
    boundaries = [0.0, *(time for time, _ in cuts), observed_end]
    intervals = [
        {
            "start": boundaries[index],
            "end": boundaries[index + 1],
            "confidence": score_by_cut.get(boundaries[index], 0.0),
        }
        for index in range(len(boundaries) - 1)
        if boundaries[index + 1] > boundaries[index]
    ]
    boundary_frames: set[float] = set()
    for time, _ in cuts:
        right = bisect.bisect_left(frame_times, time)
        if right:
            candidate = frame_times[right - 1]
            if candidate < observed_end:
                boundary_frames.add(candidate)
        if right < len(frame_times) and frame_times[right] < observed_end:
            boundary_frames.add(frame_times[right])
    return intervals, sorted(boundary_frames)


def _av_sync(info: MediaInfo) -> dict[str, Any]:
    if not info.has_audio:
        return {"status": "not_applicable", "audio_start_offset": None}
    if info.audio_start_offset is None:
        return {"status": "unknown", "audio_start_offset": None}
    return {"status": "measured", "audio_start_offset": info.audio_start_offset}


def _copy_exclusive(source: Path, destination: Path) -> None:
    with source.open("rb") as reader, destination.open("xb") as writer:
        shutil.copyfileobj(reader, writer, length=1024 * 1024)
        writer.flush()
        os.fsync(writer.fileno())
    if digest(source) != digest(destination):
        destination.unlink(missing_ok=True)
        raise RuntimeError("full media copy failed integrity verification")


def prepare_analysis_media(
    evidence_dir: Path,
    destination: Path,
    mode: Literal["full", "opening"] | str,
) -> dict[str, Any]:
    """Prepare explicit model inputs from a validated evidence directory.

    ``full`` returns an unchanged copy of the complete source and observes its
    whole timeline, refusing sources above the 12 MiB inline budget. ``opening``
    returns a continuous, audio-preserving 0..min(12s, duration) MP4 plus up to
    24 dense, actual-PTS frames from the first three seconds. OpenCV-derived shot
    candidates and their actual boundary frames are limited to the same declared
    observed interval; no later source content is claimed as observed.
    """

    if mode not in {"full", "opening"}:
        raise ValueError("analysis mode must be 'full' or 'opening'")
    evidence_dir = evidence_dir.expanduser().resolve(strict=True)
    manifest = validate_evidence(evidence_dir)
    source = _source(manifest, evidence_dir)
    destination = _empty_directory(destination)
    info = manifest.media
    observed_end = info.duration if mode == "full" else min(12.0, info.duration)

    try:
        shots, boundary_times = _shot_candidates(source, info.frame_times, observed_end)
        media_inputs: list[dict[str, Any]]
        frames: list[dict[str, Any]]
        if mode == "full":
            source_record = next(item for item in manifest.files if item.kind == "source")
            if source_record.size > _FULL_INLINE_BYTES:
                raise ValueError("full media exceeds the 12 MiB inline request budget")
            copied = destination / f"full{source.suffix or '.bin'}"
            _copy_exclusive(source, copied)
            media_inputs = [
                {
                    "path": str(copied.resolve()),
                    "mime_type": _mime_type(copied),
                    "start": 0.0,
                    "end": info.duration,
                    "kind": "full_video",
                }
            ]
            frames = [
                {
                    "path": str((evidence_dir / item.path).resolve(strict=True)),
                    "time": item.time,
                }
                for item in manifest.files
                if item.kind == "frame" and item.time is not None
            ]
        else:
            clip = extract_clip(source, destination / "opening.mp4", 0.0, observed_end, media=info)
            clip["kind"] = "opening_clip"
            media_inputs = [clip]
            dense = _dense_times(info.frame_times, observed_end)
            dense_dir = destination / "opening_frames"
            frames = extract_frames(source, dense_dir, dense, frame_times=info.frame_times)

        existing_times = {float(item["time"]) for item in frames}
        additional = [time for time in boundary_times if time not in existing_times]
        if additional:
            boundary_dir = destination / "boundary_frames"
            frames.extend(
                extract_frames(source, boundary_dir, additional, frame_times=info.frame_times)
            )
        frames.sort(key=lambda item: float(item["time"]))
        return {
            "media_inputs": media_inputs,
            "frame_paths": frames,
            "shot_candidates": shots,
            "observed_intervals": [{"start": 0.0, "end": observed_end}],
            "av_sync": _av_sync(info),
        }
    except Exception:
        for child in destination.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        raise
