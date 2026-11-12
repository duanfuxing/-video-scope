import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from video_scope.analysis_store import AnalysisStore, atomic_json
from video_scope.jobs import Store
from video_scope.provider import MODEL, Pricing, ProviderConfig, fetch_pricing, generate


@pytest.fixture
def ledger(tmp_path: Path) -> AnalysisStore:
    store = AnalysisStore(Store(tmp_path / "data"))
    store.campaign("test", Decimal("5"), MODEL, "https://api.apiyi.com")
    return store


def analysis(ledger: AnalysisStore, key: str = "first") -> str:
    return ledger.analysis("test", key, "opening", "a" * 64)


def pricing() -> Pricing:
    return Pricing(Decimal("0.75"), Decimal("3.75"), Decimal("1"), "test", "b" * 64)


def call(ledger: AnalysisStore, stage: str = "visual") -> dict[str, Any]:
    return ledger.reserve(analysis(ledger), stage, "f" * 64, Decimal("1"))


def test_budget_is_shared_across_restart(ledger: AnalysisStore) -> None:
    ledger.reserve(analysis(ledger), "visual", "a", Decimal("3"))
    restarted = AnalysisStore(ledger.store)
    with pytest.raises(ValueError, match="budget"):
        restarted.reserve(analysis(restarted, "second"), "visual", "b", Decimal("2"))
    assert restarted.summary("test")["unsettled_reserved_usd"] == "3"
    with pytest.raises(ValueError, match="cannot be reset"):
        restarted.campaign("test", Decimal("4"), MODEL, "https://api.apiyi.com")


def test_unknown_request_stops_all_new_calls(ledger: AnalysisStore) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    restarted = AnalysisStore(ledger.store)
    restarted.recover(reserved["analysis_id"])
    assert restarted.summary("test")["unknown_calls"] == 1
    with pytest.raises(ValueError, match="unresolved"):
        restarted.reserve(analysis(restarted, "other"), "visual", "b", Decimal("1"))
    assert restarted.summary("test")["unsettled_reserved_usd"] == "1"


def test_saved_connect_error_is_settled_and_same_stage_can_be_reserved(
    ledger: AnalysisStore,
) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    ledger.unknown(reserved["id"], "connect failed")
    response_path = ledger.root / reserved["response_path"]
    atomic_json(
        response_path.with_suffix(".error.json"),
        {
            "error_type": "ConnectError",
            "http_code": None,
            "dispatched": True,
            "stream_progress": {"chunk_count": 0},
        },
    )
    response_path.with_suffix(".stream.jsonl").touch()

    ledger.recover(reserved["analysis_id"])

    archived = ledger.find_call(
        reserved["analysis_id"], f"visual#connect-failed-{reserved['id'][:12]}"
    )
    assert archived is not None
    assert archived["state"] == "connection_failed"
    assert archived["estimated_usd"] == "0"
    summary = ledger.summary("test")
    assert summary["unknown_calls"] == 0
    assert summary["connection_failed_calls"] == 1
    retry = ledger.reserve(
        reserved["analysis_id"], "visual", reserved["request_hash"], Decimal("1"), "main"
    )
    assert retry["id"] != reserved["id"]


def test_explicit_abandon_preserves_unknown_cost_and_allows_same_stage_retry(
    ledger: AnalysisStore,
) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    ledger.unknown(reserved["id"], "read timed out after dispatch")

    archived = ledger.abandon_unknown(
        reserved["id"],
        source_hash="a" * 64,
        reason="user explicitly authorized a retry",
    )

    assert archived["id"] == reserved["id"]
    assert archived["state"] == "abandoned"
    assert archived["stage"].startswith("visual#abandoned-")
    audit = json.loads(archived["error"])
    assert audit["billing_outcome"] == "unknown"
    assert audit["reservation_usd"] == "1"
    summary = ledger.summary("test")
    assert summary["unknown_calls"] == 0
    assert summary["unsettled_reserved_usd"] == "0"
    assert summary["abandoned_calls"] == 1
    assert summary["abandoned_reservation_usd"] == "1"

    retried = ledger.reserve(
        reserved["analysis_id"], "visual", reserved["request_hash"], Decimal("1"), "main"
    )
    assert retried["id"] != reserved["id"]
    ledger.reserve(reserved["analysis_id"], "audiovisual", "e" * 64, Decimal("1"), "main")


def test_abandon_rejects_an_unknown_call_for_another_source(ledger: AnalysisStore) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    ledger.unknown(reserved["id"], "timeout")

    with pytest.raises(ValueError, match="selected source"):
        ledger.abandon_unknown(
            reserved["id"], source_hash="b" * 64, reason="retry a different source"
        )

    unchanged = ledger.find_call(reserved["analysis_id"], "visual")
    assert unchanged is not None
    assert unchanged["state"] == "unknown"


def test_saved_response_recovered_without_resend(ledger: AnalysisStore) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    atomic_json(
        ledger.root / reserved["response_path"],
        {
            "estimated_usd": "0.02",
            "usage": {"prompt_token_count": 10},
            "text": "{}",
        },
    )
    ledger.recover(reserved["analysis_id"])
    assert ledger.summary("test")["unknown_calls"] == 0
    assert ledger.summary("test")["estimated_usage_cost_usd"] == "0.02"
    assert ledger.summary("test")["unsettled_reserved_usd"] == "0"


def response_payload(usage: bool = True) -> dict[str, Any]:
    result: dict[str, Any] = {
        "candidates": [
            {
                "content": {"parts": [{"text": '{"ok":true}'}], "role": "model"},
                "finishReason": "STOP",
            }
        ],
        "modelVersion": MODEL,
        "responseId": "test-id",
    }
    if usage:
        result["usageMetadata"] = {
            "promptTokenCount": 100,
            "candidatesTokenCount": 20,
            "thoughtsTokenCount": 0,
            "totalTokenCount": 120,
        }
    return result


def test_actual_sdk_serialization_and_saved_response(ledger: AnalysisStore) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = json.loads(request.content)
        thinking = body["generationConfig"]["thinkingConfig"]
        assert thinking.get("thinkingBudget", thinking.get("thinking_budget")) == 0
        assert body["generationConfig"]["maxOutputTokens"] == 16384
        assert body["generationConfig"]["responseMimeType"] == "application/json"
        return httpx.Response(200, json=response_payload())

    reserved = call(ledger)
    args: dict[str, Any] = dict(
        config=ProviderConfig("test-secret"),
        pricing=pricing(),
        ledger=ledger,
        call=reserved,
        prompt="test",
        schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
        media_inputs=[],
        frame_paths=[],
        transport=httpx.MockTransport(handler),
    )
    result = generate(**args)
    assert result["estimated_usd"] == "0.00015000"
    assert len(requests) == 1
    assert generate(**args) == result
    assert len(requests) == 1
    assert "test-secret" not in (ledger.root / reserved["response_path"]).read_text()
    with pytest.raises(ValueError, match="only an unknown call"):
        ledger.reject(reserved["id"], reason="failed calls are not billed")
    saved = ledger.find_call(reserved["analysis_id"], reserved["stage"])
    assert saved is not None
    assert saved["state"] == "succeeded"


@pytest.mark.parametrize("failure", ["timeout", "500", "usage"])
def test_sdk_does_not_retry_unknown_calls(ledger: AnalysisStore, failure: str) -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        if failure == "timeout":
            raise httpx.ReadTimeout("test", request=request)
        if failure == "500":
            return httpx.Response(500, json={"error": {"code": 500, "message": "temporary"}})
        return httpx.Response(200, json=response_payload(False))

    reserved = call(ledger)
    with pytest.raises(RuntimeError):
        generate(
            config=ProviderConfig("secret"),
            pricing=pricing(),
            ledger=ledger,
            call=reserved,
            prompt="test",
            schema={"type": "object"},
            media_inputs=[],
            frame_paths=[],
            transport=httpx.MockTransport(handler),
        )
    assert count == 1
    assert ledger.summary("test")["unknown_calls"] == 1
    assert ledger.summary("test")["unsettled_reserved_usd"] == "1"
    if failure in {"timeout", "500"}:
        with pytest.raises(ValueError, match="dispatched HTTP 400"):
            ledger.reject(reserved["id"], reason="failed calls are not billed")
        saved = ledger.find_call(reserved["analysis_id"], reserved["stage"])
        assert saved is not None
        assert saved["state"] == "unknown"


def test_connect_error_is_archived_without_an_automatic_retry(ledger: AnalysisStore) -> None:
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        raise httpx.ConnectError("TLS handshake failed", request=request)

    reserved = call(ledger)
    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(
            config=ProviderConfig("secret", streaming=True),
            pricing=pricing(),
            ledger=ledger,
            call=reserved,
            prompt="test",
            schema={"type": "object"},
            media_inputs=[],
            frame_paths=[],
            transport=httpx.MockTransport(handler),
        )

    assert sent == 1
    summary = ledger.summary("test")
    assert summary["unknown_calls"] == 0
    assert summary["connection_failed_calls"] == 1
    assert summary["unsettled_reserved_usd"] == "0"


def test_response_file_blocks_http_rejection(ledger: AnalysisStore) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    ledger.unknown(reserved["id"], "HTTP 400")
    response_path = ledger.root / reserved["response_path"]
    atomic_json(response_path.with_suffix(".error.json"), {"dispatched": True, "http_code": 400})
    atomic_json(response_path, {"estimated_usd": "0.01", "usage": {"totalTokenCount": 1}})

    with pytest.raises(ValueError, match="durable response"):
        ledger.reject(reserved["id"], reason="failed calls are not billed")
    saved = ledger.find_call(reserved["analysis_id"], reserved["stage"])
    assert saved is not None
    assert saved["state"] == "unknown"
    assert saved["estimated_usd"] is None


@pytest.mark.parametrize(
    ("column", "value"),
    [("estimated_usd", "0.01"), ("usage_json", '{"totalTokenCount": 1}')],
)
def test_estimated_usage_blocks_http_rejection(
    ledger: AnalysisStore, column: str, value: str
) -> None:
    reserved = call(ledger)
    ledger.sending(reserved["id"])
    ledger.unknown(reserved["id"], "HTTP 400")
    response_path = ledger.root / reserved["response_path"]
    atomic_json(response_path.with_suffix(".error.json"), {"dispatched": True, "http_code": 400})
    with ledger.connect() as db:
        db.execute(f"UPDATE supplier_calls SET {column}=? WHERE id=?", (value, reserved["id"]))

    with pytest.raises(ValueError, match="estimated usage"):
        ledger.reject(reserved["id"], reason="failed calls are not billed")
    saved = ledger.find_call(reserved["analysis_id"], reserved["stage"])
    assert saved is not None
    assert saved["state"] == "unknown"


def test_live_price_shape_verified_without_generation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/pricing_legacy"
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "ModelRatio": {MODEL: 0.375},
                    "CompletionRatio": {MODEL: 5},
                    "GroupRatio": {},
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = fetch_pricing(ProviderConfig("secret"), client)
    assert result.input_per_million == Decimal("0.75")
    assert result.output_per_million == Decimal("3.75")
    assert result.reserve(1000) > result.estimate(1000, 16384)


@pytest.mark.parametrize("json_error", [True, False])
def test_error_details_are_saved_redacted_without_retry(
    ledger: AnalysisStore,
    json_error: bool,
) -> None:
    from urllib.parse import quote

    key = "test-private/key+value"
    prompt = "private business prompt"
    sent = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent
        sent += 1
        message = (
            "Invalid schema at generationConfig.responseJsonSchema: too many states. "
            + key
            + " "
            + quote(key, safe="")
            + " "
            + prompt
            + " https://example.com/?token=other-secret Bearer other-credential "
            + "api_key=third-credential "
            + "A" * 500
        )
        headers = {"x-request-id": "provider-request-123", "set-cookie": "private-cookie"}
        if json_error:
            return httpx.Response(
                400,
                headers=headers,
                json={
                    "error": {
                        "code": 400,
                        "status": "INVALID_ARGUMENT",
                        "message": message,
                        "details": [
                            {
                                "fieldViolations": [
                                    {
                                        "field": "generationConfig",
                                        "description": "Schema too complex",
                                    }
                                ]
                            }
                        ],
                        "api_key": "different-key",
                        "contents": [{"inlineData": {"data": "private"}}],
                    }
                },
            )
        return httpx.Response(400, headers=headers, text=message)

    reserved = call(ledger)
    with pytest.raises(RuntimeError, match="never blindly resend"):
        generate(
            config=ProviderConfig(key),
            pricing=pricing(),
            ledger=ledger,
            call=reserved,
            prompt=prompt,
            schema={"type": "object"},
            media_inputs=[],
            frame_paths=[],
            transport=httpx.MockTransport(handler),
        )
    path = (ledger.root / reserved["response_path"]).with_suffix(".error.json")
    diagnostic = json.loads(path.read_text())
    persisted = path.read_text() + json.dumps(ledger.summary("test"))
    for secret in (
        key,
        quote(key, safe=""),
        prompt,
        "other-secret",
        "other-credential",
        "third-credential",
        "different-key",
        "private-cookie",
        "A" * 500,
    ):
        assert secret not in persisted
    assert "Invalid schema" in diagnostic["message"]
    assert diagnostic["http_code"] == 400
    assert diagnostic["dispatched"] is True
    assert diagnostic["response_headers"] == {"x-request-id": "provider-request-123"}
    if json_error:
        assert "Schema too complex" in json.dumps(diagnostic["details"])
    assert sent == 1
    assert ledger.summary("test")["unknown_calls"] == 1
    assert ledger.summary("test")["unsettled_reserved_usd"] == "1"

    reason = "supplier policy verified: dispatched HTTP 400 failures are not billed"
    ledger.reject(reserved["id"], reason=reason)
    ledger.reject(reserved["id"], reason=reason)
    with pytest.raises(ValueError, match="reason cannot be changed"):
        ledger.reject(reserved["id"], reason="different policy finding")
    ledger.recover(reserved["analysis_id"])

    saved = ledger.find_call(reserved["analysis_id"], reserved["stage"])
    assert saved is not None
    assert saved["state"] == "rejected"
    assert saved["estimated_usd"] == "0"
    assert saved["usage_json"] is None
    assert saved["error"] == reason
    assert ledger.summary("test")["unknown_calls"] == 0
    assert ledger.summary("test")["estimated_usage_cost_usd"] == "0"
    assert ledger.summary("test")["unsettled_reserved_usd"] == "0"

    ledger.reserve(analysis(ledger, "after-rejection"), "visual", "a", Decimal("1"))
    assert sent == 1


def test_error_details_are_bounded() -> None:
    from google.genai.errors import ClientError

    from video_scope.provider import error_diagnostic

    exc = ClientError(
        400,
        {
            "error": {
                "message": "failure " * 10000,
                "details": [{"item": "details " * 10000}] * 1000,
            }
        },
    )
    diagnostic = error_diagnostic(
        exc, api_key="private-key", prompt="", dispatched=True, call_id="test"
    )
    assert len(json.dumps(diagnostic)) < 20000
    assert "TRUNCATED" in json.dumps(diagnostic)
