from __future__ import annotations

import hashlib
import json
import socket
from pathlib import Path
from typing import Any

import pytest

import video_scope.download as download_module
import video_scope.samples as samples_module
from video_scope.catalog import build_catalog
from video_scope.download import DownloadError
from video_scope.jobs import Store
from video_scope.samples import select_samples


def _catalog(store: Store, rows: list[dict[str, Any]], gold: list[dict[str, Any]]) -> Path:
    snapshot = store.root / "sources" / "snapshot-test"
    snapshot.mkdir(parents=True)
    (snapshot / "video.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    (snapshot / "gold3s.json").write_text(json.dumps(gold, ensure_ascii=False), encoding="utf-8")
    (snapshot / "golden3s_groups.json").write_text("[]", encoding="utf-8")
    catalog = snapshot / "catalog.sqlite3"
    build_catalog(snapshot, catalog)
    return catalog


def _row(identity: str, url: str, duration: int, category: str) -> dict[str, Any]:
    return {
        "视频ID": identity,
        "视频播放地址": url,
        "视频时长": duration,
        "文件大小": "2MB",
        "有音轨": True,
        "分类": category,
    }


def _fake_download(url: str, destination: Path, **_: Any) -> dict[str, Any]:
    payload = ("video:" + url.rsplit("/", 1)[-1]).encode()
    destination.write_bytes(payload)
    source_hash = hashlib.sha256(payload).hexdigest()
    return {
        "path": str(destination.resolve()),
        "source_sha256": source_hash,
        "size": len(payload),
        "duration": 24.0,
        "has_audio": True,
        "media": {"width": 640, "height": 360},
    }


def test_select_samples_builds_fixed_distinct_offline_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "store")
    catalog = _catalog(
        store,
        [
            _row("full-a", "https://media.example/full-a.mp4?secret=one", 20, "beauty"),
            _row("full-b", "https://media.example/full-b.mp4?secret=two", 25, "food"),
            _row("full-c", "https://media.example/full-c.mp4?secret=three", 30, "beauty"),
        ],
        [
            _row("open-a", "https://media.example/open-a.mp4?secret=four", 18, "sports"),
            _row("open-b", "https://media.example/open-b.mp4?secret=five", 22, "travel"),
            _row("open-c", "https://media.example/open-c.mp4?secret=six", 26, "sports"),
        ],
    )
    calls: list[str] = []

    def download(url: str, destination: Path, **kwargs: Any) -> dict[str, Any]:
        calls.append(url)
        return _fake_download(url, destination, **kwargs)

    monkeypatch.setattr(samples_module, "download_media", download)
    campaign = tmp_path / "campaign"

    selected = select_samples(store, {"catalog": str(catalog)}, campaign)

    assert len(selected) == 4
    assert [item["mode"] for item in selected].count("full") == 2
    assert [item["mode"] for item in selected].count("opening") == 2
    assert len({item["identity"] for item in selected}) == 4
    assert len({item["source_sha256"] for item in selected}) == 4
    assert len(calls) == 4
    for item in selected:
        source = Path(item["path"])
        assert source.is_file()
        assert source.parent.parent == campaign / "downloads"
        assert hashlib.sha256(source.read_bytes()).hexdigest() == item["source_sha256"]
    serialized = (campaign / "samples.json").read_text(encoding="utf-8")
    assert "https://" not in serialized
    assert "secret=" not in serialized

    monkeypatch.setattr(
        samples_module,
        "download_media",
        lambda *_args, **_kwargs: pytest.fail("existing samples must not download again"),
    )
    assert select_samples(store, {"catalog": str(catalog)}, campaign) == selected


def test_failed_signed_url_is_recorded_without_url_and_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "store")
    catalog = _catalog(
        store,
        [
            _row("full-a", "https://media.example/expired.mp4?token=private", 20, "a"),
            _row("full-b", "https://media.example/full-b.mp4", 21, "b"),
            _row("full-c", "https://media.example/full-c.mp4", 22, "c"),
        ],
        [
            _row("open-a", "https://media.example/open-a.mp4", 20, "d"),
            _row("open-b", "https://media.example/open-b.mp4", 20, "e"),
        ],
    )
    attempted: list[str] = []

    def download(url: str, destination: Path, **kwargs: Any) -> dict[str, Any]:
        attempted.append(url)
        if "expired" in url:
            raise DownloadError("media server returned HTTP status 403")
        return _fake_download(url, destination, **kwargs)

    monkeypatch.setattr(samples_module, "download_media", download)
    campaign = tmp_path / "campaign"

    selected = select_samples(store, {"catalog": str(catalog)}, campaign)

    assert len(selected) == 4
    assert "full-a" not in {item["identity"] for item in selected}
    rejections_text = (campaign / "rejections.json").read_text(encoding="utf-8")
    rejections = json.loads(rejections_text)
    assert rejections == [
        {
            "sourcefile": "video.json",
            "row": 0,
            "identity": "vid:full-a",
            "mode": "full",
            "reason": "media server returned HTTP status 403",
        }
    ]
    assert "https://" not in rejections_text
    assert "token=private" not in rejections_text
    assert len(attempted) == 5


def test_existing_samples_hash_mismatch_fails_without_reselection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path / "store")
    catalog = _catalog(
        store,
        [
            _row("full-a", "https://media.example/a.mp4", 20, "a"),
            _row("full-b", "https://media.example/b.mp4", 20, "b"),
        ],
        [
            _row("open-a", "https://media.example/c.mp4", 20, "c"),
            _row("open-b", "https://media.example/d.mp4", 20, "d"),
        ],
    )
    monkeypatch.setattr(samples_module, "download_media", _fake_download)
    campaign = tmp_path / "campaign"
    selected = select_samples(store, {"catalog": str(catalog)}, campaign)
    Path(selected[0]["path"]).write_bytes(b"tampered")
    monkeypatch.setattr(
        samples_module,
        "download_media",
        lambda *_args, **_kwargs: pytest.fail("invalid existing manifest must not reselect"),
    )

    with pytest.raises(ValueError, match="hash"):
        select_samples(store, {"catalog": str(catalog)}, campaign)


def test_download_url_validation_rejects_credentials_and_nonpublic_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(DownloadError, match="HTTP or HTTPS"):
        download_module._checked_url("file:///tmp/video.mp4")
    with pytest.raises(DownloadError, match="user information"):
        download_module._checked_url("https://user:secret@media.example/video.mp4")

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 443))
        ],
    )
    with pytest.raises(DownloadError, match="public"):
        download_module._checked_url("https://media.example/video.mp4")
