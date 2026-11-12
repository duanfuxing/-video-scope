from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from video_scope.evidence import EvidenceManifest, digest, write_manifest
from video_scope.media import prepare, probe
from video_scope.segments import extract_clip, extract_frames, prepare_analysis_media


def _run(*command: str) -> None:
    subprocess.run(command, check=True, capture_output=True, text=True)


@pytest.fixture
def evidence(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg") or importlib.util.find_spec("cv2") is None:
        pytest.skip("ffmpeg and OpenCV are required")
    source = tmp_path / "source.mp4"
    _run(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "color=red:size=160x90:rate=8:duration=6",
        "-f",
        "lavfi",
        "-i",
        "color=blue:size=160x90:rate=8:duration=7",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:sample_rate=48000:duration=13",
        "-filter_complex",
        "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map",
        "[v]",
        "-map",
        "2:a:0",
        "-c:v",
        "mpeg4",
        "-c:a",
        "aac",
        "-shortest",
        str(source),
    )
    directory = tmp_path / "evidence"
    directory.mkdir()
    payload = prepare(source, directory)
    manifest = EvidenceManifest(source_id=digest(source), **payload)
    write_manifest(directory, manifest)
    return directory


def test_opening_preparation_limits_observation_and_preserves_audio(
    evidence: Path, tmp_path: Path
) -> None:
    source = next(evidence.glob("source.*"))
    source_info = probe(source)
    destination = tmp_path / "opening-analysis"

    result = prepare_analysis_media(evidence, destination, "opening")

    assert set(result) == {
        "media_inputs",
        "frame_paths",
        "shot_candidates",
        "observed_intervals",
        "av_sync",
    }
    assert len(result["media_inputs"]) == 1
    media_input = result["media_inputs"][0]
    assert media_input["kind"] == "opening_clip"
    assert Path(media_input["path"]).is_absolute()
    assert media_input["mime_type"] == "video/mp4"
    assert media_input["start"] == 0.0
    assert media_input["end"] == 12.0
    assert probe(Path(media_input["path"])).has_audio is True
    assert result["observed_intervals"] == [{"start": 0.0, "end": 12.0}]
    assert result["av_sync"]["status"] in {"measured", "unknown"}

    dense = [
        item for item in result["frame_paths"] if Path(item["path"]).parent.name == "opening_frames"
    ]
    assert 1 <= len(dense) <= 24
    assert all(Path(item["path"]).is_absolute() for item in result["frame_paths"])
    assert all(item["time"] in source_info.frame_times for item in result["frame_paths"])
    assert all(item["time"] < 12.0 for item in result["frame_paths"])
    assert result["shot_candidates"]
    assert all(
        0 <= item["start"] < item["end"] <= 12.0 and 0 <= item["confidence"] <= 1
        for item in result["shot_candidates"]
    )
    assert any(
        Path(item["path"]).parent.name == "boundary_frames" for item in result["frame_paths"]
    )


def test_full_preparation_copies_complete_source_and_reports_whole_interval(
    evidence: Path, tmp_path: Path
) -> None:
    source = next(evidence.glob("source.*"))
    info = probe(source)

    result = prepare_analysis_media(evidence, tmp_path / "full-analysis", "full")

    media_input = result["media_inputs"][0]
    copied = Path(media_input["path"])
    assert media_input["kind"] == "full_video"
    assert copied.is_absolute()
    assert copied.read_bytes() == source.read_bytes()
    assert media_input["start"] == 0.0
    assert media_input["end"] == info.duration
    assert result["observed_intervals"] == [{"start": 0.0, "end": info.duration}]
    assert result["frame_paths"]
    assert all(item["time"] in info.frame_times for item in result["frame_paths"])


def test_public_frame_and_clip_extractors_use_real_pts_and_keep_audio(
    evidence: Path, tmp_path: Path
) -> None:
    source = next(evidence.glob("source.*"))
    info = probe(source)

    frames = extract_frames(source, tmp_path / "requested-frames", [0.13, 6.02])
    assert len(frames) == 2
    assert all(item["time"] in info.frame_times for item in frames)
    assert all(Path(item["path"]).is_file() for item in frames)

    clip = extract_clip(source, tmp_path / "extension.mp4", 5.0, 7.0)
    assert clip == {
        "path": str((tmp_path / "extension.mp4").resolve()),
        "mime_type": "video/mp4",
        "start": 5.0,
        "end": 7.0,
        "kind": "clip",
    }
    assert probe(Path(clip["path"])).has_audio is True


def test_invalid_mode_does_not_create_outputs(evidence: Path, tmp_path: Path) -> None:
    destination = tmp_path / "invalid"
    with pytest.raises(ValueError, match="mode"):
        prepare_analysis_media(evidence, destination, "later")
    assert not destination.exists()
