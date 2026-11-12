"""Immutable local-analysis inputs and durable single/batch progress reports."""

import hashlib
import json
from pathlib import Path
from typing import Any

from video_scope.analysis_store import AnalysisStore, atomic_json
from video_scope.evidence import digest


def analysis_modes(mode: str) -> list[str]:
    if mode == "both":
        return ["opening", "full"]
    if mode in {"full", "opening"}:
        return [mode]
    raise ValueError("--mode must be full, opening or both")


def run_slug(value: str) -> str:
    if not value or len(value) > 48 or not value.replace("-", "").isalnum():
        raise ValueError("--run-id must be 1-48 letters, digits or hyphens")
    return value


def local_campaign_id(source_hash: str, mode: str, run_id: str | None = None) -> str:
    return (
        f"local-{run_slug(run_id)}" if run_id is not None else f"local-{source_hash[:16]}-{mode}-v1"
    )


def ensure_local_input(
    campaign_dir: Path,
    *,
    campaign_id: str,
    source: Path,
    source_hash: str,
    requested_mode: str,
    modes: list[str],
) -> None:
    path = campaign_dir / "input.json"
    identity = {
        "format_version": "1.0",
        "kind": "local-file-analysis",
        "campaign_id": campaign_id,
        "source_sha256": source_hash,
        "requested_mode": requested_mode,
        "modes": modes,
    }
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing local analysis input manifest is invalid") from exc
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in identity.items()
        ):
            raise ValueError("existing local analysis campaign belongs to different inputs")
        return
    if path.exists():
        raise ValueError("local analysis input manifest path is not a regular file")
    atomic_json(path, {**identity, "authorized_source_path": str(source)})


def save_local_progress(
    ledger: AnalysisStore,
    campaign_dir: Path,
    *,
    campaign_id: str,
    source_hash: str,
    requested_mode: str,
    modes: list[str],
    completed: list[dict[str, Any]],
    error: Exception | None = None,
) -> dict[str, Any]:
    path = campaign_dir / "progress.json"
    records: dict[str, dict[str, Any]] = {}
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing local analysis progress is invalid") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("campaign_id") != campaign_id
            or existing.get("source_sha256") != source_hash
            or existing.get("requested_mode") != requested_mode
            or not isinstance(existing.get("completed"), list)
        ):
            raise ValueError("existing local analysis progress belongs to different inputs")
        for record in existing["completed"]:
            if (
                not isinstance(record, dict)
                or record.get("mode") not in modes
                or record.get("source_sha256") != source_hash
                or record.get("identity") != f"local:{source_hash}"
            ):
                raise ValueError("existing local analysis progress has an invalid record")
            records[str(record["mode"])] = record
    for record in completed:
        mode = record.get("mode")
        if (
            mode not in modes
            or record.get("source_sha256") != source_hash
            or record.get("identity") != f"local:{source_hash}"
        ):
            raise ValueError("local analysis progress contains an unauthorized mode")
        records[str(mode)] = record
    ordered = [records[mode] for mode in modes if mode in records]
    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "source_sha256": source_hash,
        "requested_mode": requested_mode,
        "status": (
            "needs_attention"
            if error is not None
            else "completed"
            if len(ordered) == len(modes)
            else "in_progress"
        ),
        "completed_count": len(ordered),
        "total": len(modes),
        "completed": ordered,
        "ledger": ledger.summary(campaign_id),
    }
    if error is not None:
        payload["last_error"] = f"{type(error).__name__}: {error}"[:2000]
    atomic_json(path, payload)
    return payload


def resolve_analysis_batch(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    units: set[tuple[str, str]] = set()
    for index, entry in enumerate(entries, 1):
        source = Path(entry["source"]).expanduser().resolve(strict=True)
        if not source.is_file():
            raise ValueError(f"analysis batch line {entry['line']} source is not a regular file")
        source_hash = digest(source)
        requested_mode = str(entry["mode"])
        modes = analysis_modes(requested_mode)
        for selected_mode in modes:
            unit = (source_hash, selected_mode)
            if unit in units:
                raise ValueError(
                    f"analysis batch line {entry['line']} duplicates an earlier "
                    f"{selected_mode} analysis for the same content"
                )
            units.add(unit)
        items.append(
            {
                "item_id": f"item-{index:04d}-{source_hash[:12]}-{requested_mode}",
                "line": entry["line"],
                "source": source,
                "source_sha256": source_hash,
                "requested_mode": requested_mode,
                "modes": modes,
            }
        )
    if len(units) > 4:
        raise ValueError(
            "analysis batch supports at most four expanded full/opening analyses "
            "under the reviewed 12-call safety limit"
        )
    return items


def batch_campaign_id(items: list[dict[str, Any]], run_id: str | None = None) -> str:
    if run_id is not None:
        return f"batch-{run_slug(run_id)}"
    identity = [
        {
            "source_sha256": item["source_sha256"],
            "requested_mode": item["requested_mode"],
        }
        for item in items
    ]
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return f"batch-{fingerprint[:16]}-v1"


def _batch_identity(campaign_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "format_version": "1.0",
        "kind": "local-analysis-batch",
        "campaign_id": campaign_id,
        "items": [
            {
                "item_id": item["item_id"],
                "source_sha256": item["source_sha256"],
                "requested_mode": item["requested_mode"],
                "modes": item["modes"],
            }
            for item in items
        ],
    }


def ensure_batch_input(
    campaign_dir: Path, *, campaign_id: str, manifest: Path, items: list[dict[str, Any]]
) -> None:
    path = campaign_dir / "input.json"
    identity = _batch_identity(campaign_id, items)
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing batch input manifest is invalid") from exc
        if not isinstance(existing, dict) or any(
            existing.get(key) != value for key, value in identity.items()
        ):
            raise ValueError("existing analysis batch campaign belongs to different inputs")
        return
    if path.exists():
        raise ValueError("analysis batch input manifest path is not a regular file")
    atomic_json(
        path,
        {
            **identity,
            "authorized_manifest_path": str(manifest),
            "sources": [
                {
                    "item_id": item["item_id"],
                    "line": item["line"],
                    "path": str(item["source"]),
                }
                for item in items
            ],
        },
    )


def batch_units(items: list[dict[str, Any]]) -> list[tuple[dict[str, Any], str]]:
    return [(item, mode) for item in items for mode in item["modes"]]


def save_batch_progress(
    ledger: AnalysisStore,
    campaign_dir: Path,
    *,
    campaign_id: str,
    items: list[dict[str, Any]],
    completed: list[dict[str, Any]],
    failures: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> dict[str, Any]:
    path = campaign_dir / "progress.json"
    units = batch_units(items)
    allowed = {(str(item["item_id"]), mode) for item, mode in units}
    expected = {(str(item["item_id"]), mode): item for item, mode in units}
    records: dict[tuple[str, str], dict[str, Any]] = {}
    failed_records: dict[tuple[str, str], dict[str, Any]] = {}

    def validate_outcome(value: dict[str, Any], *, failure: bool) -> tuple[str, str]:
        key = (str(value.get("item_id")), str(value.get("mode")))
        item = expected.get(key)
        if (
            item is None
            or value.get("line") != item["line"]
            or value.get("requested_mode") != item["requested_mode"]
            or value.get("source_sha256") != item["source_sha256"]
        ):
            raise ValueError("analysis batch progress outcome does not match its input")
        if failure:
            if not isinstance(value.get("error"), str) or not isinstance(
                value.get("blocking"), bool
            ):
                raise ValueError("analysis batch progress failure is invalid")
        elif value.get("identity") != f"batch:{item['item_id']}":
            raise ValueError("analysis batch progress record identity is invalid")
        return key

    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("existing analysis batch progress is invalid") from exc
        if (
            not isinstance(existing, dict)
            or existing.get("campaign_id") != campaign_id
            or not isinstance(existing.get("completed"), list)
            or not isinstance(existing.get("failures", []), list)
        ):
            raise ValueError("existing analysis batch progress belongs to different inputs")
        for record in existing["completed"]:
            if not isinstance(record, dict):
                raise ValueError("existing analysis batch progress has an invalid record")
            key = validate_outcome(record, failure=False)
            if key not in allowed:
                raise ValueError("existing analysis batch progress has an unauthorized record")
            records[key] = record
        for failure in existing.get("failures", []):
            if not isinstance(failure, dict):
                raise ValueError("existing analysis batch progress has an invalid failure")
            key = validate_outcome(failure, failure=True)
            if key not in allowed:
                raise ValueError("existing analysis batch progress has an unauthorized failure")
            failed_records[key] = failure
        if records.keys() & failed_records.keys():
            raise ValueError("existing analysis batch progress has conflicting outcomes")
    for record in completed:
        key = validate_outcome(record, failure=False)
        if key not in allowed:
            raise ValueError("analysis batch progress contains an unauthorized record")
        records[key] = record
        failed_records.pop(key, None)
    for failure in failures or []:
        key = validate_outcome(failure, failure=True)
        if key not in allowed:
            raise ValueError("analysis batch progress contains an unauthorized failure")
        failed_records[key] = failure
        records.pop(key, None)
    ordered = [
        records[(str(item["item_id"]), mode)]
        for item, mode in units
        if (str(item["item_id"]), mode) in records
    ]
    ordered_failures = [
        failed_records[(str(item["item_id"]), mode)]
        for item, mode in units
        if (str(item["item_id"]), mode) in failed_records
    ]
    attempted = len(ordered) + len(ordered_failures)
    blocking_failure = any(failure.get("blocking") is True for failure in ordered_failures)
    payload: dict[str, Any] = {
        "campaign_id": campaign_id,
        "status": (
            "needs_attention"
            if error is not None or blocking_failure
            else "completed"
            if len(ordered) == len(units)
            else "completed_with_failures"
            if attempted == len(units)
            else "in_progress_with_failures"
            if ordered_failures
            else "in_progress"
        ),
        "completed_count": len(ordered),
        "failure_count": len(ordered_failures),
        "total": len(units),
        "entry_count": len(items),
        "completed": ordered,
        "failures": ordered_failures,
        "ledger": ledger.summary(campaign_id),
    }
    if error is not None:
        payload["last_error"] = f"{type(error).__name__}: {error}"[:2000]
    atomic_json(path, payload)
    return payload


def batch_unknown_source(
    ledger: AnalysisStore, call_id: str, campaign_id: str, items: list[dict[str, Any]]
) -> str:
    with ledger.connect() as database:
        row = database.execute(
            """SELECT supplier_calls.campaign_id, analyses.source_hash
               FROM supplier_calls JOIN analyses ON analyses.id=supplier_calls.analysis_id
               WHERE supplier_calls.id=?""",
            (call_id,),
        ).fetchone()
    if row is None or row["campaign_id"] != campaign_id:
        raise ValueError("unknown call does not belong to this analysis batch")
    source_hash = str(row["source_hash"])
    if source_hash not in {str(item["source_sha256"]) for item in items}:
        raise ValueError("unknown call source is outside this analysis batch")
    return source_hash
