"""One explicitly authorized Gemini-native endpoint, with no automatic paid retries."""

import fnmatch
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import ROUND_UP, Decimal
from pathlib import Path
from typing import Any
from urllib.parse import quote, quote_plus, urlsplit

import httpx
from dotenv import dotenv_values
from google import genai
from google.genai import types

from video_scope.analysis_store import AnalysisStore, atomic_json
from video_scope.streaming import collect_stream, recover_stream

MODEL = "gemini-3.8-flash"
ENDPOINT = "https://api.apiyi.com"
MAX_REQUEST_BYTES = 20_000_000
MAX_OUTPUT_TOKENS = 16384


@dataclass(frozen=True)
class ProviderConfig:
    api_key: str = field(repr=False)
    model: str = MODEL
    endpoint: str = ENDPOINT
    streaming: bool = False

    @classmethod
    def load(cls, env_file: Path = Path(".env")) -> "ProviderConfig":
        values = dotenv_values(env_file) if env_file.is_file() else {}
        key = os.environ.get("GEMINI_API_KEY") or values.get("GEMINI_API_KEY")
        model = os.environ.get("GEMINI_MODEL") or values.get("GEMINI_MODEL") or MODEL
        endpoint = (
            os.environ.get("GEMINI_BASE_URL") or values.get("GEMINI_BASE_URL") or ENDPOINT
        ).rstrip("/")
        if not key:
            raise ValueError("GEMINI_API_KEY is not configured in this project's environment")
        if model != MODEL or endpoint != ENDPOINT:
            raise ValueError(
                "model or endpoint differs from the authorized fixed analysis configuration"
            )
        if urlsplit(endpoint).scheme != "https":
            raise ValueError("supplier endpoint must use HTTPS")
        return cls(api_key=key, model=model, endpoint=endpoint)


@dataclass(frozen=True)
class Pricing:
    input_per_million: Decimal
    output_per_million: Decimal
    multiplier: Decimal
    checked_at: str
    evidence_hash: str

    def estimate(self, prompt: int, output: int) -> Decimal:
        if prompt < 0 or output < 0:
            raise ValueError("negative usage is invalid")
        return (
            (Decimal(prompt) * self.input_per_million + Decimal(output) * self.output_per_million)
            * self.multiplier
            / Decimal(1_000_000)
        ).quantize(Decimal("0.00000001"), rounding=ROUND_UP)

    def reserve(self, input_token_bound: int) -> Decimal:
        return (self.estimate(input_token_bound, MAX_OUTPUT_TOKENS) * Decimal("1.25")).quantize(
            Decimal("0.000001"), rounding=ROUND_UP
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": MODEL,
            "input_usd_per_million": str(self.input_per_million),
            "output_usd_per_million": str(self.output_per_million),
            "conservative_group_multiplier": str(self.multiplier),
            "checked_at": self.checked_at,
            "pricing_evidence_sha256": self.evidence_hash,
            "source": ENDPOINT + "/api/pricing_legacy",
            "basis": "live authenticated pricing lookup; no cache discount assumed",
            "final_supplier_bill": False,
            "thoughts": "disabled via thinkingBudget=0; returned thoughts still counted",
        }


def fetch_pricing(config: ProviderConfig, client: httpx.Client | None = None) -> Pricing:
    owned = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=False)
    try:
        response = client.get(
            config.endpoint + "/api/pricing_legacy",
            headers={"Authorization": f"Bearer {config.api_key}"},
        )
        if response.status_code != 200:
            raise ValueError(
                f"pricing preflight failed (HTTP {response.status_code}); no generation"
            )
        payload = response.json()
        if payload.get("success") is not True:
            raise ValueError("supplier did not confirm pricing data")
        data = payload["data"]
        for key in ("ModelConditionalPricing", "ModelFixedPrice", "BillingExpr", "BillingMode"):
            if any(
                fnmatch.fnmatchcase(config.model, pattern) and value not in (None, "", 0, "token")
                for pattern, value in data.get(key, {}).items()
            ):
                raise ValueError(f"unhandled {key} pricing rule; do not start paid calls")
        ratio = Decimal(str(data["ModelRatio"][config.model]))
        completion = Decimal(str(data["CompletionRatio"][config.model]))
        overrides = data.get("UserConfig", {})
        if config.model in overrides.get("ModelRatio", {}):
            ratio = Decimal(str(overrides["ModelRatio"][config.model]))
        if config.model in overrides.get("CompletionRatio", {}):
            completion = Decimal(str(overrides["CompletionRatio"][config.model]))
        groups = [
            Decimal("1"),
            *(Decimal(str(value)) for value in data.get("GroupRatio", {}).values()),
        ]
        if not all(value.is_finite() and value > 0 for value in [ratio, completion, *groups]):
            raise ValueError("invalid live prices")
        pricing = Pricing(
            input_per_million=ratio * 2,
            output_per_million=ratio * 2 * completion,
            multiplier=max(groups),
            checked_at=datetime.now(UTC).isoformat(),
            evidence_hash=hashlib.sha256(response.content).hexdigest(),
        )
        if pricing.input_per_million > Decimal("0.75") or pricing.output_per_million > Decimal(
            "3.75"
        ):
            raise ValueError("live prices exceed the reviewed rate; pause for price review")
        return pricing
    except (httpx.HTTPError, KeyError, TypeError) as exc:
        raise ValueError("unable to verify supplier pricing; no generation was sent") from exc
    finally:
        if owned:
            client.close()


def usage_cost(
    response: types.GenerateContentResponse, pricing: Pricing
) -> tuple[dict[str, Any], Decimal]:
    metadata = response.usage_metadata
    if metadata is None or metadata.prompt_token_count is None:
        raise ValueError("response has no usable input usage; do not assume zero cost")
    if metadata.candidates_token_count is None or metadata.total_token_count is None:
        raise ValueError("response has no complete output usage; stop for reconciliation")
    usage = metadata.model_dump(mode="json", exclude_none=True)
    prompt = metadata.prompt_token_count
    output = max(
        metadata.candidates_token_count + (metadata.thoughts_token_count or 0),
        metadata.total_token_count - prompt,
    )
    return usage, pricing.estimate(prompt, output)


def error_diagnostic(
    exc: Exception,
    *,
    api_key: str,
    prompt: str,
    dispatched: bool,
    call_id: str,
) -> dict[str, Any]:
    """Keep bounded supplier diagnostics, never credentials or echoed request media."""
    remaining = 12000
    sensitive = re.compile(
        r"authorization|cookie|apikey|token|secret|password|credential|"
        r"inlinedata|filedata|contents|prompt|requestbody|requestheaders",
        re.I,
    )
    secrets = {
        api_key,
        quote(api_key, safe=""),
        quote_plus(api_key),
        json.dumps(api_key, ensure_ascii=True)[1:-1],
    }

    def text(value: str) -> str:
        nonlocal remaining
        for secret in sorted(secrets, key=len, reverse=True):
            if secret:
                value = value.replace(secret, "[REDACTED]")
        if prompt:
            value = value.replace(prompt, "[REQUEST_TEXT_REDACTED]")
        value = re.sub(r"https?://[^\s\"<>]+", "[URL_REDACTED]", value)
        value = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [REDACTED]", value)
        value = re.sub(
            r"(?i)((?:api[-_]?key|token|secret|password|authorization)"
            r"[\"']?\s*[:=]\s*[\"']?)[^\s,;\"'}]+",
            r"\1[REDACTED]",
            value,
        )
        value = re.sub(r"[A-Za-z0-9+/=_-]{128,}", "[LONG_DATA_REDACTED]", value)
        limit = min(4000, remaining)
        remaining -= min(len(value), limit)
        return value[:limit] + ("[TRUNCATED]" if len(value) > limit else "")

    def clean(value: Any, depth: int = 0) -> Any:
        if depth > 6 or remaining <= 0:
            return "[TRUNCATED]"
        if isinstance(value, dict):
            return {
                text(str(key)): "[REDACTED]"
                if sensitive.search(re.sub(r"[^a-zA-Z]", "", str(key)))
                else clean(item, depth + 1)
                for key, item in list(value.items())[:20]
            }
        if isinstance(value, list):
            return [clean(item, depth + 1) for item in value[:20]]
        if value is None or isinstance(value, (bool, int)):
            return value
        return text(str(value))

    code = getattr(exc, "code", None)
    response = getattr(exc, "response", None)
    if not isinstance(code, int) and isinstance(response, httpx.Response):
        code = response.status_code
    result: dict[str, Any] = {
        "call_id": call_id,
        "received_at": datetime.now(UTC).isoformat(),
        "error_type": type(exc).__name__,
        "dispatched": dispatched,
        "http_code": code if isinstance(code, int) else None,
        "message": clean(getattr(exc, "message", None) or str(exc)),
        "status": clean(getattr(exc, "status", None)),
        "details": clean(getattr(exc, "details", None)),
        "redacted": True,
    }
    if isinstance(response, httpx.Response):
        result["response_headers"] = {
            name: clean(response.headers[name])
            for name in ("x-request-id", "request-id", "x-goog-request-id", "retry-after")
            if name in response.headers
        }
    return result


def generate(
    *,
    config: ProviderConfig,
    pricing: Pricing,
    ledger: AnalysisStore,
    call: dict[str, Any],
    prompt: str,
    schema: dict[str, Any],
    media_inputs: list[dict[str, Any]],
    frame_paths: list[dict[str, Any]],
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    response_path = ledger.root / call["response_path"]
    if response_path.is_file():
        saved: dict[str, Any] = json.loads(response_path.read_text(encoding="utf-8"))
        if saved.get("estimated_usd") is None:
            raise ValueError("saved response has unknown cost; paid calls remain paused")
        ledger.finish(call["id"], saved)
        return saved
    stream_path = response_path.with_suffix(".stream.jsonl")
    if config.streaming and stream_path.is_file():
        if call["state"] not in {"sending", "unknown"}:
            raise ValueError("saved stream journal has an unexpected ledger state")
        try:
            recovered_response, recovered_progress = recover_stream(stream_path)
            audit = json.loads(
                response_path.with_suffix(".request-audit.json").read_text(encoding="utf-8")
            )
            if not isinstance(audit, dict) or audit.get("streaming") is not True:
                raise ValueError("saved stream request audit is invalid")
            recovered_usage, recovered_cost = usage_cost(recovered_response, pricing)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(
                "saved stream is incomplete or invalid; automatic resend is forbidden"
            ) from exc
        recovered_candidates = recovered_response.candidates or []
        recovered_finish = (
            str(recovered_candidates[0].finish_reason.value)
            if recovered_candidates and recovered_candidates[0].finish_reason
            else None
        )
        recovered_envelope: dict[str, Any] = {
            "call_id": call["id"],
            "model": config.model,
            "model_version": recovered_response.model_version,
            "response_id": recovered_response.response_id,
            "text": recovered_response.text,
            "finish_reason": recovered_finish,
            "wire_request": audit,
            "usage": recovered_usage,
            "estimated_usd": str(recovered_cost),
            "pricing": pricing.to_dict(),
            "received_at": datetime.now(UTC).isoformat(),
            "streaming": True,
            "chunk_count": recovered_progress["chunk_count"],
            "recovered_from_stream": True,
        }
        atomic_json(response_path, recovered_envelope)
        ledger.finish(call["id"], recovered_envelope)
        return recovered_envelope
    if call["state"] != "reserved":
        raise ValueError("this call was already dispatched; automatic resend is forbidden")
    parts = [types.Part.from_text(text=prompt)]
    raw_bytes = 0
    for item in media_inputs:
        path = Path(item["path"])
        size = path.stat().st_size
        raw_bytes += size
        if raw_bytes * 4 // 3 > MAX_REQUEST_BYTES:
            raise ValueError("media alone exceeds inline request budget")
        parts.append(
            types.Part.from_text(
                text=(
                    f"连续视听片段；原片范围 {item['start']}～{item['end']} 秒，"
                    f"片段起点偏移 {item['start']}。"
                )
            )
        )
        parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=item["mime_type"]))
    for frame in frame_paths:
        path = Path(frame["path"])
        raw_bytes += path.stat().st_size
        if raw_bytes * 4 // 3 > MAX_REQUEST_BYTES:
            raise ValueError("frames and video exceed inline request budget")
        parts.append(types.Part.from_text(text=f"静态证据帧，原片时间 {frame['time']} 秒。"))
        parts.append(types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"))
    wire: dict[str, Any] = {}
    stream_progress: dict[str, Any] = {}
    dispatched = False
    method = "streamGenerateContent" if config.streaming else "generateContent"

    def before_request(request: httpx.Request) -> None:
        nonlocal dispatched
        if str(request.url).split("?", 1)[0] != (
            config.endpoint + f"/v1beta/models/{config.model}:{method}"
        ):
            raise ValueError("SDK attempted an unexpected endpoint")
        if config.streaming and dict(request.url.params) != {"alt": "sse"}:
            raise ValueError("native streaming requires exactly alt=sse")
        if request.method != "POST" or len(request.content) > MAX_REQUEST_BYTES:
            raise ValueError("serialized request exceeds the approved request shape/size")
        if dispatched:
            raise ValueError("SDK attempted an automatic resend; blocked before network")
        wire.update(
            {
                "bytes": len(request.content),
                "sha256": hashlib.sha256(request.content).hexdigest(),
                "method": method,
                "streaming": config.streaming,
            }
        )
        atomic_json(response_path.with_suffix(".request-audit.json"), wire)
        ledger.sending(call["id"])
        dispatched = True

    try:
        with httpx.Client(
            timeout=300,
            follow_redirects=False,
            transport=transport,
            event_hooks={"request": [before_request]},
        ) as http_client:
            with genai.Client(
                vertexai=False,
                api_key=config.api_key,
                http_options=types.HttpOptions(
                    base_url=config.endpoint,
                    api_version="v1beta",
                    timeout=300_000,
                    retry_options=types.HttpRetryOptions(attempts=1),
                    httpx_client=http_client,
                ),
            ) as client:
                generation_config = types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_json_schema=schema,
                    max_output_tokens=MAX_OUTPUT_TOKENS,
                    thinking_config=types.ThinkingConfig(thinking_budget=0),
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                )
                contents = types.Content(role="user", parts=parts)
                if config.streaming:
                    chunks = client.models.generate_content_stream(
                        model=config.model,
                        contents=contents,
                        config=generation_config,
                    )
                    try:
                        response = collect_stream(
                            chunks,
                            response_path.with_suffix(".stream.jsonl"),
                            call["id"],
                            stream_progress,
                        )
                    finally:
                        close = getattr(chunks, "close", None)
                        if callable(close):
                            close()
                else:
                    response = client.models.generate_content(
                        model=config.model,
                        contents=contents,
                        config=generation_config,
                    )
        candidates = response.candidates or []
        finish = (
            str(candidates[0].finish_reason.value)
            if candidates and candidates[0].finish_reason
            else None
        )
        envelope: dict[str, Any] = {
            "call_id": call["id"],
            "model": config.model,
            "model_version": response.model_version,
            "response_id": response.response_id,
            "text": response.text,
            "finish_reason": finish,
            "wire_request": wire,
            "usage": response.usage_metadata.model_dump(mode="json", exclude_none=True)
            if response.usage_metadata
            else None,
            "estimated_usd": None,
            "pricing": pricing.to_dict(),
            "received_at": datetime.now(UTC).isoformat(),
            "streaming": config.streaming,
            "chunk_count": stream_progress.get("chunk_count", 0),
        }
        atomic_json(response_path, envelope)
        usage, cost = usage_cost(response, pricing)
        envelope.update(usage=usage, estimated_usd=str(cost))
        atomic_json(response_path, envelope)
        ledger.finish(call["id"], envelope)
        return envelope
    except Exception as exc:
        diagnostic = error_diagnostic(
            exc,
            api_key=config.api_key,
            prompt=prompt,
            dispatched=dispatched,
            call_id=call["id"],
        )
        diagnostic["wire_request"] = wire
        diagnostic["stage"] = call["stage"]
        diagnostic["streaming"] = config.streaming
        diagnostic["stream_progress"] = stream_progress
        atomic_json(response_path.with_suffix(".error.json"), diagnostic)
        if dispatched:
            existing = ledger.find_call(call["analysis_id"], call["stage"])
            if existing and existing["state"] != "succeeded":
                ledger.unknown(call["id"], json.dumps(diagnostic, sort_keys=True))
                if (
                    isinstance(exc, httpx.ConnectError)
                    and stream_progress.get("chunk_count", 0) == 0
                ):
                    ledger.settle_connect_failure(call["id"])
        raise RuntimeError(
            f"supplier step failed ({diagnostic['error_type']}, "
            f"HTTP {diagnostic['http_code']}); redacted diagnostics: "
            f"{response_path.with_suffix('.error.json')}; never blindly resend"
        ) from None
