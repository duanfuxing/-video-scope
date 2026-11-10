from __future__ import annotations

import hashlib
import json
import subprocess
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

import video_scope.media as media_module
from video_scope.media import MediaInfo, prepare, probe


def _run(*command: str) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


def _make_av(path: Path) -> None:
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x90:rate=30:duration=2",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=2",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-vf",
        "select=not(mod(n\\,4))+not(mod(n\\,7))",
        "-fps_mode:v:0",
        "vfr",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-output_ts_offset",
        "3",
        str(path),
    )


def _make_video(path: Path) -> None:
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=96x64:rate=5:duration=1.4",
        "-an",
        "-c:v",
        "mpeg4",
        str(path),
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ffprobe_times(path: Path) -> list[float]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    frames = json.loads(result.stdout)["frames"]
    raw = [float(frame["best_effort_timestamp_time"]) for frame in frames]
    origin = min(raw)
    return sorted({round(value - origin, 9) for value in raw})


def test_probe_uses_real_pts_and_normalizes_nonzero_start(tmp_path: Path) -> None:
    source = tmp_path / "offset-av.mp4"
    _make_av(source)

    info = probe(source)

    assert info.width == 160
    assert info.height == 90
    assert info.has_audio is True
    assert info.rotation == 0
    assert info.duration == pytest.approx(2.0, abs=0.15)
    assert info.video_start_time == pytest.approx(3.0, abs=1e-6)
    assert info.audio_start_offset == pytest.approx(-0.022, abs=1e-6)
    assert info.frame_times == pytest.approx(_ffprobe_times(source), abs=1e-9)
    assert info.frame_times[0] == 0.0
    assert len(info.frame_times) > 12
    intervals = {
        round(current - previous, 6)
        for previous, current in zip(info.frame_times, info.frame_times[1:], strict=False)
    }
    assert len(intervals) > 1
    with pytest.raises(ValidationError):
        MediaInfo.model_validate({**info.model_dump(), "unexpected": True})


def test_media_info_rejects_nonfinite_or_invalid_timelines() -> None:
    valid = {
        "duration": 1.0,
        "width": 16,
        "height": 16,
        "has_audio": False,
        "rotation": 0,
        "frame_times": [0.0, 0.5],
    }
    for duration in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            MediaInfo.model_validate({**valid, "duration": duration})
    for frame_times in ([0.0, float("nan")], [-0.1, 0.5], [0.5, 0.4]):
        with pytest.raises(ValidationError):
            MediaInfo.model_validate({**valid, "frame_times": frame_times})
    with pytest.raises(ValidationError):
        MediaInfo.model_validate({**valid, "duration": 0.5})


def test_prepare_copies_hashes_and_extracts_bounded_real_evidence(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    destination = tmp_path / "evidence"
    destination.mkdir()
    _make_av(source)

    result = prepare(source, destination)

    assert result["media"] == probe(source).model_dump()
    source_records = [item for item in result["files"] if item["kind"] == "source"]
    frame_records = [item for item in result["files"] if item["kind"] == "frame"]
    audio_records = [item for item in result["files"] if item["kind"] == "audio"]

    assert len(source_records) == 1
    assert source_records[0]["path"] == "source.mp4"
    assert source_records[0]["sha256"] == _sha256(source)
    assert _sha256(destination / "source.mp4") == _sha256(source)
    assert source_records[0]["size"] == source.stat().st_size
    assert "time" not in source_records[0]

    assert 1 <= len(frame_records) <= 12
    assert frame_records[0]["time"] == result["media"]["frame_times"][0]
    assert frame_records[-1]["time"] == result["media"]["frame_times"][-1]
    assert [item["time"] for item in frame_records] == sorted(
        item["time"] for item in frame_records
    )

    assert len(audio_records) == 1
    assert audio_records[0]["path"] == "audio.wav"
    assert "time" not in audio_records[0]
    with wave.open(str(destination / "audio.wav"), "rb") as audio:
        assert audio.getframerate() == 16000
        assert audio.getnchannels() == 1
        assert audio.getsampwidth() == 2
        assert audio.getnframes() > 0

    for record in result["files"]:
        output = destination / record["path"]
        assert output.is_file()
        assert record["sha256"] == _sha256(output)
        assert record["size"] == output.stat().st_size


def test_prepare_uses_immutable_copy_when_original_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "changing.mp4"
    destination = tmp_path / "evidence"
    destination.mkdir()
    _make_video(source)
    original_hash = _sha256(source)
    real_copy = media_module._copy_and_hash

    def copy_then_change(source_path: Path, output: Path) -> tuple[str, int]:
        result = real_copy(source_path, output)
        source_path.write_bytes(b"changed after evidence copy")
        return result

    monkeypatch.setattr(media_module, "_copy_and_hash", copy_then_change)

    result = prepare(source, destination)

    source_record = next(item for item in result["files"] if item["kind"] == "source")
    assert source_record["sha256"] == original_hash
    assert _sha256(destination / "source.mp4") == original_hash
    assert any(item["kind"] == "frame" for item in result["files"])


def test_probe_rejects_local_hls_manifest(tmp_path: Path) -> None:
    playlist = tmp_path / "playlist.m3u8"
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=64x48:rate=5:duration=1",
        "-c:v",
        "mpeg2video",
        "-f",
        "hls",
        "-hls_time",
        "0.25",
        "-hls_list_size",
        "0",
        str(playlist),
    )
    disguised = tmp_path / "local-video.bin"
    playlist.rename(disguised)

    with pytest.raises(ValueError, match="playlist|manifest"):
        probe(disguised)


def test_prepare_video_without_audio_omits_wav(tmp_path: Path) -> None:
    source = tmp_path / "silent.mp4"
    destination = tmp_path / "evidence"
    destination.mkdir()
    _make_video(source)

    result = prepare(source, destination)

    assert result["media"]["has_audio"] is False
    assert not (destination / "audio.wav").exists()
    assert all(item["kind"] != "audio" for item in result["files"])


def test_probe_reports_rotation_metadata(tmp_path: Path) -> None:
    base = tmp_path / "base.mp4"
    rotated = tmp_path / "rotated.mp4"
    _make_video(base)
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-display_rotation:v:0",
        "90",
        "-i",
        str(base),
        "-c",
        "copy",
        str(rotated),
    )

    info = probe(rotated)

    assert info.rotation == 90
    assert (info.width, info.height) == (96, 64)


@pytest.mark.parametrize("kind", ["missing", "directory", "text", "audio"])
def test_probe_rejects_non_video_inputs(tmp_path: Path, kind: str) -> None:
    source = tmp_path / kind
    if kind == "directory":
        source.mkdir()
    elif kind == "text":
        source.write_text("not media", encoding="utf-8")
    elif kind == "audio":
        source = source.with_suffix(".wav")
        _run(
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=220:duration=0.5",
            str(source),
        )

    with pytest.raises((ValueError, RuntimeError)):
        probe(source)


def test_prepare_requires_new_empty_directory(tmp_path: Path) -> None:
    source = tmp_path / "input.mp4"
    _make_video(source)

    missing = tmp_path / "missing"
    with pytest.raises((ValueError, FileNotFoundError)):
        prepare(source, missing)

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep").write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="empty"):
        prepare(source, nonempty)
    assert (nonempty / "keep").read_text(encoding="utf-8") == "x"
