"""Atomic, commit-pinned synchronization of the configured public JSON sources."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from http.client import HTTPException
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from pydantic import BaseModel, ConfigDict, Field

from video_scope.catalog import CATALOG_VERSION, build_catalog
from video_scope.evidence import digest
from video_scope.jobs import Store

REPOSITORY = "804607843-sudo/804607843-sudo.github.io"
BRANCH = "main"
FILES = ("video.json", "golden3s_groups.json", "gold3s.json")
MAX_FILE_BYTES = 128 * 1024 * 1024
API = f"https://api.github.com/repos/{REPOSITORY}"


class CachedFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blob: str = Field(pattern=r"^[0-9a-f]{40}$")
    etag: str | None = Field(default=None, max_length=256)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0, le=MAX_FILE_BYTES)


class Snapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["sources-0.1.0"] = "sources-0.1.0"
    repository: Literal["804607843-sudo/804607843-sudo.github.io"] = (
        "804607843-sudo/804607843-sudo.github.io"
    )
    catalog_version: str = Field(default=CATALOG_VERSION, max_length=64)
    commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    directory: str = Field(pattern=r"^snapshot-[a-zA-Z0-9_-]+$")
    checked_at: str
    files: dict[str, CachedFile]
    catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    counts: dict[str, int]


def _request(url: str) -> Request:
    return Request(url, headers={"User-Agent": "VideoScope/0.1", "Accept": "application/json"})


def _get_json(url: str) -> Any:
    try:
        with urlopen(_request(url), timeout=60) as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise ValueError("GitHub metadata exceeds size limit")
        return json.loads(data)
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise RuntimeError(
            "GitHub metadata request failed; current snapshot was not changed"
        ) from exc


def _revision() -> tuple[str, dict[str, tuple[str, int]]]:
    try:
        return _api_revision()
    except RuntimeError as exc:
        cause = exc.__cause__
        if isinstance(cause, HTTPError) and cause.code in (403, 429):
            return _git_revision()
        raise


def _git_revision() -> tuple[str, dict[str, tuple[str, int]]]:
    try:
        result = subprocess.run(
            [
                "git",
                "ls-remote",
                "--refs",
                f"https://github.com/{REPOSITORY}.git",
                f"refs/heads/{BRANCH}",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError("GitHub API limited and Git revision check failed") from exc
    rows = [line.split() for line in result.stdout.splitlines() if line.strip()]
    if len(rows) != 1 or len(rows[0]) != 2 or rows[0][1] != f"refs/heads/{BRANCH}":
        raise ValueError("Git did not return the expected source branch")
    commit = rows[0][0]
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("invalid Git commit SHA")
    files = {}
    for name in FILES:
        url = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/static/data/{name}"
        request = Request(url, method="HEAD", headers={"User-Agent": "VideoScope/0.1"})
        try:
            with urlopen(request, timeout=60) as response:
                etag = response.headers.get("ETag", "")
                size = int(response.headers.get("Content-Length", "0"))
        except (URLError, OSError, HTTPException) as exc:
            raise RuntimeError(f"source version check failed: {name}") from exc
        if not etag or len(etag) > 256 or not 0 < size <= MAX_FILE_BYTES:
            raise ValueError(f"source response lacks bounded size or ETag: {name}")
        files[name] = (f"etag:{etag}", size)
    return commit, files


def _api_revision() -> tuple[str, dict[str, tuple[str, int]]]:
    ref = _get_json(f"{API}/git/ref/heads/{BRANCH}")
    try:
        commit = ref["object"]["sha"]
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise ValueError("invalid commit SHA")
        entries = _get_json(f"{API}/contents/static/data?ref={commit}")
        if not isinstance(entries, list):
            raise ValueError("source listing must be an array")
        files = {}
        for entry in entries:
            if entry.get("name") not in FILES:
                continue
            name, blob, size = entry["name"], entry["sha"], entry["size"]
            if entry.get("type") != "file" or not re.fullmatch(r"[0-9a-f]{40}", blob):
                raise ValueError("invalid source blob")
            if type(size) is not int or not 0 < size <= MAX_FILE_BYTES:
                raise ValueError("source exceeds size limit or is empty")
            files[name] = (blob, size)
        if set(files) != set(FILES):
            raise ValueError("remote revision does not contain all three required JSON files")
        return commit, files
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid GitHub revision metadata") from exc


def _download(commit: str, name: str, path: Path, revision: str, size: int) -> CachedFile:
    url = f"https://raw.githubusercontent.com/{REPOSITORY}/{commit}/static/data/{name}"
    git_hash = hashlib.sha1(f"blob {size}\0".encode(), usedforsecurity=False)
    sha256 = hashlib.sha256()
    total = 0
    try:
        with urlopen(_request(url), timeout=60) as response, path.open("xb") as output:
            etag = response.headers.get("ETag")
            if revision.startswith("etag:") and revision != f"etag:{etag}":
                raise ValueError(f"source ETag changed between version check and download: {name}")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > size or total > MAX_FILE_BYTES:
                    raise ValueError(f"download exceeds declared size: {name}")
                output.write(chunk)
                git_hash.update(chunk)
                sha256.update(chunk)
            output.flush()
            os.fsync(output.fileno())
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise RuntimeError(
            f"source download failed: {name}; current snapshot was not changed"
        ) from exc
    blob = git_hash.hexdigest()
    if total != size or (not revision.startswith("etag:") and blob != revision):
        raise ValueError(f"remote Git blob integrity check failed: {name}")
    return CachedFile(blob=blob, etag=etag, sha256=sha256.hexdigest(), size=total)


def _load(root: Path) -> Snapshot | None:
    pointer = root / "current.json"
    if not pointer.exists():
        return None
    if pointer.is_symlink():
        raise ValueError("source pointer may not be a symlink")
    snapshot = Snapshot.model_validate_json(pointer.read_text(encoding="utf-8"))
    if set(snapshot.files) != set(FILES):
        raise ValueError("incomplete source snapshot")
    directory = root / snapshot.directory
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid source snapshot directory")
    return snapshot


def _file_valid(root: Path, snapshot: Snapshot, name: str) -> bool:
    path = root / snapshot.directory / name
    item = snapshot.files[name]
    return (
        not path.is_symlink()
        and path.is_file()
        and path.stat().st_size == item.size
        and digest(path) == item.sha256
    )


def _catalog_valid(root: Path, snapshot: Snapshot) -> bool:
    path = root / snapshot.directory / "catalog.sqlite3"
    return (
        snapshot.catalog_version == CATALOG_VERSION
        and not path.is_symlink()
        and path.is_file()
        and digest(path) == snapshot.catalog_sha256
    )


def _publish(root: Path, snapshot: Snapshot) -> None:
    descriptor, name = tempfile.mkstemp(prefix=".pointer-", dir=root)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(snapshot.model_dump_json(indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, root / "current.json")
    finally:
        temporary.unlink(missing_ok=True)


def sync_sources(store: Store, *, offline: bool = False) -> dict[str, Any]:
    """Check remote revision, then publish all files and the deduplicated index together."""
    with store.lock():
        root = store.root / "sources"
        if root.is_symlink():
            raise ValueError("sources directory may not be a symlink")
        root.mkdir(exist_ok=True, mode=0o700)
        current = _load(root)
        valid = {name: bool(current and _file_valid(root, current, name)) for name in FILES}
        catalog_valid = bool(current and _catalog_valid(root, current))
        if offline:
            if current is None or not all(valid.values()) or not catalog_valid:
                raise ValueError("offline mode requires an intact previously synchronized snapshot")
            return _result(root, current, "offline", [])

        commit, remote_files = _revision()
        version_check = (
            "git-etag"
            if any(value[0].startswith("etag:") for value in remote_files.values())
            else "github-api"
        )
        now = datetime.now(UTC).isoformat()
        changed = [
            name
            for name in FILES
            if current is None
            or not valid[name]
            or remote_files[name][0]
            not in (current.files[name].blob, f"etag:{current.files[name].etag}")
        ]
        if current is not None and not changed and catalog_valid:
            current = current.model_copy(update={"commit": commit, "checked_at": now})
            _publish(root, current)
            return _result(root, current, "unchanged", [], version_check)

        staging = Path(tempfile.mkdtemp(prefix="snapshot-", dir=root))
        published = False
        try:
            records = {}
            for name in FILES:
                if current is not None and name not in changed:
                    shutil.copyfile(root / current.directory / name, staging / name)
                    if digest(staging / name) != current.files[name].sha256:
                        raise ValueError("source cache changed during synchronization")
                    records[name] = current.files[name]
                else:
                    blob, size = remote_files[name]
                    records[name] = _download(commit, name, staging / name, blob, size)
            counts = build_catalog(staging, staging / "catalog.sqlite3")
            snapshot = Snapshot(
                commit=commit,
                directory=staging.name,
                checked_at=now,
                files=records,
                catalog_sha256=digest(staging / "catalog.sqlite3"),
                counts=counts,
            )
            _publish(root, snapshot)
            published = True
            return _result(root, snapshot, "updated", changed, version_check)
        finally:
            if not published:
                shutil.rmtree(staging)


def _result(
    root: Path,
    snapshot: Snapshot,
    status: str,
    downloaded: list[str],
    version_check: str = "offline",
) -> dict[str, Any]:
    return {
        "status": status,
        "version_check": version_check,
        "repository": REPOSITORY,
        "commit": snapshot.commit,
        "last_remote_check": snapshot.checked_at,
        "snapshot": str(root / snapshot.directory),
        "catalog": str(root / snapshot.directory / "catalog.sqlite3"),
        "downloaded": downloaded,
        "counts": snapshot.counts,
    }
