import json
import sqlite3
from pathlib import Path
from typing import Any, cast

import pytest

from video_scope.catalog import build_catalog


def _write_snapshot(
    root: Path,
    *,
    video: list[object] | None = None,
    gold3s: list[object] | None = None,
    groups: list[object] | None = None,
) -> Path:
    root.mkdir()
    payloads = {
        "video.json": [] if video is None else video,
        "gold3s.json": [] if gold3s is None else gold3s,
        "golden3s_groups.json": [] if groups is None else groups,
    }
    for name, payload in payloads.items():
        (root / name).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return root


def _fetch_one(db: sqlite3.Connection, sql: str, parameter: str) -> sqlite3.Row:
    row = db.execute(sql, (parameter,)).fetchone()
    assert row is not None
    return cast("sqlite3.Row", row)


def test_build_catalog_deduplicates_metadata_and_preserves_observations(tmp_path: Path) -> None:
    large_item_id = 9_007_199_254_740_993_123
    snapshot = _write_snapshot(
        tmp_path / "snapshot",
        video=[
            {
                "视频ID": "vid-a",
                "视频链接": "https://www.douyin.com/video/101",
                "视频名称": "old title",
                "视频播放地址": "https://media.example/old-signed",
                "采集时间": "2026-01-01 10:00:00",
                "抖音ItemID": large_item_id,
            },
            {
                "视频ID": "vid-a",
                "视频链接": "https://www.douyin.com/video/101",
                "视频名称": "new title",
                "视频播放地址": "https://media.example/new-signed",
                "采集时间": "2026-02-01 10:00:00",
            },
            {"视频ID": None, "视频链接": "https://www.douyin.com/video/101"},
            {"视频ID": "vid-b", "视频链接": "https://www.douyin.com/video/202"},
            {"视频ID": "vid-c", "视频链接": "https://www.douyin.com/video/202"},
            {"视频ID": None, "视频链接": "https://www.douyin.com/video/202"},
            {"视频ID": None, "视频链接": "https://www.douyin.com/video/303"},
            {"视频名称": "same", "抖音ItemID": "777"},
            {"视频名称": "same", "抖音ItemID": "777"},
        ],
        gold3s=[
            {
                "视频ID": "vid-a",
                "视频链接": "https://www.douyin.com/video/101",
                "视频播放地址": None,
                "采集时间": "2026-03-01 10:00:00",
            }
        ],
        groups=[
            {
                "sourceRecordId": "record-a",
                "sourceVid": "vid-a",
                "sourceMid": "material-not-an-identity",
                "industry": "beauty",
                "asr": "words",
                "structure": "structure",
                "top": [
                    {
                        "name": "b title",
                        "link": "https://www.douyin.com/video/202",
                        "vid": "vid-b",
                        "mid": "material-b",
                    },
                    {
                        "name": "top title",
                        "link": "https://www.douyin.com/video/404",
                        "vid": "vid-top",
                        "mid": "material-top",
                    },
                    {
                        "name": "no vid",
                        "link": "https://www.douyin.com/video/303",
                        "vid": "",
                        "mid": "material-303",
                    },
                ],
            }
        ],
    )
    output = tmp_path / "catalog.sqlite3"

    counts = build_catalog(snapshot, output)

    assert counts == {
        "source_records": 11,
        "candidate_count": 14,
        "unique_videos": 8,
        "duplicate_candidates": 6,
        "observations": 14,
        "unidentified_candidates": 2,
        "url_resolved_candidates": 1,
        "conflicted_candidates": 4,
        "conflicts": 2,
        "url_vid_conflicts": 1,
        "douyin_vid_conflicts": 1,
        "vid_douyin_conflicts": 0,
    }
    with sqlite3.connect(output) as db:
        db.row_factory = sqlite3.Row
        video_a = _fetch_one(db, "SELECT * FROM videos WHERE identity = ?", "vid:vid-a")
        assert dict(video_a) == {
            "identity": "vid:vid-a",
            "identity_kind": "vid",
            "video_id": "vid-a",
            "douyin_id": "101",
            "title": "new title",
            "playback_url": "https://media.example/new-signed",
            "playback_collected_at": "2026-02-01 10:00:00",
            "observation_count": 5,
        }
        assert db.execute("SELECT COUNT(*) FROM observations").fetchone()[0] == 14
        assert db.execute("SELECT COUNT(*) FROM source_records").fetchone()[0] == 11
        assert db.execute("SELECT COUNT(*) FROM conflicts").fetchone()[0] == 2
        roles = db.execute(
            """SELECT role, top_index, identity
               FROM observations
               WHERE file = 'golden3s_groups.json'
               ORDER BY id"""
        ).fetchall()
        assert [tuple(row) for row in roles] == [
            ("source", None, "vid:vid-a"),
            ("top", 0, "vid:vid-b"),
            ("top", 1, "vid:vid-top"),
            ("top", 2, "douyin:303"),
        ]
        unknown = db.execute(
            "SELECT identity FROM videos WHERE identity_kind = 'unidentified' ORDER BY identity"
        ).fetchall()
        assert len(unknown) == 2
        preserved = db.execute(
            """SELECT raw_json FROM observation_rows
               WHERE file = 'video.json' AND row_index = 0"""
        ).fetchone()
        assert preserved is not None
        assert json.loads(preserved["raw_json"])["抖音ItemID"] == large_item_id


def test_exact_url_is_required_to_attach_a_missing_id_to_a_vid(tmp_path: Path) -> None:
    snapshot = _write_snapshot(
        tmp_path / "snapshot",
        video=[
            {
                "视频ID": "known",
                "视频链接": "https://www.douyin.com/video/555?source=known",
            },
            {"视频链接": "https://www.douyin.com/video/555?source=other"},
        ],
    )
    output = tmp_path / "catalog.sqlite3"

    counts = build_catalog(snapshot, output)

    assert counts["unique_videos"] == 2
    assert counts["url_resolved_candidates"] == 0
    with sqlite3.connect(output) as db:
        identities = {row[0] for row in db.execute("SELECT identity FROM videos").fetchall()}
    assert identities == {"vid:known", "douyin:555"}


def test_empty_snapshot_is_valid(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path / "snapshot")
    output = tmp_path / "catalog.sqlite3"

    counts = build_catalog(snapshot, output)

    assert counts["candidate_count"] == 0
    assert counts["unique_videos"] == 0
    assert counts["duplicate_candidates"] == 0
    assert counts["observations"] == 0
    with sqlite3.connect(output) as db:
        assert db.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0


def test_existing_output_is_not_overwritten(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path / "snapshot")
    output = tmp_path / "catalog.sqlite3"
    output.write_bytes(b"keep me")

    with pytest.raises(ValueError, match="already exists"):
        build_catalog(snapshot, output)

    assert output.read_bytes() == b"keep me"


@pytest.mark.parametrize(
    ("file", "payload", "message"),
    [
        ("video.json", {}, "top level must be a list"),
        ("gold3s.json", [1], "row 0 must be an object"),
        ("golden3s_groups.json", [{}], "top must be a list"),
        ("golden3s_groups.json", [{"top": [1]}], r"top\[0\] must be an object"),
        ("video.json", [{"视频ID": 1.5}], "视频ID must be"),
        ("video.json", [{"视频链接": 123}], "视频链接 must be"),
    ],
)
def test_malformed_snapshot_is_rejected(
    tmp_path: Path, file: str, payload: Any, message: str
) -> None:
    snapshot = _write_snapshot(tmp_path / "snapshot")
    (snapshot / file).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    output = tmp_path / "catalog.sqlite3"

    with pytest.raises(ValueError, match=message):
        build_catalog(snapshot, output)

    assert not output.exists()


def test_duplicate_json_keys_are_rejected_without_losing_data(tmp_path: Path) -> None:
    snapshot = _write_snapshot(tmp_path / "snapshot")
    (snapshot / "video.json").write_text('[{"视频ID":"first","视频ID":"second"}]', encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        build_catalog(snapshot, tmp_path / "catalog.sqlite3")


@pytest.mark.parametrize(
    "link", ["未知", "https://www.douyin.com/", "https://example.com/video/123"]
)
def test_non_video_links_cannot_merge_missing_ids(tmp_path: Path, link: str) -> None:
    snapshot = _write_snapshot(
        tmp_path / "snapshot",
        video=[{"视频ID": "known", "视频链接": link}, {"视频链接": link}],
    )
    counts = build_catalog(snapshot, tmp_path / "catalog.sqlite3")
    assert counts["unique_videos"] == 2
    assert counts["unidentified_candidates"] == 1
