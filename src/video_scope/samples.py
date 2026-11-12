"""Select and locally materialize the four-video analysis campaign."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from video_scope.download import MAX_DOWNLOAD_BYTES, DownloadError, download_media
from video_scope.evidence import digest
from video_scope.jobs import Store

_SAMPLE_COUNT = 4
_MODE_COUNTS = {"full": 2, "opening": 2}
_MAX_CANDIDATE_METADATA = 40
_MAX_DOWNLOAD_ATTEMPTS = 12
_PREFERRED_BYTES = 12 * 1024 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class _Candidate:
    sourcefile: str
    row: int
    identity: str
    url: str
    duration: float | None
    size: int | None
    width: int | None
    height: int | None
    has_audio: bool | None
    category: str | None


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) and number >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    if ":" in text:
        try:
            parts = [float(part) for part in text.split(":")]
        except ValueError:
            return None
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        if len(parts) == 3:
            return parts[0] * 3600 + parts[1] * 60 + parts[2]
        return None
    match = re.search(r"[-+]?\d+(?:\.\d+)?", text.replace(",", ""))
    if match is None:
        return None
    try:
        number = float(match.group())
    except ValueError:
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _normalized_key(value: str) -> str:
    return re.sub(r"[\s_\-/]", "", value).lower()


def _metadata_objects(raw: dict[str, Any], top_index: int | None) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    if top_index is not None:
        top = raw.get("top")
        if isinstance(top, list) and 0 <= top_index < len(top) and isinstance(top[top_index], dict):
            objects.append(top[top_index])
    objects.append(raw)
    return objects


def _field(objects: list[dict[str, Any]], keys: set[str]) -> tuple[Any, str] | None:
    for item in objects:
        for key, value in item.items():
            normalized = _normalized_key(str(key))
            if normalized in keys and value is not None:
                return value, normalized
    return None


def _candidate_metadata(
    raw_json: str, top_index: int | None
) -> tuple[float | None, int | None, int | None, int | None, bool | None, str | None]:
    try:
        raw = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None, None, None, None, None, None
    if not isinstance(raw, dict):
        return None, None, None, None, None, None
    objects = _metadata_objects(raw, top_index)

    duration_entry = _field(
        objects,
        {"duration", "durationms", "videoduration", "videodurationms", "时长", "视频时长"},
    )
    duration = _number(duration_entry[0]) if duration_entry else None
    if duration is not None and duration_entry and duration_entry[1].endswith("ms"):
        duration /= 1000

    size_entry = _field(
        objects,
        {"size", "filesize", "videosize", "bytes", "文件大小", "视频大小"},
    )
    size_number = _number(size_entry[0]) if size_entry else None
    size = int(size_number) if size_number is not None else None
    if size_number is not None and size_entry and isinstance(size_entry[0], str):
        text = size_entry[0].lower()
        if "gb" in text:
            size = int(size_number * 1024**3)
        elif "mb" in text:
            size = int(size_number * 1024**2)
        elif "kb" in text:
            size = int(size_number * 1024)

    width_entry = _field(objects, {"width", "videowidth", "宽度", "视频宽度"})
    height_entry = _field(objects, {"height", "videoheight", "高度", "视频高度"})
    width_number = _number(width_entry[0]) if width_entry else None
    height_number = _number(height_entry[0]) if height_entry else None
    width = int(width_number) if width_number is not None and width_number > 0 else None
    height = int(height_number) if height_number is not None and height_number > 0 else None
    if width is None or height is None:
        resolution = _field(objects, {"resolution", "videoresolution", "分辨率", "视频分辨率"})
        if resolution is not None and isinstance(resolution[0], str):
            match = re.search(r"(\d{2,5})\s*[x×*]\s*(\d{2,5})", resolution[0])
            if match is not None:
                width, height = int(match.group(1)), int(match.group(2))

    audio_entry = _field(objects, {"hasaudio", "audio", "有音轨", "是否有音频"})
    has_audio: bool | None = None
    if audio_entry is not None:
        value = audio_entry[0]
        if isinstance(value, bool):
            has_audio = value
        elif isinstance(value, (int, float)) and value in {0, 1}:
            has_audio = bool(value)
        elif isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"yes", "true", "1", "有", "是"}:
                has_audio = True
            elif normalized in {"no", "false", "0", "无", "否"}:
                has_audio = False

    category_entry = _field(
        objects,
        {"industry", "category", "videotype", "type", "行业", "分类", "类目", "品类", "视频类型"},
    )
    category = None
    if category_entry is not None and isinstance(category_entry[0], (str, int)):
        rendered = str(category_entry[0]).strip()
        category = rendered[:120] or None
    return duration, size, width, height, has_audio, category


def _load_candidates(catalog: Path) -> list[_Candidate]:
    query = """
        SELECT o.file, o.row_index, o.identity, o.top_index,
               COALESCE(o.observed_playback_url, v.playback_url, o.observed_url) AS media_url,
               r.raw_json
        FROM observations AS o
        JOIN videos AS v ON v.identity = o.identity
        JOIN source_records AS r ON r.id = o.record_id
        WHERE o.file IN ({placeholders})
          AND o.identity_conflict = 0
          AND o.identity NOT LIKE 'unidentified:%'
          AND COALESCE(o.observed_playback_url, v.playback_url, o.observed_url) IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM observations AS conflict
              WHERE conflict.identity = o.identity AND conflict.identity_conflict = 1
          )
        ORDER BY o.file, o.row_index, COALESCE(o.top_index, -1), o.id
        LIMIT ?
    """
    groups = (("video.json",), ("gold3s.json", "golden3s_groups.json"))
    limits = (_MAX_CANDIDATE_METADATA // 2, _MAX_CANDIDATE_METADATA // 2)
    candidates: list[_Candidate] = []
    seen: set[tuple[str, str]] = set()
    with sqlite3.connect(f"file:{catalog.as_posix()}?mode=ro", uri=True) as database:
        database.row_factory = sqlite3.Row
        for files, limit in zip(groups, limits, strict=True):
            sql = query.format(placeholders=",".join("?" for _ in files))
            rows = database.execute(sql, (*files, limit)).fetchall()
            for row in rows:
                identity = str(row["identity"])
                mode = "full" if row["file"] == "video.json" else "opening"
                if (mode, identity) in seen:
                    continue
                url = row["media_url"]
                if not isinstance(url, str) or not url.strip():
                    continue
                duration, size, width, height, has_audio, category = _candidate_metadata(
                    row["raw_json"], row["top_index"]
                )
                candidates.append(
                    _Candidate(
                        sourcefile=str(row["file"]),
                        row=int(row["row_index"]),
                        identity=identity,
                        url=url,
                        duration=duration,
                        size=size,
                        width=width,
                        height=height,
                        has_audio=has_audio,
                        category=category,
                    )
                )
                seen.add((mode, identity))
    return candidates


def _mode(candidate: _Candidate) -> str:
    return "full" if candidate.sourcefile == "video.json" else "opening"


def _score(candidate: _Candidate, categories: set[str]) -> tuple[int, int, int, int, int, str, int]:
    duration_score = (
        0
        if candidate.duration is not None and 8 <= candidate.duration <= 25
        else 1
        if candidate.duration is not None
        else 2
    )
    size_score = (
        0
        if candidate.size is not None and candidate.size < _PREFERRED_BYTES
        else 1
        if candidate.size is None
        else 2
    )
    dimension_score = (
        0
        if candidate.width is not None
        and candidate.height is not None
        and candidate.width <= 1920
        and candidate.height <= 1920
        else 1
        if candidate.width is None or candidate.height is None
        else 2
    )
    audio_score = 0 if candidate.has_audio is True else 1 if candidate.has_audio is None else 2
    category_score = (
        0 if candidate.category is not None and candidate.category not in categories else 1
    )
    return (
        duration_score,
        size_score,
        dimension_score,
        audio_score,
        category_score,
        candidate.identity,
        candidate.row,
    )


def _suffix(url: str) -> str:
    try:
        suffix = Path(urlsplit(url).path).suffix.lower()
    except ValueError:
        return ".bin"
    return suffix if suffix in {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"} else ".bin"


def _safe_reason(error: Exception) -> str:
    if isinstance(error, DownloadError):
        return str(error)[:300]
    if isinstance(error, ValueError):
        return str(error)[:300]
    return type(error).__name__


def _atomic_json(path: Path, payload: Any) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2, allow_nan=False)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _load_rejections(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("rejections.json is invalid") from exc
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("rejections.json must contain a list of objects")
    return payload


def _validate_existing(path: Path, campaign_dir: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("samples.json is invalid") from exc
    if not isinstance(payload, list) or len(payload) != _SAMPLE_COUNT:
        raise ValueError("samples.json must contain exactly four samples")
    modes = {name: 0 for name in _MODE_COUNTS}
    identities: set[str] = set()
    hashes: set[str] = set()
    downloads = (campaign_dir / "downloads").resolve()
    for item in payload:
        if not isinstance(item, dict):
            raise ValueError("samples.json entries must be objects")
        mode = item.get("mode")
        if mode not in modes:
            raise ValueError("samples.json contains an invalid mode")
        modes[mode] += 1
        identity = item.get("identity")
        source_hash = item.get("source_sha256")
        if not isinstance(identity, str) or not identity or identity in identities:
            raise ValueError("samples.json identities must be distinct")
        if (
            not isinstance(source_hash, str)
            or not _SHA256.fullmatch(source_hash)
            or source_hash in hashes
        ):
            raise ValueError("samples.json source hashes must be distinct SHA-256 values")
        file_value = item.get("path")
        if not isinstance(file_value, str):
            raise ValueError("samples.json path is invalid")
        try:
            source = Path(file_value).resolve(strict=True)
        except OSError as exc:
            raise ValueError("sample media file is missing") from exc
        if (
            not source.is_file()
            or not source.is_relative_to(downloads)
            or digest(source) != source_hash
        ):
            raise ValueError("sample media hash or location is invalid")
        identities.add(identity)
        hashes.add(source_hash)
    if modes != _MODE_COUNTS:
        raise ValueError("samples.json must contain two full and two opening samples")
    return payload


def select_samples(
    store: Store, snapshot: dict[str, Any], campaign_dir: Path
) -> list[dict[str, Any]]:
    """Select and download exactly two full and two opening-mode videos.

    At most 40 catalog candidates are inspected and at most 12 downloads are
    attempted for a campaign. Failed signed URLs are persisted by identity in
    ``rejections.json`` and are not retried on a later invocation. Existing
    ``samples.json`` is integrity-checked and returned without network access.
    Source URLs are deliberately absent from both output files.
    """

    campaign_dir = campaign_dir.expanduser().absolute()
    if campaign_dir.is_symlink():
        raise ValueError("campaign directory may not be a symlink")
    campaign_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    samples_path = campaign_dir / "samples.json"
    if samples_path.exists():
        return _validate_existing(samples_path, campaign_dir)

    catalog_value = snapshot.get("catalog")
    if not isinstance(catalog_value, str):
        raise ValueError("snapshot does not contain a catalog path")
    try:
        catalog = Path(catalog_value).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError("snapshot catalog is missing") from exc
    if not catalog.is_file() or not catalog.is_relative_to(store.root):
        raise ValueError("snapshot catalog must be a file inside the store")

    rejection_path = campaign_dir / "rejections.json"
    rejections = _load_rejections(rejection_path)
    if len(rejections) >= _MAX_DOWNLOAD_ATTEMPTS:
        raise ValueError("campaign has exhausted its 12 download attempts")
    rejected_identities = {
        item.get("identity") for item in rejections if isinstance(item.get("identity"), str)
    }
    candidates = [
        candidate
        for candidate in _load_candidates(catalog)
        if candidate.identity not in rejected_identities
    ]
    downloads = campaign_dir / "downloads"
    downloads.mkdir(exist_ok=True, mode=0o700)

    selected: list[dict[str, Any]] = []
    selected_identities: set[str] = set()
    selected_hashes: set[str] = set()
    categories: set[str] = set()
    created_directories: list[Path] = []
    mode_order = ("full", "opening", "full", "opening")
    try:
        for wanted_mode in mode_order:
            while (
                sum(1 for item in selected if item["mode"] == wanted_mode)
                < _MODE_COUNTS[wanted_mode]
            ):
                if len(rejections) + len(selected) >= _MAX_DOWNLOAD_ATTEMPTS:
                    raise ValueError("unable to select four samples within 12 download attempts")
                eligible = [
                    candidate
                    for candidate in candidates
                    if _mode(candidate) == wanted_mode
                    and candidate.identity not in selected_identities
                    and candidate.identity not in rejected_identities
                ]
                if not eligible:
                    raise ValueError(f"catalog has too few usable {wanted_mode} candidates")
                candidate = min(eligible, key=lambda item: _score(item, categories))
                attempt = len(rejections) + len(selected) + 1
                directory_name = (
                    f"{attempt:02d}-{hashlib.sha256(candidate.identity.encode()).hexdigest()[:16]}"
                )
                target_dir = downloads / directory_name
                if target_dir.exists():
                    shutil.rmtree(target_dir)
                target_dir.mkdir(mode=0o700)
                created_directories.append(target_dir)
                target = target_dir / f"source{_suffix(candidate.url)}"
                try:
                    result = download_media(
                        candidate.url,
                        target,
                        max_bytes=MAX_DOWNLOAD_BYTES,
                        max_redirects=3,
                    )
                    source_hash = result.get("source_sha256")
                    duration = result.get("duration")
                    size = result.get("size")
                    media = result.get("media")
                    width = media.get("width") if isinstance(media, dict) else None
                    height = media.get("height") if isinstance(media, dict) else None
                    if not isinstance(source_hash, str) or digest(target) != source_hash:
                        raise ValueError("download hash verification failed")
                    if source_hash in selected_hashes:
                        raise ValueError("download duplicates an already selected source")
                    if (
                        not isinstance(duration, (int, float))
                        or not math.isfinite(duration)
                        or duration <= 0
                    ):
                        raise ValueError("downloaded media duration is invalid")
                    if (
                        not isinstance(width, int)
                        or not isinstance(height, int)
                        or width <= 0
                        or height <= 0
                        or width > 1920
                        or height > 1920
                    ):
                        raise ValueError("media dimensions exceed the 1920x1920 campaign limit")
                    if wanted_mode == "full" and (
                        not isinstance(size, int)
                        or size > _PREFERRED_BYTES
                        or not 8 <= duration <= 25
                    ):
                        raise ValueError("full-mode media exceeds the short-video request budget")
                except Exception as exc:
                    shutil.rmtree(target_dir, ignore_errors=True)
                    rejection = {
                        "sourcefile": candidate.sourcefile,
                        "row": candidate.row,
                        "identity": candidate.identity,
                        "mode": wanted_mode,
                        "reason": _safe_reason(exc),
                    }
                    rejections.append(rejection)
                    rejected_identities.add(candidate.identity)
                    _atomic_json(rejection_path, rejections)
                    continue

                reason_parts = [
                    f"selected for {wanted_mode} analysis",
                    f"actual duration {float(duration):.3f}s",
                    f"actual size {int(size)} bytes"
                    if isinstance(size, int)
                    else "actual size unavailable",
                    f"actual dimensions {width}x{height}",
                    "actual audio present"
                    if result.get("has_audio") is True
                    else "actual audio absent",
                ]
                if candidate.category is not None:
                    reason_parts.append(f"category {candidate.category}")
                    categories.add(candidate.category)
                selected.append(
                    {
                        "sourcefile": candidate.sourcefile,
                        "row": candidate.row,
                        "identity": candidate.identity,
                        "source_sha256": source_hash,
                        "path": str(target.resolve()),
                        "mode": wanted_mode,
                        "duration": float(duration),
                        "reason": "; ".join(reason_parts),
                    }
                )
                selected_identities.add(candidate.identity)
                selected_hashes.add(source_hash)
                candidates.remove(candidate)
                break

        if len(selected) != _SAMPLE_COUNT:
            raise ValueError("sample selection did not produce exactly four videos")
        # Preserve the fixed campaign order while validating all invariants before publish.
        _atomic_json(samples_path, selected)
        return _validate_existing(samples_path, campaign_dir)
    except Exception:
        for directory in created_directories:
            if any(item.get("path", "").startswith(str(directory)) for item in selected):
                shutil.rmtree(directory, ignore_errors=True)
        raise
