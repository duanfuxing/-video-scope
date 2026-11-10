"""Versioned local evidence inventory, not an analysis result contract."""

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from video_scope.media import MediaInfo


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class EvidenceFile(StrictModel):
    path: str
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    size: Annotated[int, Field(gt=0)]
    kind: Literal["source", "frame", "audio"]
    time: Annotated[float, Field(ge=0)] | None = None

    @model_validator(mode="after")
    def check_path_and_time(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            not self.path
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or ":" in self.path
            or path.as_posix() != self.path
        ):
            raise ValueError("media path must be a normalized relative POSIX path")
        if (self.kind == "frame") != (self.time is not None):
            raise ValueError("only frames must carry a source-relative timestamp")
        return self


class EvidenceManifest(StrictModel):
    schema_version: Literal["evidence-0.1.0"] = "evidence-0.1.0"
    status: Literal["prepared_not_analyzed"] = "prepared_not_analyzed"
    source_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    pipeline_version: Literal["media-0.1.0"] = "media-0.1.0"
    media: MediaInfo
    files: list[EvidenceFile]
    unverified: list[str] = Field(
        default_factory=lambda: [
            "No semantic analysis has been performed.",
            "Frames are sparse static evidence, not verified shots or actions.",
            "ASR and OCR have not been run.",
            "Source completeness and redistribution rights are unknown.",
        ]
    )

    @model_validator(mode="after")
    def check_inventory(self) -> Self:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate media paths")
        sources = [file for file in self.files if file.kind == "source"]
        if len(sources) != 1 or sources[0].sha256 != self.source_id:
            raise ValueError("exactly one matching source is required")
        audio = [file for file in self.files if file.kind == "audio"]
        if len(audio) != int(self.media.has_audio):
            raise ValueError("audio inventory does not match source")
        frames = [file for file in self.files if file.kind == "frame"]
        if not frames:
            raise ValueError("at least one actual frame is required")
        if len({frame.time for frame in frames}) != len(frames):
            raise ValueError("duplicate frame timestamps")
        source_times = set(self.media.frame_times)
        for frame in frames:
            if frame.time is None or not math.isfinite(frame.time):
                raise ValueError("invalid frame timestamp")
            if frame.time >= self.media.duration:
                raise ValueError("frame outside source duration")
            if frame.time not in source_times:
                raise ValueError("frame timestamp was not observed in the source")
        return self


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def validate_evidence(directory: Path) -> EvidenceManifest:
    root = directory.resolve(strict=True)
    manifest_path = root / "evidence.json"
    if manifest_path.is_symlink():
        raise ValueError("manifest may not be a symlink")
    manifest = EvidenceManifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.files:
        path = root / item.path
        if any(part.is_symlink() for part in [path, *path.parents] if part != root):
            raise ValueError(f"symlink in media path: {item.path}")
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"media outside evidence directory: {item.path}")
        if resolved.stat().st_size != item.size or digest(resolved) != item.sha256:
            raise ValueError(f"media integrity failure: {item.path}")
    return manifest


def write_manifest(directory: Path, manifest: EvidenceManifest) -> None:
    (directory / "evidence.json").write_text(
        json.dumps(manifest.model_dump(), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
