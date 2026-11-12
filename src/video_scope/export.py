"""Atomic export and validation for self-contained version 1.0.0 result packages."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal, Never, cast

from video_scope.evidence import EvidenceFile, digest, validate_evidence
from video_scope.result_models import (
    FullVideoResult,
    KeyFrame,
    MediaReference,
    OpeningResult,
    PackageFile,
    PackageManifest,
)
from video_scope.validation import validate_result

_FRAME_BIND_TOLERANCE = 0.05


def _reject_constant(value: str) -> Never:
    raise ValueError(f"non-standard JSON number {value!r}")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key {key!r}")
        result[key] = value
    return result


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"invalid JSON file {path.name}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return cast("dict[str, Any]", value)


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _package_file(
    path: Path,
    root: Path,
    kind: Literal["result", "source", "frame"],
    source_time: float | None = None,
) -> PackageFile:
    return PackageFile(
        path=path.relative_to(root).as_posix(),
        sha256=digest(path),
        size=path.stat().st_size,
        kind=kind,
        source_time=source_time,
    )


def _all_key_frames(result: FullVideoResult | OpeningResult) -> Iterable[tuple[str, KeyFrame]]:
    for shot in result.shots:
        for index, frame in enumerate(shot.key_frames):
            yield f"镜头 {shot.shot_id} 关键帧 {index}", frame
    if isinstance(result, OpeningResult):
        for evidence_index, evidence in enumerate(result.follow_up.evidence):
            for frame_index, frame in enumerate(evidence.key_frames):
                yield f"后文证据 {evidence_index} 关键帧 {frame_index}", frame


def _copy_media(
    *,
    evidence_dir: Path,
    staging: Path,
    evidence_files: list[EvidenceFile],
) -> tuple[list[PackageFile], list[tuple[float, PackageFile]]]:
    inventory: list[PackageFile] = []
    frame_inventory: list[tuple[float, PackageFile]] = []
    source = next(item for item in evidence_files if item.kind == "source")
    source_suffix = Path(source.path).suffix
    source_output = staging / "media" / f"source{source_suffix}"
    source_output.parent.mkdir(parents=True)
    shutil.copyfile(evidence_dir / source.path, source_output)
    inventory.append(_package_file(source_output, staging, "source"))

    frames_output = staging / "media" / "frames"
    frames_output.mkdir()
    frames = sorted(
        (item for item in evidence_files if item.kind == "frame"),
        key=lambda item: (cast("float", item.time), item.path),
    )
    for index, frame in enumerate(frames, 1):
        assert frame.time is not None
        suffix = Path(frame.path).suffix or ".jpg"
        output = frames_output / f"{index:03d}{suffix.lower()}"
        shutil.copyfile(evidence_dir / frame.path, output)
        record = _package_file(output, staging, "frame", frame.time)
        inventory.append(record)
        frame_inventory.append((frame.time, record))
    return inventory, frame_inventory


def _bind_frames(
    result: FullVideoResult | OpeningResult,
    frames: list[tuple[float, PackageFile]],
) -> list[str]:
    warnings: list[str] = []
    for location, key_frame in _all_key_frames(result):
        # Supplier/model-provided media identifiers are never trusted. The package
        # reference is rebuilt exclusively from the validated evidence inventory.
        key_frame.media_reference = None
        nearest_time, nearest = min(
            frames,
            key=lambda item: (abs(item[0] - key_frame.source_time), item[0], item[1].path),
        )
        difference = abs(nearest_time - key_frame.source_time)
        if difference <= _FRAME_BIND_TOLERANCE + 1e-12:
            key_frame.media_reference = MediaReference.model_validate(
                {
                    "path": nearest.path,
                    "source_time": nearest_time,
                    "sha256": nearest.sha256,
                }
            )
        else:
            warnings.append(
                f"{location}: no evidence frame within {_FRAME_BIND_TOLERANCE:.3f}s "
                f"of requested {key_frame.source_time:.9f}s; media reference is null"
            )
    return warnings


def _nonblank(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonblank string")
    return value.strip()


def export_result(
    *,
    result: dict[str, Any],
    mode: str,
    evidence_dir: Path,
    output: Path,
    analysis_id: str,
    model: str,
    provider: str,
    call_ids: list[str],
) -> Path:
    """Atomically create a new package without copying raw provider artifacts."""

    analysis_id = _nonblank(analysis_id, "analysis_id")
    model = _nonblank(model, "model")
    provider = _nonblank(provider, "provider")
    if not isinstance(call_ids, list):
        raise ValueError("call_ids must be a list")
    clean_call_ids = [_nonblank(value, "call_id") for value in call_ids]

    evidence_root = Path(evidence_dir).expanduser().resolve(strict=True)
    evidence = validate_evidence(evidence_root)
    parsed = validate_result(result, mode, evidence.media.duration)
    if not isinstance(parsed, (FullVideoResult, OpeningResult)):
        raise TypeError("unsupported result type")
    if parsed.source_id != evidence.source_id:
        raise ValueError("result source id does not match evidence source id")

    destination = Path(output).expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError("export destination already exists; it will not be overwritten")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    claimed_destination = False
    try:
        inventory, frames = _copy_media(
            evidence_dir=evidence_root,
            staging=staging,
            evidence_files=evidence.files,
        )
        warnings = _bind_frames(parsed, frames)
        result_name = "full-video.json" if mode == "full" else "opening.json"
        result_path = staging / result_name
        _write_json(result_path, parsed.model_dump(mode="json", by_alias=True))
        inventory.insert(0, _package_file(result_path, staging, "result"))

        manifest = PackageManifest(
            source_id=evidence.source_id,
            analysis_id=analysis_id,
            mode=cast("Literal['full', 'opening']", mode),
            model=model,
            provider=provider,
            call_ids=clean_call_ids,
            duration=evidence.media.duration,
            files=inventory,
            warnings=warnings,
        )
        _write_json(staging / "manifest.json", manifest.model_dump(mode="json"))
        validate_package(staging)

        try:
            destination.mkdir()
            claimed_destination = True
        except FileExistsError as exc:
            raise ValueError(
                "export destination already exists; it will not be overwritten"
            ) from exc
        try:
            os.rename(staging, destination)
        except Exception:
            destination.rmdir()
            claimed_destination = False
            raise
        claimed_destination = False
        return destination
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        if claimed_destination and destination.exists():
            destination.rmdir()


def _safe_file(root: Path, relative: str) -> Path:
    path = root / relative
    parents = [path, *path.parents]
    if any(item.is_symlink() for item in parents if item != root):
        raise ValueError(f"symlink in package path: {relative}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"missing package file: {relative}") from exc
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError(f"package file outside package: {relative}")
    return resolved


def _validate_media_references(
    result: FullVideoResult | OpeningResult,
    frame_files: dict[str, PackageFile],
) -> None:
    for location, frame in _all_key_frames(result):
        reference = frame.media_reference
        if reference is None:
            continue
        packaged = frame_files.get(reference.path)
        if packaged is None:
            raise ValueError(f"{location}: media reference is not an inventoried frame")
        if reference.sha256 != packaged.sha256 or reference.source_time != packaged.source_time:
            raise ValueError(f"{location}: media reference metadata does not match manifest")
        if abs(reference.source_time - frame.source_time) > _FRAME_BIND_TOLERANCE + 1e-12:
            raise ValueError(f"{location}: media reference is outside frame binding tolerance")


def validate_package(path: Path) -> dict[str, Any]:
    """Validate package paths, hashes, mode, business result, and media references."""

    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise ValueError("package directory may not be a symlink")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError("package must be an existing directory") from exc
    if not root.is_dir():
        raise ValueError("package must be a directory")

    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("manifest may not be a symlink")
    manifest = PackageManifest.model_validate(_read_object(manifest_path))

    resolved_files: dict[str, Path] = {}
    for item in manifest.files:
        resolved = _safe_file(root, item.path)
        if resolved.stat().st_size != item.size or digest(resolved) != item.sha256:
            raise ValueError(f"package integrity failure: {item.path}")
        resolved_files[item.path] = resolved

    actual_files: set[str] = set()
    for child in root.rglob("*"):
        if child.is_symlink():
            raise ValueError(f"symlink in package: {child.relative_to(root).as_posix()}")
        if child.is_file():
            actual_files.add(child.relative_to(root).as_posix())
    expected_files = {"manifest.json", *(item.path for item in manifest.files)}
    if actual_files != expected_files:
        extras = sorted(actual_files - expected_files)
        missing = sorted(expected_files - actual_files)
        raise ValueError(f"package file inventory mismatch; extra={extras}, missing={missing}")

    result_name = "full-video.json" if manifest.mode == "full" else "opening.json"
    result_item = next(item for item in manifest.files if item.kind == "result")
    if result_item.path != result_name:
        raise ValueError("result filename does not match package mode")
    source_item = next(item for item in manifest.files if item.kind == "source")
    if source_item.sha256 != manifest.source_id:
        raise ValueError("source file hash does not match manifest source_id")

    result_payload = _read_object(resolved_files[result_item.path])
    result = validate_result(result_payload, manifest.mode, manifest.duration)
    if not isinstance(result, (FullVideoResult, OpeningResult)):
        raise TypeError("unsupported result type")
    if result.source_id != manifest.source_id:
        raise ValueError("result source id does not match package source id")

    frame_files = {item.path: item for item in manifest.files if item.kind == "frame"}
    _validate_media_references(result, frame_files)
    return manifest.model_dump(mode="json")
