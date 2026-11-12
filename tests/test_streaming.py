from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from video_scope.analysis_store import AnalysisStore
from video_scope.jobs import Store
from video_scope.provider import MODEL, Pricing, ProviderConfig, generate


@pytest.fixture
def ledger(tmp_path: Path) -> AnalysisStore:
    result = AnalysisStore(Store(tmp_path / "data"))
    result.campaign("streaming", Decimal("5"), MODEL, "https://api.apiyi.com")
    return result


def reserve(ledger: AnalysisStore) -> dict[str, Any]:
    analysis_id = ledger.analysis("streaming", "sample", "opening", "a" * 64)
    return ledger.reserve(analysis_id, "visual", "f" * 64, Decimal("1"))


def pricing() -> Pricing:
    return Pricing(Decimal("0.75"), Decimal("3.75"), Decimal("1"), "test", "b" * 64)


def event(payload: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode()


def text_chunk(text: str, *, output_tokens: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidates": [{"content": {"parts": [{"text": text}], "role": "model"}}],
        "modelVersion": MODEL,
        "responseId": "stream-response",
    }
    if output_tokens is not None:
        result["usageMetadata"] = {
            "promptTokenCount": 100,
            "candidatesTokenCount": output_tokens,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 100 + output_tokens,
        }
    return result


def terminal(*, usage: bool = True, model_version: str = MODEL) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidates": [{"finishReason": "STOP"}],
        "modelVersion": model_version,
        "responseId": "stream-response",
    }
    if usage:
        result["usageMetadata"] = {
            "promptTokenCount": 100,
            "candidatesTokenCount": 6,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 106,
        }
    return result


def terminal_with_metadata_part() -> dict[str, Any]:
    result = terminal()
    result["candidates"][0]["content"] = {
        "parts": [{"thoughtSignature": "dGVzdA=="}],
        "role": "model",
    }
    return result


class ScriptedStream(httpx.SyncByteStream):
    def __init__(
        self,
        chunks: list[bytes],
        *,
        after_chunk: Callable[[int], None] | None = None,
        failure_after: int | None = None,
    ) -> None:
        self.chunks = chunks
        self.after_chunk = after_chunk
        self.failure_after = failure_after

    def __iter__(self) -> Iterator[bytes]:
        for index, chunk in enumerate(self.chunks):
            yield chunk
            if self.after_chunk is not None:
                self.after_chunk(index)
            if self.failure_after == index:
                raise httpx.ReadTimeout("simulated interrupted SSE body")


def stream_response(stream: httpx.SyncByteStream) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream; charset=utf-8"},
        stream=stream,
    )


def arguments(
    ledger: AnalysisStore,
    call: dict[str, Any],
    transport: httpx.BaseTransport,
) -> dict[str, Any]:
    return {
        "config": ProviderConfig("test-secret", streaming=True),
        "pricing": pricing(),
        "ledger": ledger,
        "call": call,
        "prompt": "test",
        "schema": {"type": "object"},
        "media_inputs": [],
        "frame_paths": [],
        "transport": transport,
    }


def paths(ledger: AnalysisStore, call: dict[str, Any]) -> tuple[Path, Path, Path]:
    response = ledger.root / call["response_path"]
    return response, response.with_suffix(".stream.jsonl"), response.with_suffix(".error.json")


def records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def assert_unknown(ledger: AnalysisStore, call: dict[str, Any]) -> None:
    saved = ledger.find_call(call["analysis_id"], call["stage"])
    assert saved is not None
    assert saved["state"] == "unknown"
    assert saved["estimated_usd"] is None
    assert saved["usage_json"] is None
    summary = ledger.summary("streaming")
    assert summary["unknown_calls"] == 1
    assert summary["unsettled_reserved_usd"] == "1"


def test_streaming_success_is_durable_before_final_envelope_and_replayed(
    ledger: AnalysisStore,
    capsys: pytest.CaptureFixture[str],
) -> None:
    call = reserve(ledger)
    response_path, stream_path, _ = paths(ledger, call)
    first = event(text_chunk('{"问":"你', output_tokens=2))
    split = first.index("你".encode()) + 1
    chunks = [
        first[:split],
        first[split:],
        event(text_chunk('好"}', output_tokens=6)),
        event(terminal()),
    ]
    expected_records = [0, 1, 2, 3]

    def check_durability(index: int) -> None:
        assert not response_path.exists()
        expected = expected_records[index]
        if expected:
            assert len(records(stream_path)) == expected
        else:
            assert not stream_path.exists() or stream_path.stat().st_size == 0

    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.method == "POST"
        assert request.url.path == f"/v1beta/models/{MODEL}:streamGenerateContent"
        assert request.url.query == b"alt=sse"
        return stream_response(ScriptedStream(chunks, after_chunk=check_durability))

    args = arguments(ledger, call, httpx.MockTransport(handler))
    result = generate(**args)

    assert result["text"] == '{"问":"你好"}'
    assert result["finish_reason"] == "STOP"
    assert result["usage"]["prompt_token_count"] == 100
    assert result["usage"]["candidates_token_count"] == 6
    assert result["usage"]["total_token_count"] == 106
    assert result["estimated_usd"] == "0.00009750"
    assert result["streaming"] is True
    assert result["stream_protocol_complete"] is True
    assert result["reported_model_versions"] == [MODEL]
    assert result["chunk_count"] == 3
    assert len(records(stream_path)) == 3
    assert json.loads(response_path.read_text(encoding="utf-8")) == result
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err
    assert len(requests) == 1

    assert generate(**args) == result
    assert len(requests) == 1
    assert ProviderConfig("test-secret").streaming is False


def test_terminal_metadata_only_part_is_accepted(ledger: AnalysisStore) -> None:
    call = reserve(ledger)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream(
                [
                    event(text_chunk('{"ok":true}', output_tokens=4)),
                    event(terminal_with_metadata_part()),
                ]
            )
        )

    result = generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert result["text"] == '{"ok":true}'
    assert result["finish_reason"] == "STOP"


def test_terminal_can_refine_internal_model_version_for_same_response(
    ledger: AnalysisStore,
) -> None:
    call = reserve(ledger)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream(
                [
                    event(text_chunk('{"ok":true}', output_tokens=4)),
                    event(terminal(model_version="internal-concrete-version")),
                ]
            )
        )

    result = generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert result["model"] == MODEL
    assert result["model_version"] == "internal-concrete-version"
    assert result["reported_model_versions"] == [MODEL, "internal-concrete-version"]


def test_complete_unknown_stream_journal_is_recovered_without_network(
    ledger: AnalysisStore,
) -> None:
    call = reserve(ledger)
    ledger.sending(call["id"])
    ledger.unknown(call["id"], "collector rejected a terminal metadata part")
    current = ledger.find_call(call["analysis_id"], call["stage"])
    assert current is not None
    response_path, stream_path, _ = paths(ledger, current)
    saved_records = [
        {
            "index": 0,
            "elapsed_seconds": 1.0,
            "text_delta": '{"ok":true}',
            "finish_reason": None,
            "usage": {"prompt_token_count": 100, "total_token_count": 100},
            "response_id": "saved-response",
            "model_version": MODEL,
        },
        {
            "index": 1,
            "elapsed_seconds": 1.2,
            "text_delta": "",
            "finish_reason": "STOP",
            "usage": {
                "prompt_token_count": 100,
                "candidates_token_count": 4,
                "total_token_count": 104,
            },
            "response_id": "saved-response",
            "model_version": "internal-concrete-version",
        },
    ]
    stream_path.write_text(
        "".join(json.dumps(record) + "\n" for record in saved_records), encoding="utf-8"
    )
    response_path.with_suffix(".request-audit.json").write_text(
        json.dumps({"method": "streamGenerateContent", "streaming": True}),
        encoding="utf-8",
    )

    def no_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("complete journals must be recovered without a network request")

    result = generate(**arguments(ledger, current, httpx.MockTransport(no_request)))

    assert result["text"] == '{"ok":true}'
    assert result["finish_reason"] == "STOP"
    assert result["recovered_from_stream"] is True
    assert result["chunk_count"] == 2
    assert result["model"] == MODEL
    assert result["model_version"] == "internal-concrete-version"
    assert result["reported_model_versions"] == [MODEL, "internal-concrete-version"]
    saved = ledger.find_call(call["analysis_id"], call["stage"])
    assert saved is not None
    assert saved["state"] == "succeeded"


def test_missing_terminal_marker_preserves_partial_stream(ledger: AnalysisStore) -> None:
    call = reserve(ledger)
    response_path, stream_path, _ = paths(ledger, call)
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return stream_response(
            ScriptedStream(
                [
                    event(text_chunk("partial", output_tokens=2)),
                    event(text_chunk(" body", output_tokens=4)),
                ]
            )
        )

    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert sent == 1
    assert not response_path.exists()
    assert len(records(stream_path)) == 2
    assert_unknown(ledger, call)


def test_terminal_without_final_usage_preserves_chunks(ledger: AnalysisStore) -> None:
    call = reserve(ledger)
    response_path, stream_path, _ = paths(ledger, call)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream(
                [
                    event(text_chunk("partial", output_tokens=2)),
                    event(terminal(usage=False)),
                ]
            )
        )

    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert not response_path.exists()
    assert len(records(stream_path)) == 2
    assert_unknown(ledger, call)


def test_midstream_timeout_preserves_received_chunks(ledger: AnalysisStore) -> None:
    call = reserve(ledger)
    response_path, stream_path, error_path = paths(ledger, call)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream([event(text_chunk("partial", output_tokens=2))], failure_after=0)
        )

    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert not response_path.exists()
    assert len(records(stream_path)) == 1
    assert json.loads(error_path.read_text(encoding="utf-8"))["error_type"] == "ReadTimeout"
    assert_unknown(ledger, call)


def test_timeout_after_stop_and_usage_does_not_invalidate_protocol_completion(
    ledger: AnalysisStore,
) -> None:
    call = reserve(ledger)
    response_path, stream_path, _ = paths(ledger, call)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream(
                [event(text_chunk("complete", output_tokens=6)), event(terminal())],
                failure_after=1,
            )
        )

    result = generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert response_path.exists()
    assert len(records(stream_path)) == 2
    assert result["text"] == "complete"
    assert result["finish_reason"] == "STOP"
    assert result["stream_protocol_complete"] is True
    saved = ledger.find_call(call["analysis_id"], call["stage"])
    assert saved is not None
    assert saved["state"] == "succeeded"


def test_chunks_after_terminal_and_usage_are_not_consumed(ledger: AnalysisStore) -> None:
    call = reserve(ledger)
    _, stream_path, _ = paths(ledger, call)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream(
                [
                    event(text_chunk("complete", output_tokens=6)),
                    event(terminal()),
                    event(text_chunk("late duplicate", output_tokens=2)),
                ]
            )
        )

    result = generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert result["text"] == "complete"
    assert result["chunk_count"] == 2
    assert len(records(stream_path)) == 2


@pytest.mark.parametrize(
    "bad_event",
    [
        b"data: this-is-not-json\n\n",
        event({"error": {"code": 500, "status": "INTERNAL", "message": "stream failed"}}),
    ],
    ids=["malformed-sse", "supplier-error-event"],
)
def test_bad_sse_event_preserves_earlier_chunks(
    ledger: AnalysisStore,
    bad_event: bytes,
) -> None:
    call = reserve(ledger)
    response_path, stream_path, _ = paths(ledger, call)

    def handler(request: httpx.Request) -> httpx.Response:
        return stream_response(
            ScriptedStream([event(text_chunk("kept", output_tokens=1)), bad_event])
        )

    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    assert not response_path.exists()
    saved_records = records(stream_path)
    assert len(saved_records) == 1
    assert "kept" in json.dumps(saved_records)
    assert_unknown(ledger, call)


def test_streaming_http_400_retains_redacted_diagnostics(ledger: AnalysisStore) -> None:
    call = reserve(ledger)
    response_path, stream_path, error_path = paths(ledger, call)
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return httpx.Response(
            400,
            headers={"x-request-id": "stream-request-400"},
            json={
                "error": {
                    "code": 400,
                    "status": "INVALID_ARGUMENT",
                    "message": "bad streaming schema test-secret",
                }
            },
        )

    with pytest.raises(RuntimeError, match="HTTP 400"):
        generate(**arguments(ledger, call, httpx.MockTransport(handler)))

    diagnostic = json.loads(error_path.read_text(encoding="utf-8"))
    assert diagnostic["http_code"] == 400
    assert diagnostic["dispatched"] is True
    assert diagnostic["response_headers"] == {"x-request-id": "stream-request-400"}
    assert "test-secret" not in error_path.read_text(encoding="utf-8")
    assert not response_path.exists()
    assert not stream_path.exists() or stream_path.stat().st_size == 0
    assert sent == 1
    assert_unknown(ledger, call)


def test_existing_unknown_streaming_call_is_never_resent_or_released(
    ledger: AnalysisStore,
) -> None:
    call = reserve(ledger)
    ledger.sending(call["id"])
    ledger.unknown(call["id"], "previous stream outcome is unknown")
    current = ledger.find_call(call["analysis_id"], call["stage"])
    assert current is not None
    _, stream_path, _ = paths(ledger, current)
    stream_path.write_text('{"partial":"durable"}\n', encoding="utf-8")
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        return stream_response(ScriptedStream([event(terminal())]))

    with pytest.raises(ValueError, match="already dispatched|resend"):
        generate(**arguments(ledger, current, httpx.MockTransport(handler)))

    assert sent == 0
    assert records(stream_path) == [{"partial": "durable"}]
    assert_unknown(ledger, current)
