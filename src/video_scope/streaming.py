"""Durable Gemini text-stream collection; incomplete streams are never results."""

import json
import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from google.genai import types


def _unsupported_non_text_part(part: types.Part) -> bool:
    if part.text is not None or part.thought:
        return False
    payload = part.model_dump(mode="python", exclude_none=True)
    metadata_only = {"thought", "thought_signature", "part_metadata"}
    return any(name not in metadata_only for name in payload)


def recover_stream(
    path: Path,
) -> tuple[types.GenerateContentResponse, dict[str, Any]]:
    """Rebuild a complete terminal response from its durable JSONL journal."""
    raw = path.read_bytes()
    if not raw or len(raw) > 8_000_000 or not raw.endswith(b"\n"):
        raise ValueError("saved stream journal is empty, truncated, or oversized")
    try:
        lines = raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise ValueError("saved stream journal is not UTF-8") from exc
    if len(lines) > 8192:
        raise ValueError("saved stream journal has too many chunks")

    text: list[str] = []
    finish: types.FinishReason | None = None
    final_usage: types.GenerateContentResponseUsageMetadata | None = None
    response_id: str | None = None
    model_version: str | None = None
    elapsed_seconds: float | int = 0
    for index, line in enumerate(lines):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError("saved stream journal contains invalid JSON") from exc
        if not isinstance(record, dict) or record.get("index") != index:
            raise ValueError("saved stream journal indices are not contiguous")
        delta = record.get("text_delta")
        if not isinstance(delta, str):
            raise ValueError("saved stream journal has a non-text delta")
        if finish is not None and delta:
            raise ValueError("saved stream journal has text after its terminal event")
        text.append(delta)

        current_finish = record.get("finish_reason")
        if current_finish is not None:
            if not isinstance(current_finish, str):
                raise ValueError("saved stream journal has an invalid finish reason")
            try:
                parsed_finish = types.FinishReason(current_finish)
            except ValueError as exc:
                raise ValueError("saved stream journal has an unknown finish reason") from exc
            if parsed_finish != types.FinishReason.FINISH_REASON_UNSPECIFIED:
                if finish is not None and finish != parsed_finish:
                    raise ValueError("saved stream journal has conflicting finish reasons")
                finish = parsed_finish

        usage = record.get("usage")
        if finish is not None and usage is not None:
            if not isinstance(usage, dict):
                raise ValueError("saved stream journal has invalid final usage")
            final_usage = types.GenerateContentResponseUsageMetadata.model_validate(usage)

        for field, previous in (
            ("response_id", response_id),
            ("model_version", model_version),
        ):
            current = record.get(field)
            if current is not None and not isinstance(current, str):
                raise ValueError(f"saved stream journal has invalid {field}")
            if previous is not None and current is not None and previous != current:
                raise ValueError(f"saved stream journal changed {field}")
        response_id = record.get("response_id") or response_id
        model_version = record.get("model_version") or model_version
        current_elapsed = record.get("elapsed_seconds", elapsed_seconds)
        if isinstance(current_elapsed, bool) or not isinstance(current_elapsed, (int, float)):
            raise ValueError("saved stream journal has invalid elapsed time")
        elapsed_seconds = current_elapsed

    if finish is None:
        raise ValueError("saved stream journal has no terminal finish reason")
    if final_usage is None:
        raise ValueError("saved stream journal has no final usage")
    progress = {
        "chunk_count": len(lines),
        "characters": sum(len(item) for item in text),
        "terminal_received": True,
        "elapsed_seconds": elapsed_seconds,
        "recovered": True,
    }
    return (
        types.GenerateContentResponse(
            candidates=[
                types.Candidate(
                    content=types.Content(
                        role="model", parts=[types.Part.from_text(text="".join(text))]
                    ),
                    finish_reason=finish,
                )
            ],
            usage_metadata=final_usage,
            response_id=response_id,
            model_version=model_version,
        ),
        progress,
    )


def collect_stream(
    chunks: Iterator[types.GenerateContentResponse],
    path: Path,
    call_id: str,
    progress: dict[str, Any],
) -> types.GenerateContentResponse:
    started = time.monotonic()
    text: list[str] = []
    finish: types.FinishReason | None = None
    final_usage: types.GenerateContentResponseUsageMetadata | None = None
    response_id: str | None = None
    model_version: str | None = None
    total_bytes = 0
    progress.update(chunk_count=0, characters=0, terminal_received=False)
    print(f"流式请求 {call_id}：等待首个分块（空闲读取上限 300 秒）", file=sys.stderr, flush=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as journal:
        for chunk in chunks:
            candidates = chunk.candidates or []
            if len(candidates) > 1:
                raise ValueError("stream returned multiple candidates; cannot merge safely")
            candidate = candidates[0] if candidates else None
            parts = candidate.content.parts if candidate and candidate.content else []
            delta = "".join(part.text or "" for part in parts or [] if not part.thought)
            current_finish = candidate.finish_reason if candidate else None
            response_id = chunk.response_id or response_id
            model_version = chunk.model_version or model_version
            record = {
                "index": progress["chunk_count"],
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "text_delta": delta,
                "finish_reason": current_finish.value if current_finish else None,
                "usage": chunk.usage_metadata.model_dump(mode="json", exclude_none=True)
                if chunk.usage_metadata
                else None,
                "response_id": chunk.response_id,
                "model_version": chunk.model_version,
            }
            line = json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n"
            total_bytes += len(line.encode("utf-8"))
            if total_bytes > 8_000_000 or progress["chunk_count"] >= 8192:
                raise ValueError("stream exceeded the bounded response journal")
            journal.write(line)
            journal.flush()
            os.fsync(journal.fileno())
            progress["chunk_count"] += 1
            progress["characters"] += len(delta)
            progress["elapsed_seconds"] = record["elapsed_seconds"]
            print(
                f"流式 {call_id[:8]}：{progress['chunk_count']} 块，"
                f"{progress['characters']} 字符，{record['elapsed_seconds']} 秒",
                file=sys.stderr,
                flush=True,
            )
            if not candidates and chunk.usage_metadata is None:
                raise ValueError("stream event has neither candidate nor usage; not a valid result")
            if finish is not None and delta:
                raise ValueError("stream returned text after its terminal event")
            if any(_unsupported_non_text_part(part) for part in parts or []):
                raise ValueError("structured text stream returned an unsupported non-text part")
            text.append(delta)
            if current_finish and current_finish != types.FinishReason.FINISH_REASON_UNSPECIFIED:
                if finish is not None and finish != current_finish:
                    raise ValueError("stream returned conflicting terminal reasons")
                finish = current_finish
                progress["terminal_received"] = True
            # Usage is cumulative, not a per-chunk delta; require it at/after the terminal event.
            if finish is not None and chunk.usage_metadata is not None:
                final_usage = chunk.usage_metadata
    if finish is None:
        raise ValueError("stream ended without a terminal finish reason")
    if final_usage is None:
        raise ValueError("stream ended without final usage; cost is unknown")
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(
                    role="model", parts=[types.Part.from_text(text="".join(text))]
                ),
                finish_reason=finish,
            )
        ],
        usage_metadata=final_usage,
        response_id=response_id,
        model_version=model_version,
    )
