"""Read-only review queues built from validated local result packages."""

import json
from pathlib import Path
from typing import Any

from video_scope.export import validate_package
from video_scope.result_models import FullVideoResult, OpeningResult
from video_scope.validation import validate_result


def review_package(directory: Path) -> dict[str, Any]:
    """Expose model claims for review; never mark them as independently verified."""
    manifest = validate_package(directory)
    root = directory.expanduser().resolve(strict=True)
    result_file = next(item for item in manifest["files"] if item["kind"] == "result")
    payload = json.loads((root / result_file["path"]).read_text(encoding="utf-8"))
    result = validate_result(payload, manifest["mode"], manifest["duration"])
    if not isinstance(result, (FullVideoResult, OpeningResult)):
        raise ValueError("unsupported review result type")
    queue: list[dict[str, Any]] = []

    def add(kind: str, start: float | None, end: float | None, claim: Any) -> None:
        queue.append(
            {"kind": kind, "start": start, "end": end, "status": "pending", "claim": claim}
        )

    for shot in result.shots:
        add("shot", shot.start, shot.end, shot.model_dump(mode="json", by_alias=True))
    timeline = result.audiovisual_timeline
    for kind, records in (
        ("speech", timeline.speeches),
        ("sound", timeline.sounds),
        ("overlay", timeline.overlays),
    ):
        for record in records:
            add(kind, record.start, record.end, record.model_dump(mode="json", by_alias=True))
    for point in timeline.sync_points:
        add(
            "sync",
            point.source_time,
            point.source_time,
            point.model_dump(mode="json", by_alias=True),
        )
    queue.sort(key=lambda item: (item["start"] is None, item["start"] or 0))
    for index, item in enumerate(queue, 1):
        item["review_id"] = f"review-{index:04d}"

    return {
        "report_version": "1.0.0",
        "source_id": manifest["source_id"],
        "analysis_id": manifest["analysis_id"],
        "mode": manifest["mode"],
        "package_integrity": "passed",
        "package_review_status": manifest["review_status"],
        "review_status": "pending",
        "source_media": next(
            item["path"] for item in manifest["files"] if item["kind"] == "source"
        ),
        "observed_ranges": payload["已观察范围"],
        "opening_range": payload.get("开场范围"),
        "follow_up": payload.get("后文承接"),
        "uncertainties": payload["不确定项"],
        "warnings": manifest["warnings"],
        "instructions": [
            "claim 是模型原始结论，pending 不代表已核实。",
            "媒体相对路径以原结果包目录为基准。",
            "逐项回看原片，核对动作、台词、声音、文字及时间边界。",
            "静态关键帧不能证明动态过程或声音；同步点需同时核对音画。",
            "只在已观察范围内复核；不确定项与开场后文承接需单独核对。",
        ],
        "queue": queue,
    }
