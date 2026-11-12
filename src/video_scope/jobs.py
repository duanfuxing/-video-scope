"""Local preparation jobs. No supplier requests are made by this module."""

import fcntl
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from video_scope.evidence import EvidenceManifest, digest, validate_evidence, write_manifest
from video_scope.media import prepare


class Store:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        checkout = Path(__file__).resolve().parents[2]
        if (checkout / "pyproject.toml").exists() and self.root.is_relative_to(checkout):
            raise ValueError("data directory must be outside the code checkout")
        for parent in [self.root, *self.root.parents]:
            if (parent / ".git").exists():
                raise ValueError("data directory must be outside Git working trees")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("evidence", "temporary"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, source TEXT NOT NULL, source_hash TEXT NOT NULL,
                state TEXT NOT NULL, evidence TEXT, error TEXT,
                created TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )""")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.root / "tasks.sqlite3", timeout=30)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @contextmanager
    def lock(self) -> Iterator[None]:
        with (self.root / "scheduler.lock").open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError("another preparation process is running; retry later") from exc
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def create(self, source: Path) -> str:
        source = source.expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError("input must be a local regular file")
        source_hash = digest(source)
        identifier = uuid.uuid4().hex
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(id, source, source_hash, state) VALUES (?, ?, ?, 'pending')",
                (identifier, str(source), source_hash),
            )
        return identifier

    def status(self, identifier: str) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id = ?", (identifier,)).fetchone()
        if row is None:
            raise ValueError(f"unknown task: {identifier}")
        result = dict(row)
        result["supplier_calls"] = 0
        result["analysis_status"] = "not_implemented"
        return result

    def update(
        self, identifier: str, state: str, evidence: str | None, error: str | None = None
    ) -> None:
        with self.connect() as db:
            db.execute(
                """UPDATE jobs SET state=?, evidence=?, error=?, updated=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (state, evidence, error, identifier),
            )

    def run(self, identifier: str) -> dict[str, Any]:
        with self.lock():
            job = self.status(identifier)
            relative = f"evidence/{job['source_hash']}-media-0.1.0"
            target = self.root / relative
            temporary: Path | None = None
            self.update(identifier, "preparing", None)
            try:
                if target.exists():
                    manifest = validate_evidence(target)
                    if manifest.source_id != job["source_hash"]:
                        raise ValueError("cached evidence source mismatch")
                else:
                    source = Path(job["source"])
                    if digest(source) != job["source_hash"]:
                        raise ValueError("source changed since task creation; create a new task")
                    temporary = Path(
                        tempfile.mkdtemp(prefix="prepare-", dir=self.root / "temporary")
                    )
                    payload = prepare(source, temporary)
                    manifest = EvidenceManifest(source_id=job["source_hash"], **payload)
                    write_manifest(temporary, manifest)
                    validate_evidence(temporary)
                    os.rename(temporary, target)
                    temporary = None
                self.update(identifier, "prepared", relative)
            except Exception as exc:
                self.update(identifier, "failed", None, f"{type(exc).__name__}: {exc}"[:2000])
                raise
            finally:
                if temporary is not None:
                    shutil.rmtree(temporary)
        return self.status(identifier)


def export_evidence(store: Store, identifier: str, destination: Path) -> None:
    job = store.status(identifier)
    if job["state"] != "prepared" or not job["evidence"]:
        raise ValueError("task does not have prepared evidence")
    destination = destination.expanduser().resolve()
    if destination.exists():
        raise ValueError("export destination already exists; it will not be overwritten")
    for parent in [destination, *destination.parents]:
        if (parent / ".git").exists():
            raise ValueError("exports must be outside Git working trees")
    source = store.root / job["evidence"]
    validate_evidence(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".video-scope-", dir=destination.parent))
    try:
        shutil.copytree(source, staging, dirs_exist_ok=True)
        validate_evidence(staging)
        # Reserve the name exclusively so a concurrent export cannot be overwritten.
        destination.mkdir()
        try:
            os.rename(staging, destination)
        except Exception:
            destination.rmdir()
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def read_batch(path: Path) -> list[dict[str, Any]]:
    entries = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
            if not isinstance(entry, dict) or set(entry) != {"source"}:
                raise ValueError('each line must contain only {"source": "local path"}')
            if not isinstance(entry["source"], str) or not entry["source"].strip():
                raise ValueError("source must be a nonempty string")
            source = Path(entry["source"]).expanduser()
            if not source.is_absolute():
                source = path.resolve().parent / source
            entries.append({"line": line_number, "source": source})
        except (ValueError, TypeError) as exc:
            entries.append({"line": line_number, "error": str(exc)})
    return entries


def read_analysis_batch(path: Path) -> list[dict[str, Any]]:
    """Read a paid-analysis JSONL manifest strictly, before any provider startup."""
    path = path.expanduser().resolve(strict=True)
    if not path.is_file():
        raise ValueError("analysis batch manifest must be a local regular file")
    if path.stat().st_size > 1_000_000:
        raise ValueError("analysis batch manifest exceeds the 1 MB safety limit")
    entries: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"analysis batch line {line_number} is not valid JSON") from exc
        if not isinstance(entry, dict) or set(entry) != {"source", "mode"}:
            raise ValueError(
                f"analysis batch line {line_number} must contain only "
                '{"source": "local path", "mode": "full|opening|both"}'
            )
        if not isinstance(entry["source"], str) or not entry["source"].strip():
            raise ValueError(f"analysis batch line {line_number} source must be nonempty")
        if not isinstance(entry["mode"], str) or entry["mode"] not in {
            "full",
            "opening",
            "both",
        }:
            raise ValueError(
                f"analysis batch line {line_number} mode must be full, opening or both"
            )
        source = Path(entry["source"]).expanduser()
        if not source.is_absolute():
            source = path.parent / source
        entries.append({"line": line_number, "source": source, "mode": entry["mode"]})
    if not entries:
        raise ValueError("analysis batch contains no entries")
    return entries
