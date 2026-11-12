import json
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from video_scope.cli import app
from video_scope.evidence import validate_evidence
from video_scope.jobs import Store, export_evidence, read_batch


@pytest.fixture
def video(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg unavailable")
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=10:duration=1",
            "-c:v",
            "mpeg4",
            str(path),
        ],
        check=True,
    )
    return path


def test_prepare_resume_export(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    identifier = store.create(video)
    first = store.run(identifier)
    assert first["state"] == "prepared"
    assert first["supplier_calls"] == 0
    evidence = store.root / first["evidence"]
    manifest = validate_evidence(evidence)
    assert manifest.status == "prepared_not_analyzed"
    before = (evidence / "evidence.json").stat().st_mtime_ns
    video.unlink()
    assert store.run(identifier)["state"] == "prepared"
    assert (evidence / "evidence.json").stat().st_mtime_ns == before
    output = tmp_path / "exported"
    export_evidence(store, identifier, output)
    assert validate_evidence(output).source_id == manifest.source_id
    with pytest.raises(ValueError, match="already exists"):
        export_evidence(store, identifier, output)
    assert store.status(identifier)["state"] == "prepared"


def test_changed_input_fails(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    identifier = store.create(video)
    video.write_bytes(b"changed")
    with pytest.raises(ValueError, match="source changed"):
        store.run(identifier)
    assert store.status(identifier)["state"] == "failed"


def test_corrupt_cache_not_reused(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    identifier = store.create(video)
    job = store.run(identifier)
    evidence = store.root / job["evidence"]
    manifest = validate_evidence(evidence)
    (evidence / manifest.files[0].path).write_bytes(b"broken")
    with pytest.raises(ValueError, match="integrity"):
        store.run(identifier)
    assert store.status(identifier)["state"] == "failed"


def test_path_traversal_rejected(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    job = store.run(store.create(video))
    evidence = store.root / job["evidence"]
    path = evidence / "evidence.json"
    payload = json.loads(path.read_text())
    payload["files"][0]["path"] = "../escape.mp4"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="relative POSIX"):
        validate_evidence(evidence)


def test_symlink_rejected(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    job = store.run(store.create(video))
    evidence = store.root / job["evidence"]
    manifest = validate_evidence(evidence)
    source = next(item for item in manifest.files if item.kind == "source")
    path = evidence / source.path
    path.unlink()
    path.symlink_to(video)
    with pytest.raises(ValueError, match="symlink"):
        validate_evidence(evidence)


def test_batch_isolates_bad_rows(
    video: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "video_scope.cli.sync_sources",
        lambda *args, **kwargs: {"status": "test", "commit": "a" * 40, "snapshot": "fixture"},
    )
    batch = tmp_path / "batch.jsonl"
    batch.write_text("not json\n" + json.dumps({"source": video.name}) + "\n")
    entries = read_batch(batch)
    assert "error" in entries[0]
    assert entries[1]["source"] == video
    result = CliRunner().invoke(
        app, ["prepare-batch", str(batch), "--data-dir", str(tmp_path / "runtime")]
    )
    assert result.exit_code == 1
    assert '"state": "prepared"' in result.stdout


def test_code_directory_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(ValueError, match="outside Git"):
        Store(repo / "data")


def test_lock_excludes_second_scheduler(tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    with store.lock(), pytest.raises(ValueError, match="another preparation"):
        with store.lock():
            pass


def test_failed_export_keeps_prepared_task(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    identifier = store.create(video)
    store.run(identifier)
    result = CliRunner().invoke(
        app,
        [
            "export-evidence",
            identifier,
            "--output",
            str(tmp_path / "out"),
            "--data-dir",
            str(store.root),
        ],
    )
    assert result.exit_code == 1
    assert not (tmp_path / "out").exists()
    assert store.status(identifier)["state"] == "prepared"


def test_interrupted_state_resumes(video: Path, tmp_path: Path) -> None:
    store = Store(tmp_path / "runtime")
    identifier = store.create(video)
    store.update(identifier, "preparing", None)
    assert store.run(identifier)["state"] == "prepared"


@pytest.mark.parametrize("time", [-1.0, 99.0, 0.012345, float("nan")])
def test_invalid_frame_time_rejected(video: Path, tmp_path: Path, time: float) -> None:
    store = Store(tmp_path / "runtime")
    job = store.run(store.create(video))
    evidence = store.root / job["evidence"]
    path = evidence / "evidence.json"
    payload = json.loads(path.read_text())
    frame = next(item for item in payload["files"] if item["kind"] == "frame")
    frame["time"] = time
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        validate_evidence(evidence)
