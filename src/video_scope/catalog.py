"""Build an offline, metadata-only video catalog from snapshot JSON files."""

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Never, cast
from urllib.parse import urlsplit

type JsonValue = str | int | float | bool | None | list[JsonValue] | dict[str, JsonValue]
type DateRank = tuple[int, str, int]

CATALOG_VERSION = "catalog-0.1.1"
_SOURCE_FILES = ("video.json", "gold3s.json", "golden3s_groups.json")


@dataclass(frozen=True, slots=True)
class _SourceRecord:
    identifier: int
    file: str
    row_index: int
    raw_json: str


@dataclass(slots=True)
class _Candidate:
    record_id: int
    file: str
    row_index: int
    role: str
    top_index: int | None
    video_id: str | None
    url: str | None
    douyin_id: str | None
    title: str | None
    playback_url: str | None
    collected_at: str | None
    sequence: int
    identity: str = ""
    resolution: str = ""
    conflict: bool = False


@dataclass(slots=True)
class _Video:
    identity: str
    identity_kind: str
    video_ids: set[str] = field(default_factory=set)
    douyin_ids: set[str] = field(default_factory=set)
    title: str | None = None
    title_rank: DateRank | None = None
    playback_url: str | None = None
    playback_collected_at: str | None = None
    playback_rank: DateRank | None = None
    observation_count: int = 0

    def add(self, candidate: _Candidate) -> None:
        self.observation_count += 1
        if candidate.video_id is not None:
            self.video_ids.add(candidate.video_id)
        if candidate.douyin_id is not None:
            self.douyin_ids.add(candidate.douyin_id)
        rank = _date_rank(candidate.collected_at, candidate.sequence)
        if candidate.title is not None and (self.title_rank is None or rank > self.title_rank):
            self.title = candidate.title
            self.title_rank = rank
        if candidate.playback_url is not None and (
            self.playback_rank is None or rank > self.playback_rank
        ):
            self.playback_url = candidate.playback_url
            self.playback_collected_at = candidate.collected_at
            self.playback_rank = rank


def _reject_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON number {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _load_rows(path: Path, name: str) -> list[dict[str, JsonValue]]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except OSError as exc:
        raise ValueError(f"{name}: unable to read required snapshot file: {exc}") from exc
    except (ValueError, UnicodeError) as exc:
        raise ValueError(f"{name}: invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise ValueError(f"{name}: top level must be a list")
    rows: list[dict[str, JsonValue]] = []
    for row_index, value in enumerate(payload):
        if not isinstance(value, dict):
            raise ValueError(f"{name}: row {row_index} must be an object")
        rows.append(cast("dict[str, JsonValue]", value))
    return rows


def _optional_id(value: JsonValue, location: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ValueError(f"{location} must be a string, integer, or null")
    result = str(value).strip()
    return result or None


def _optional_text(value: JsonValue, location: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{location} must be a string or null")
    result = value.strip()
    return result or None


def _douyin_id(url: str | None) -> str | None:
    if url is None:
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname not in {
        "douyin.com",
        "www.douyin.com",
    }:
        return None
    parts = parsed.path.rstrip("/").split("/")
    if len(parts) != 3 or parts[0] or parts[1] != "video" or not parts[2].isdigit():
        return None
    return parts[2]


def _date_rank(value: str | None, sequence: int) -> DateRank:
    if value is None:
        return (-1, "", sequence)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        normalized = parsed.astimezone(UTC).isoformat(timespec="microseconds")
        return (1, normalized, sequence)
    except ValueError:
        # Keep unusual supplier timestamps usable and deterministically ordered.
        return (0, value, sequence)


def _record_candidate(
    record_id: int,
    file: str,
    row_index: int,
    row: dict[str, JsonValue],
    sequence: int,
) -> _Candidate:
    prefix = f"{file}: row {row_index}"
    url = _optional_text(row.get("视频链接"), f"{prefix} 视频链接")
    return _Candidate(
        record_id=record_id,
        file=file,
        row_index=row_index,
        role="record",
        top_index=None,
        video_id=_optional_id(row.get("视频ID"), f"{prefix} 视频ID"),
        url=url,
        douyin_id=_douyin_id(url),
        title=_optional_text(row.get("视频名称"), f"{prefix} 视频名称"),
        playback_url=_optional_text(row.get("视频播放地址"), f"{prefix} 视频播放地址"),
        collected_at=_optional_text(row.get("采集时间"), f"{prefix} 采集时间"),
        sequence=sequence,
    )


def _group_candidates(
    record_id: int,
    row_index: int,
    row: dict[str, JsonValue],
    sequence: int,
) -> list[_Candidate]:
    file = "golden3s_groups.json"
    prefix = f"{file}: row {row_index}"
    if "top" not in row or not isinstance(row["top"], list):
        raise ValueError(f"{prefix} top must be a list")
    candidates = [
        _Candidate(
            record_id=record_id,
            file=file,
            row_index=row_index,
            role="source",
            top_index=None,
            video_id=_optional_id(row.get("sourceVid"), f"{prefix} sourceVid"),
            url=None,
            douyin_id=None,
            title=None,
            playback_url=None,
            collected_at=None,
            sequence=sequence,
        )
    ]
    for top_index, value in enumerate(row["top"]):
        if not isinstance(value, dict):
            raise ValueError(f"{prefix} top[{top_index}] must be an object")
        item = value
        item_prefix = f"{prefix} top[{top_index}]"
        url = _optional_text(item.get("link"), f"{item_prefix} link")
        candidates.append(
            _Candidate(
                record_id=record_id,
                file=file,
                row_index=row_index,
                role="top",
                top_index=top_index,
                video_id=_optional_id(item.get("vid"), f"{item_prefix} vid"),
                url=url,
                douyin_id=_douyin_id(url),
                title=_optional_text(item.get("name"), f"{item_prefix} name"),
                playback_url=None,
                collected_at=None,
                sequence=sequence + top_index + 1,
            )
        )
    return candidates


def _read_snapshot(snapshot: Path) -> tuple[list[_SourceRecord], list[_Candidate]]:
    records: list[_SourceRecord] = []
    candidates: list[_Candidate] = []
    sequence = 0
    for file in _SOURCE_FILES:
        rows = _load_rows(snapshot / file, file)
        for row_index, row in enumerate(rows):
            record_id = len(records) + 1
            records.append(
                _SourceRecord(
                    identifier=record_id,
                    file=file,
                    row_index=row_index,
                    raw_json=json.dumps(
                        row, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                    ),
                )
            )
            if file == "golden3s_groups.json":
                group_candidates = _group_candidates(record_id, row_index, row, sequence)
                candidates.extend(group_candidates)
                sequence += len(group_candidates)
            else:
                candidates.append(_record_candidate(record_id, file, row_index, row, sequence))
                sequence += 1
    return records, candidates


def _sets_by_key(
    candidates: list[_Candidate], key_name: str, value_name: str
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for candidate in candidates:
        key = cast("str | None", getattr(candidate, key_name))
        value = cast("str | None", getattr(candidate, value_name))
        if key is not None and value is not None:
            result.setdefault(key, set()).add(value)
    return result


def _resolve_candidates(
    candidates: list[_Candidate],
) -> tuple[dict[str, _Video], dict[str, dict[str, set[str]]], dict[str, int]]:
    vids_by_url = _sets_by_key(
        [candidate for candidate in candidates if candidate.douyin_id is not None],
        "url",
        "video_id",
    )
    vids_by_douyin = _sets_by_key(candidates, "douyin_id", "video_id")
    douyin_by_vid = _sets_by_key(candidates, "video_id", "douyin_id")
    conflict_maps = {
        "url_multiple_video_ids": {
            key: values for key, values in vids_by_url.items() if len(values) > 1
        },
        "douyin_id_multiple_video_ids": {
            key: values for key, values in vids_by_douyin.items() if len(values) > 1
        },
        "video_id_multiple_douyin_ids": {
            key: values for key, values in douyin_by_vid.items() if len(values) > 1
        },
    }
    unidentified = 0
    url_resolved = 0
    conflicted = 0
    videos: dict[str, _Video] = {}
    for candidate in candidates:
        url_vids = vids_by_url.get(candidate.url, set()) if candidate.url is not None else set()
        candidate.conflict = (
            len(url_vids) > 1
            or (
                candidate.douyin_id is not None
                and len(vids_by_douyin.get(candidate.douyin_id, set())) > 1
            )
            or (
                candidate.video_id is not None
                and len(douyin_by_vid.get(candidate.video_id, set())) > 1
            )
        )
        if candidate.conflict:
            conflicted += 1
        if candidate.video_id is not None:
            candidate.identity = f"vid:{candidate.video_id}"
            candidate.resolution = "video_id"
        elif len(url_vids) == 1:
            resolved_vid = next(iter(url_vids))
            candidate.identity = f"vid:{resolved_vid}"
            candidate.resolution = "exact_url_to_video_id"
            url_resolved += 1
        elif candidate.douyin_id is not None:
            candidate.identity = f"douyin:{candidate.douyin_id}"
            candidate.resolution = "douyin_url_conflict" if len(url_vids) > 1 else "douyin_url"
        else:
            top = "" if candidate.top_index is None else f":{candidate.top_index}"
            candidate.identity = (
                f"unidentified:{candidate.file}:{candidate.row_index}:{candidate.role}{top}"
            )
            candidate.resolution = (
                "unidentified_url_conflict" if len(url_vids) > 1 else "unidentified"
            )
            unidentified += 1
        identity_kind = candidate.identity.partition(":")[0]
        video = videos.setdefault(candidate.identity, _Video(candidate.identity, identity_kind))
        video.add(candidate)
    resolution_counts = {
        "unidentified_candidates": unidentified,
        "url_resolved_candidates": url_resolved,
        "conflicted_candidates": conflicted,
    }
    return videos, conflict_maps, resolution_counts


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        PRAGMA user_version = 1;

        CREATE TABLE catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE videos (
            identity TEXT PRIMARY KEY,
            identity_kind TEXT NOT NULL,
            video_id TEXT,
            douyin_id TEXT,
            title TEXT,
            playback_url TEXT,
            playback_collected_at TEXT,
            observation_count INTEGER NOT NULL CHECK (observation_count > 0)
        );

        CREATE TABLE source_records (
            id INTEGER PRIMARY KEY,
            file TEXT NOT NULL,
            row_index INTEGER NOT NULL CHECK (row_index >= 0),
            raw_json TEXT NOT NULL,
            UNIQUE (file, row_index)
        );

        CREATE TABLE observations (
            id INTEGER PRIMARY KEY,
            identity TEXT NOT NULL REFERENCES videos(identity),
            record_id INTEGER NOT NULL REFERENCES source_records(id),
            file TEXT NOT NULL,
            row_index INTEGER NOT NULL CHECK (row_index >= 0),
            role TEXT NOT NULL CHECK (role IN ('record', 'source', 'top')),
            top_index INTEGER,
            observed_video_id TEXT,
            observed_url TEXT,
            observed_douyin_id TEXT,
            observed_title TEXT,
            observed_playback_url TEXT,
            collected_at TEXT,
            resolution TEXT NOT NULL,
            identity_conflict INTEGER NOT NULL CHECK (identity_conflict IN (0, 1)),
            CHECK (
                (role = 'top' AND top_index IS NOT NULL AND top_index >= 0)
                OR (role <> 'top' AND top_index IS NULL)
            )
        );

        CREATE TABLE conflicts (
            kind TEXT NOT NULL,
            conflict_key TEXT NOT NULL,
            values_json TEXT NOT NULL,
            PRIMARY KEY (kind, conflict_key)
        );

        CREATE INDEX observations_identity_idx ON observations(identity);
        CREATE INDEX observations_source_idx ON observations(file, row_index);

        CREATE VIEW observation_rows AS
        SELECT observations.*, source_records.raw_json
        FROM observations
        JOIN source_records ON source_records.id = observations.record_id;
        """
    )


def _write_catalog(
    path: Path,
    records: list[_SourceRecord],
    candidates: list[_Candidate],
    videos: dict[str, _Video],
    conflict_maps: dict[str, dict[str, set[str]]],
) -> None:
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        with connection:
            connection.executemany(
                "INSERT INTO catalog_meta(key, value) VALUES (?, ?)",
                (
                    ("schema_version", CATALOG_VERSION),
                    ("deduplication", "metadata_identity_only"),
                    ("content_hash_deduplication", "not_performed"),
                ),
            )
            connection.executemany(
                """INSERT INTO videos(
                       identity, identity_kind, video_id, douyin_id, title,
                       playback_url, playback_collected_at, observation_count
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        video.identity,
                        video.identity_kind,
                        next(iter(video.video_ids)) if len(video.video_ids) == 1 else None,
                        next(iter(video.douyin_ids)) if len(video.douyin_ids) == 1 else None,
                        video.title,
                        video.playback_url,
                        video.playback_collected_at,
                        video.observation_count,
                    )
                    for identity, video in sorted(videos.items())
                ),
            )
            connection.executemany(
                "INSERT INTO source_records(id, file, row_index, raw_json) VALUES (?, ?, ?, ?)",
                (
                    (record.identifier, record.file, record.row_index, record.raw_json)
                    for record in records
                ),
            )
            connection.executemany(
                """INSERT INTO observations(
                       id, identity, record_id, file, row_index, role, top_index,
                       observed_video_id, observed_url, observed_douyin_id,
                       observed_title, observed_playback_url, collected_at,
                       resolution, identity_conflict
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    (
                        index,
                        candidate.identity,
                        candidate.record_id,
                        candidate.file,
                        candidate.row_index,
                        candidate.role,
                        candidate.top_index,
                        candidate.video_id,
                        candidate.url,
                        candidate.douyin_id,
                        candidate.title,
                        candidate.playback_url,
                        candidate.collected_at,
                        candidate.resolution,
                        int(candidate.conflict),
                    )
                    for index, candidate in enumerate(candidates, 1)
                ),
            )
            connection.executemany(
                "INSERT INTO conflicts(kind, conflict_key, values_json) VALUES (?, ?, ?)",
                (
                    (kind, key, json.dumps(sorted(values), ensure_ascii=False))
                    for kind, entries in sorted(conflict_maps.items())
                    for key, values in sorted(entries.items())
                ),
            )
    finally:
        connection.close()


def build_catalog(snapshot: Path, output: Path) -> dict[str, int]:
    """Create a new SQLite metadata catalog from a three-file JSON snapshot.

    Identity resolution prefers an explicit video id. A candidate without one is
    associated with an explicit id only when its exact URL has one unambiguous
    id; otherwise an exact Douyin ``/video/<digits>`` URL is its identity. A
    candidate with neither receives its own stable, source-position identity.
    No media is downloaded and no content-hash claims are made.
    """
    try:
        snapshot = snapshot.expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("snapshot must be an existing directory") from exc
    if not snapshot.is_dir():
        raise ValueError("snapshot must be a directory")
    output = output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError("catalog output already exists; it will not be overwritten")

    records, candidates = _read_snapshot(snapshot)
    videos, conflict_maps, resolution_counts = _resolve_candidates(candidates)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging_file = tempfile.NamedTemporaryFile(
        prefix=f".{output.name}-", suffix=".tmp", dir=output.parent, delete=False
    )
    staging = Path(staging_file.name)
    staging_file.close()
    try:
        _write_catalog(staging, records, candidates, videos, conflict_maps)
        try:
            os.link(staging, output)
        except FileExistsError as exc:
            raise ValueError("catalog output already exists; it will not be overwritten") from exc
    finally:
        staging.unlink(missing_ok=True)

    conflict_counts = {kind: len(entries) for kind, entries in conflict_maps.items()}
    counts = {
        "source_records": len(records),
        "candidate_count": len(candidates),
        "unique_videos": len(videos),
        "duplicate_candidates": len(candidates) - len(videos),
        "observations": len(candidates),
        **resolution_counts,
        "conflicts": sum(conflict_counts.values()),
        "url_vid_conflicts": conflict_counts["url_multiple_video_ids"],
        "douyin_vid_conflicts": conflict_counts["douyin_id_multiple_video_ids"],
        "vid_douyin_conflicts": conflict_counts["video_id_multiple_douyin_ids"],
    }
    return counts
