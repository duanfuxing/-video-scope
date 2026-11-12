import hashlib
import io
import json
import subprocess
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest
from typer.testing import CliRunner

from video_scope import remote
from video_scope.cli import app
from video_scope.jobs import Store


class Response(io.BytesIO):
    def __init__(self, data: bytes) -> None:
        super().__init__(data)
        self.headers = {
            "ETag": '"' + hashlib.sha256(data).hexdigest() + '"',
            "Content-Length": str(len(data)),
        }


class FakeGitHub:
    def __init__(self) -> None:
        self.commit = "a" * 40
        self.data = {
            "video.json": json.dumps([{"视频ID": "v1", "视频名称": "one"}]).encode(),
            "gold3s.json": json.dumps([{"视频ID": "v1", "前3秒台词": "hello"}]).encode(),
            "golden3s_groups.json": b"[]",
        }
        self.requests: list[str] = []
        self.fail_name: str | None = None
        self.truncate_name: str | None = None
        self.rate_limited = False

    def open(self, request: Request, timeout: int) -> io.BytesIO:
        assert timeout == 60
        url = request.full_url
        self.requests.append(("HEAD " if request.get_method() == "HEAD" else "") + url)
        if self.rate_limited and "api.github.com" in url:
            raise HTTPError(url, 403, "rate limit exceeded", Message(), None)
        if self.fail_name and self.fail_name in url:
            raise URLError("offline")
        if "/git/ref/heads/" in url:
            payload = json.dumps({"object": {"sha": self.commit}}).encode()
        elif "/contents/static/data" in url:
            assert url.endswith(f"ref={self.commit}")
            payload = json.dumps(
                [
                    {"name": name, "sha": self.blob(data), "size": len(data), "type": "file"}
                    for name, data in self.data.items()
                ]
            ).encode()
        else:
            assert f"/{self.commit}/static/data/" in url
            name = url.rsplit("/", 1)[-1]
            payload = self.data[name]
            if self.truncate_name == name:
                payload = payload[:-1]
        return Response(payload)

    @staticmethod
    def blob(data: bytes) -> str:
        return hashlib.sha1(
            f"blob {len(data)}\0".encode() + data, usedforsecurity=False
        ).hexdigest()

    def downloads(self) -> list[str]:
        return [
            url.rsplit("/", 1)[-1]
            for url in self.requests
            if "raw.githubusercontent.com" in url and not url.startswith("HEAD ")
        ]


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    github = FakeGitHub()
    monkeypatch.setattr(remote, "urlopen", github.open)
    return github


def test_initial_sync_and_no_redownload(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    assert first["status"] == "updated"
    assert set(github.downloads()) == set(remote.FILES)
    assert first["counts"]["unique_videos"] == 1
    github.requests.clear()
    second = remote.sync_sources(store)
    assert second["status"] == "unchanged"
    assert second["snapshot"] == first["snapshot"]
    assert not github.downloads()
    assert len(github.requests) == 2


def test_only_changed_file_downloaded(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    github.commit = "b" * 40
    github.data["gold3s.json"] = json.dumps([{"视频ID": "v2"}]).encode()
    github.requests.clear()
    second = remote.sync_sources(store)
    assert github.downloads() == ["gold3s.json"]
    assert second["commit"] == github.commit
    assert second["counts"]["unique_videos"] == 2
    assert Path(first["snapshot"]).is_dir()
    assert second["snapshot"] != first["snapshot"]


def test_unrelated_commit_reuses_snapshot(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    github.commit = "c" * 40
    github.requests.clear()
    second = remote.sync_sources(store)
    assert second["snapshot"] == first["snapshot"]
    assert second["commit"] == github.commit
    assert github.downloads() == []


@pytest.mark.parametrize("failure", ["network", "truncated", "invalid_json", "wrong_shape"])
def test_failed_update_keeps_old_snapshot(github: FakeGitHub, tmp_path: Path, failure: str) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    pointer = store.root / "sources/current.json"
    before = pointer.read_bytes()
    github.commit = "b" * 40
    github.data["video.json"] = b'[{"video": "changed"}]'
    if failure == "network":
        github.fail_name = "video.json"
    elif failure == "truncated":
        github.truncate_name = "video.json"
    elif failure == "invalid_json":
        github.data["video.json"] = b"not json"
    else:
        github.data["video.json"] = b"{}"
    with pytest.raises((ValueError, RuntimeError)):
        remote.sync_sources(store)
    assert pointer.read_bytes() == before
    assert remote.sync_sources(store, offline=True)["snapshot"] == first["snapshot"]
    assert len(list((store.root / "sources").glob("snapshot-*"))) == 1


def test_offline_explicit_and_requires_intact_cache(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    with pytest.raises(ValueError, match="intact"):
        remote.sync_sources(store, offline=True)
    first = remote.sync_sources(store)
    github.requests.clear()
    assert remote.sync_sources(store, offline=True)["status"] == "offline"
    assert github.requests == []
    (Path(first["snapshot"]) / "video.json").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="intact"):
        remote.sync_sources(store, offline=True)
    assert github.requests == []
    repaired = remote.sync_sources(store)
    assert repaired["downloaded"] == ["video.json"]


def test_corrupt_catalog_rebuilt_without_download(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    Path(first["catalog"]).write_bytes(b"bad database")
    github.requests.clear()
    second = remote.sync_sources(store)
    assert second["status"] == "updated"
    assert second["downloaded"] == []
    assert github.downloads() == []
    assert remote.sync_sources(store, offline=True)["status"] == "offline"


def test_remote_failure_is_not_silent_fallback(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    remote.sync_sources(store)
    github.fail_name = "/git/ref/"
    with pytest.raises(RuntimeError, match="metadata request failed"):
        remote.sync_sources(store)
    assert remote.sync_sources(store, offline=True)["status"] == "offline"


def test_sync_cli(github: FakeGitHub, tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["sync-sources", "--data-dir", str(tmp_path / "data")])
    assert result.exit_code == 0, result.output
    output = json.loads(result.stdout)
    assert output["counts"]["unique_videos"] == 1


def test_prepare_stops_before_task_on_sync_failure(
    github: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github.fail_name = "/git/ref/"
    created = False

    def create(*args: Any) -> str:
        nonlocal created
        created = True
        return "task"

    monkeypatch.setattr(Store, "create", create)
    result = CliRunner().invoke(
        app, ["prepare", "missing.mp4", "--data-dir", str(tmp_path / "data")]
    )
    assert result.exit_code == 1
    assert not created


def test_remote_missing_file_keeps_previous_snapshot(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    remote.sync_sources(store)
    pointer = store.root / "sources/current.json"
    before = pointer.read_bytes()
    del github.data["gold3s.json"]
    with pytest.raises(ValueError, match="all three"):
        remote.sync_sources(store)
    assert pointer.read_bytes() == before


def test_corrupt_pointer_cannot_escape_sources(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    remote.sync_sources(store)
    pointer = store.root / "sources/current.json"
    payload = json.loads(pointer.read_text())
    payload["directory"] = "../../elsewhere"
    pointer.write_text(json.dumps(payload))
    github.requests.clear()
    with pytest.raises(ValueError):
        remote.sync_sources(store, offline=True)
    assert github.requests == []


def test_old_catalog_version_rebuilds_without_download(github: FakeGitHub, tmp_path: Path) -> None:
    store = Store(tmp_path / "data")
    first = remote.sync_sources(store)
    pointer = store.root / "sources/current.json"
    payload = json.loads(pointer.read_text())
    payload["catalog_version"] = "old-version"
    pointer.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="intact"):
        remote.sync_sources(store, offline=True)
    github.requests.clear()
    second = remote.sync_sources(store)
    assert second["status"] == "updated"
    assert second["snapshot"] != first["snapshot"]
    assert github.downloads() == []


def test_api_limit_uses_pinned_git_and_etags(
    github: FakeGitHub, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def git(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert args[0][1:3] == ["ls-remote", "--refs"]
        return subprocess.CompletedProcess(args[0], 0, f"{github.commit}\trefs/heads/main\n", "")

    monkeypatch.setattr("video_scope.remote.subprocess.run", git)
    store = Store(tmp_path / "data")
    github.rate_limited = True
    first = remote.sync_sources(store)
    assert first["version_check"] == "git-etag"
    assert set(github.downloads()) == set(remote.FILES)
    github.requests.clear()
    second = remote.sync_sources(store)
    assert second["status"] == "unchanged"
    assert github.downloads() == []
    github.rate_limited = False
    github.requests.clear()
    third = remote.sync_sources(store)
    assert third["status"] == "unchanged"
    assert third["version_check"] == "github-api"
    assert third["snapshot"] == first["snapshot"]
    assert github.downloads() == []
    github.rate_limited = True
    github.requests.clear()
    assert remote.sync_sources(store)["status"] == "unchanged"
    assert github.downloads() == []
