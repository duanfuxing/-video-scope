from __future__ import annotations

import copy
import importlib.util
import json
import shutil
import subprocess
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from test_results import _full_payload, _opening_payload

import video_scope.analysis as analysis_module
from video_scope.analysis import (
    _base_prompt,
    _canonical_ids,
    _scope_check,
    analyze_sample,
    section_schema,
)
from video_scope.analysis_store import AnalysisStore, atomic_json
from video_scope.evidence import digest
from video_scope.export import export_result, validate_package
from video_scope.jobs import Store
from video_scope.media import MediaInfo
from video_scope.provider import Pricing, ProviderConfig
from video_scope.validation import validate_result


def _schema_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        reference = value.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            refs.add(reference.rsplit("/", 1)[-1])
        for key, child in value.items():
            if key != "$defs":
                refs.update(_schema_refs(child))
    elif isinstance(value, list):
        for child in value:
            refs.update(_schema_refs(child))
    return refs


def test_prompt_requires_jump_cut_orientation_claim_and_timing_discipline() -> None:
    prompt = _base_prompt(
        "full",
        "a" * 64,
        5.0,
        {
            "observed_intervals": [{"start": 0.0, "end": 5.0}],
            "av_sync": {"status": "measured", "audio_start_offset": 0.0},
            "shot_candidates": [{"start": 0.0, "end": 5.0, "confidence": 0.0}],
        },
    )

    assert "硬切/跳切后的新镜头" in prompt
    assert "手心/手背" in prompt
    assert "口播声称" in prompt
    assert "区间外的后续语句" in prompt


def test_section_schema_keeps_only_transitive_subset_references() -> None:
    schema = section_schema("full", ["镜头列表"])

    assert list(schema["properties"]) == ["镜头列表"]
    assert schema["required"] == ["镜头列表"]
    assert "title" not in schema
    assert schema["$defs"]["KeyFrame"]["properties"]["媒体引用"] == {"type": "null"}

    reachable = _schema_refs(schema)
    pending = list(reachable)
    while pending:
        name = pending.pop()
        for reference in _schema_refs(schema["$defs"][name]):
            if reference not in reachable:
                reachable.add(reference)
                pending.append(reference)
    assert set(schema["$defs"]) == reachable


def test_canonical_ids_rewrite_definitions_and_all_references() -> None:
    payload = _full_payload()
    aliases = {
        "stage_1": "phase-z",
        "character_1": "talent-z",
        "scene_1": "set-z",
        "object_1": "prop-z",
        "shot_1": "take-z",
        "shot_2": "take-y",
        "action_1": "move-z",
        "action_2": "move-y",
    }

    def rename(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: rename(child) for key, child in value.items()}
        if isinstance(value, list):
            return [rename(child) for child in value]
        return aliases.get(value, value)

    canonical = _canonical_ids(rename(payload))

    validate_result(canonical, "full", 4.0)
    serialized = json.dumps(canonical, ensure_ascii=False)
    assert not any(alias in serialized for alias in aliases.values())
    assert canonical["镜头列表"][1]["与前镜衔接"]["前镜标识"] == "shot_1"
    assert canonical["镜头列表"][0]["所属阶段标识"] == "stage_1"
    assert canonical["人物场景与产品关系"]["关键关系"][0]["关联实体标识"] == [
        "character_1",
        "object_1",
    ]
    action = canonical["镜头列表"][1]["动作过程"]["动作节点"][0]
    assert action["动作标识"] == "action_2"
    assert action["执行实体标识"] == ["character_1"]
    assert action["作用对象标识"] == ["object_1"]


def test_canonical_ids_rewrite_uncertainty_paths_without_changing_prose() -> None:
    payload = _full_payload()
    payload["不确定项"] = [
        {
            "涉及的模块或记录标识": "镜头列表.shot_2.动作过程[action_2] / shot_20",
            "说明": "shot_2 是供应商原文，保留审计依据",
            "对参考或复刻的影响": "需核对动作",
            "需补充的证据": "逐帧画面",
        }
    ]
    # Reverse definition order so canonical names differ from the original names.
    payload["镜头列表"].reverse()
    canonical = _canonical_ids(payload)
    issue = canonical["不确定项"][0]
    assert issue["涉及的模块或记录标识"] == "镜头列表.shot_1.动作过程[action_1] / shot_20"
    assert issue["说明"] == "shot_2 是供应商原文，保留审计依据"


def test_scope_check_rejects_timed_evidence_outside_provided_twelve_seconds() -> None:
    result = _opening_payload()
    result["开场镜头列表"][1]["关键帧"][0]["原片时间秒"] = 12.01
    media = {"observed_intervals": [{"start": 0.0, "end": 12.0}]}

    with pytest.raises(ValueError, match="timed evidence exceeds"):
        _scope_check(result, media, 13.0)

    result = _opening_payload()
    result["后文承接"]["核对状态"] = "已核对无承接"
    with pytest.raises(ValueError, match="partial viewing"):
        _scope_check(result, media, 13.0)


def test_input_bound_uses_observed_seconds_not_decoded_frame_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duration = 12.1
    info = MediaInfo(
        duration=duration,
        width=1920,
        height=1080,
        has_audio=True,
        rotation=0,
        frame_times=[index / 60 for index in range(726)],
    )
    monkeypatch.setattr(analysis_module, "probe", lambda path: info)
    media = {
        "media_inputs": [{"path": str(tmp_path / "video.mp4")}],
        "frame_paths": [{"path": "one.jpg"}, {"path": "two.jpg"}],
    }
    prompt = "测试"
    schema = {"type": "object"}

    bound = analysis_module._input_bound(media, prompt, schema)

    text_bound = len(prompt.encode()) + len(json.dumps(schema, ensure_ascii=False).encode())
    assert bound == text_bound + 4096 * (13 + 2) + int(duration * 512)
    assert bound < 100_000


@pytest.fixture
def synthetic_video(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg") or importlib.util.find_spec("cv2") is None:
        pytest.skip("ffmpeg and OpenCV are required")
    path = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=96x64:rate=4:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "mpeg4",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def _pricing() -> Pricing:
    return Pricing(Decimal("0.01"), Decimal("0.01"), Decimal("1"), "test", "b" * 64)


def _offline_generate(
    calls: list[dict[str, Any]],
) -> Callable[..., dict[str, Any]]:
    def generate(**kwargs: Any) -> dict[str, Any]:
        schema = kwargs["schema"]
        properties = schema["properties"]
        mode = (
            "full" if "镜头列表" in properties or "口播声音与后期包装" in properties else "opening"
        )
        payload = _full_payload() if mode == "full" else _opening_payload()
        staged = {name: copy.deepcopy(payload[name]) for name in properties}
        call = kwargs["call"]
        ledger = kwargs["ledger"]
        assert all(Path(item["path"]).is_file() for item in kwargs["media_inputs"])
        assert all(Path(item["path"]).is_file() for item in kwargs["frame_paths"])
        envelope = {
            "call_id": call["id"],
            "text": json.dumps(staged, ensure_ascii=False),
            "finish_reason": "STOP",
            "usage": {"prompt_token_count": 1, "candidates_token_count": 1},
            "estimated_usd": "0",
        }
        atomic_json(ledger.root / call["response_path"], envelope)
        ledger.finish(call["id"], envelope)
        calls.append(
            {
                "mode": mode,
                "stage": call["stage"],
                "properties": list(schema["properties"]),
            }
        )
        return envelope

    return generate


def _ledger(tmp_path: Path) -> tuple[AnalysisStore, ProviderConfig]:
    ledger = AnalysisStore(Store(tmp_path / "runtime"))
    config = ProviderConfig("offline-only")
    ledger.campaign("campaign", Decimal("5"), config.model, config.endpoint)
    return ledger, config


@pytest.mark.parametrize("mode", ["full", "opening"])
def test_analyze_sample_offline_stages_and_exports_real_media(
    mode: str,
    synthetic_video: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, config = _ledger(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(analysis_module, "generate", _offline_generate(calls))
    sample = {
        "identity": f"fixture-{mode}",
        "mode": mode,
        "path": str(synthetic_video),
        "source_sha256": digest(synthetic_video),
    }

    result = analyze_sample(
        ledger=ledger,
        campaign_id="campaign",
        sample=sample,
        config=config,
        pricing=_pricing(),
    )

    package = Path(result["package"])
    manifest = validate_package(package)
    assert result["reused"] is False
    assert manifest["mode"] == mode
    assert manifest["source_id"] == sample["source_sha256"]
    assert [call["stage"] for call in calls] == ["visual", "audiovisual"]
    assert calls[0]["properties"] == analysis_module._FIELDS[mode][0]
    assert calls[1]["properties"] == analysis_module._FIELDS[mode][1]
    assert len(manifest["call_ids"]) == 2
    assert (package / ("full-video.json" if mode == "full" else "opening.json")).is_file()
    assert next((package / "media").glob("source.*")).stat().st_size > 0


def test_saved_paid_outputs_are_reused_after_export_failure_without_resend(
    synthetic_video: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, config = _ledger(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(analysis_module, "generate", _offline_generate(calls))
    real_export = export_result
    attempts = 0

    def fail_first_export(**kwargs: Any) -> Path:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("simulated export interruption")
        return real_export(**kwargs)

    monkeypatch.setattr(analysis_module, "export_result", fail_first_export)
    sample = {
        "identity": "fixture-full",
        "mode": "full",
        "path": str(synthetic_video),
        "source_sha256": digest(synthetic_video),
    }
    arguments: dict[str, Any] = {
        "ledger": ledger,
        "campaign_id": "campaign",
        "sample": sample,
        "config": config,
        "pricing": _pricing(),
    }

    with pytest.raises(RuntimeError, match="simulated export interruption"):
        analyze_sample(**arguments)
    assert len(calls) == 2
    summary = ledger.summary("campaign")
    assert [call["state"] for call in summary["calls"]] == ["succeeded", "succeeded"]

    recovered = analyze_sample(**arguments)

    assert len(calls) == 2
    assert attempts == 2
    manifest = validate_package(Path(recovered["package"]))
    assert set(manifest["call_ids"]) == {call["id"] for call in summary["calls"]}
    assert ledger.summary("campaign")["call_count"] == 2


def test_only_invalid_module_is_repaired(
    synthetic_video: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger, config = _ledger(tmp_path)
    calls: list[dict[str, Any]] = []
    generate = _offline_generate(calls)

    def invalid_second_stage(**kwargs: Any) -> dict[str, Any]:
        envelope = generate(**kwargs)
        if kwargs["call"]["stage"] == "audiovisual":
            payload = json.loads(envelope["text"])
            payload["复刻依据"]["表现保留建议"] = [
                {
                    "关联镜头标识": ["shot_1"],
                    "关联视听记录标识": ["action_1"],
                    "建议保留的关系或表现": "保留动作",
                    "对表达的作用": "动作演示",
                }
            ]
            envelope["text"] = json.dumps(payload, ensure_ascii=False)
            atomic_json(ledger.root / kwargs["call"]["response_path"], envelope)
        return envelope

    monkeypatch.setattr(analysis_module, "generate", invalid_second_stage)
    result = analyze_sample(
        ledger=ledger,
        campaign_id="campaign",
        config=config,
        pricing=_pricing(),
        sample={
            "identity": "fixture",
            "mode": "opening",
            "path": str(synthetic_video),
            "source_sha256": digest(synthetic_video),
        },
    )
    validate_package(Path(result["package"]))
    assert len(calls) == 3
    assert calls[-1]["stage"] == "repair-module-v1"
    assert calls[-1]["properties"] == ["复刻依据"]
    with pytest.raises(ValueError, match="bounded module"):
        analysis_module.repair_fields("opening", "unrecognized validation issue")
