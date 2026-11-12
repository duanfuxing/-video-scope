from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest
from pydantic import ValidationError

from video_scope.evidence import EvidenceManifest, digest, write_manifest
from video_scope.export import export_result, validate_package
from video_scope.result_models import FullVideoResult, OpeningResult, generate_contracts
from video_scope.review import review_package
from video_scope.validation import result_type, validate_result


def _entities() -> dict[str, Any]:
    return {
        "人物": [
            {
                "人物标识": "character_1",
                "称呼": "演示者",
                "可见特征": None,
                "内容作用": "展示",
            }
        ],
        "场景": [{"场景标识": "scene_1", "名称": "桌面", "空间与环境特征": None}],
        "产品与道具": [
            {
                "对象标识": "object_1",
                "名称": "杯子",
                "类型": "产品",
                "可见特征": None,
                "内容作用": "演示对象",
            }
        ],
        "关键关系": [{"关联实体标识": ["character_1", "object_1"], "关系说明": "人物持杯"}],
    }


def _shot(
    identifier: str,
    start: float,
    end: float,
    previous: str | None,
    *,
    frame_time: float,
    stage: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "镜头标识": identifier,
        "开始秒": start,
        "结束秒": end,
        "人物标识": ["character_1"],
        "场景标识": "scene_1",
        "对象标识": ["object_1"],
        "主体空间关系": "人物在杯子后方",
        "动作过程": {
            "起始状态": [
                {
                    "实体标识": "object_1",
                    "画面位置": "中央",
                    "朝向姿态与可见状态": "正面可见",
                    "持有或接触的实体标识": ["character_1"],
                }
            ],
            "动作节点": [
                {
                    "动作标识": f"action_{identifier[-1]}",
                    "开始秒": start,
                    "结束秒": end,
                    "执行实体标识": ["character_1"],
                    "作用对象标识": ["object_1"],
                    "动作与状态变化": "拿起并展示",
                }
            ],
            "结束状态": [
                {
                    "实体标识": "object_1",
                    "画面位置": "中央",
                    "朝向姿态与可见状态": "正面可见",
                    "持有或接触的实体标识": ["character_1"],
                }
            ],
        },
        "镜头表现": {
            "景别": "中景",
            "机位角度": None,
            "起始构图": "居中",
            "结束构图": "居中",
            "相机运动": [
                {
                    "开始秒": start,
                    "结束秒": end,
                    "运动表现": "固定",
                    "速度变化与停顿": None,
                }
            ],
            "画面速度处理": None,
        },
        "光线色彩与材质表现": {
            "主要光线与阴影": None,
            "影调与色彩": None,
            "材质与关键细节": None,
        },
        "与前镜衔接": {
            "前镜标识": previous,
            "方式": "片头" if previous is None else "硬切",
            "开始秒": None if previous is None else start,
            "结束秒": None if previous is None else start,
            "衔接表现": None,
            "需保持的连续状态": [],
            "刻意改变的状态": [],
        },
        "关键帧": [
            {
                "原片时间秒": frame_time,
                "用途": "核对构图",
                "媒体引用": None,
                "含后期文字或贴片": None,
            }
        ],
        "动态证据片段": [{"开始秒": start, "结束秒": end, "核对内容": "核对动作"}],
    }
    if stage is not None:
        value["所属阶段标识"] = stage
    return value


def _timeline() -> dict[str, Any]:
    return {"口播对白轨": [], "声音轨": [], "文字与包装轨": [], "视听同步点": []}


def _replication() -> dict[str, Any]:
    return {"表现保留建议": [], "元素替换约束": [], "素材依赖": []}


def _full_payload(source_id: str = "a" * 64) -> dict[str, Any]:
    return {
        "来源视频标识": source_id,
        "已观察范围": [{"开始秒": 0.0, "结束秒": 4.0, "已核对内容": ["动态画面"]}],
        "整片创意与内容结构": {
            "人群与情境": None,
            "需求动机": None,
            "核心买点": "便携",
            "核心创意": "现场展示",
            "转化与收尾": None,
            "内容阶段": [
                {
                    "阶段标识": "stage_1",
                    "开始秒": 0.0,
                    "结束秒": 4.0,
                    "实际内容": "展示产品",
                    "表达作用": "建立理解",
                }
            ],
        },
        "人物场景与产品关系": _entities(),
        "镜头列表": [
            _shot("shot_1", 0.0, 2.0, None, frame_time=0.02, stage="stage_1"),
            _shot("shot_2", 2.0005, 4.0, "shot_1", frame_time=2.5, stage="stage_1"),
        ],
        "口播声音与后期包装": _timeline(),
        "关键表达方法": [],
        "整片参考用法": {
            "可借鉴点": ["动作清晰"],
            "适用条件": [],
            "必要依赖": [],
            "不可直接迁移的内容": [],
            "检索摘要": "人物展示便携杯",
        },
        "复刻依据": _replication(),
        "不确定项": [],
    }


def _opening_payload(source_id: str = "a" * 64) -> dict[str, Any]:
    return {
        "来源视频标识": source_id,
        "已观察范围": [{"开始秒": 0.0, "结束秒": 3.5, "已核对内容": ["动态画面"]}],
        "开场范围": {
            "实际前三秒": {"开始秒": 0.0, "结束秒": 3.0},
            "完整开场": {
                "开始秒": 0.0,
                "结束秒": None,
                "结束依据": "原片观察范围不足，无法确认",
            },
        },
        "必要的整片上下文": {
            "视频主题": None,
            "产品与主要表达": "杯子展示",
            "开场面向的人群与情境": None,
        },
        "人物场景与产品关系": _entities(),
        "开场镜头列表": [
            _shot("shot_1", 0.0, 2.0, None, frame_time=0.02),
            _shot("shot_2", 2.0, 3.5, "shot_1", frame_time=3.0),
        ],
        "开场视听时序": _timeline(),
        "吸引机制": {
            "实际前三秒表现": "立即拿起产品",
            "完整开场如何成立": None,
            "第一眼吸引点": "动作直接",
            "建立的观看期待": None,
            "视听配合方式": None,
            "关联镜头标识": ["shot_1", "shot_2"],
            "关联视听记录标识": [],
        },
        "后文承接": {
            "核对状态": "未核对",
            "开场结束状态": None,
            "与紧接后文的衔接": None,
            "承接说明": None,
            "与产品买点的关系": None,
            "证据片段": [],
        },
        "开场参考用法": {
            "可借鉴点": ["直接展示"],
            "适用的口播开头与后文": None,
            "必要依赖": [],
            "对原台词的依赖": None,
            "对音乐文字及其他元素的依赖": None,
            "不改当前口播时的借鉴边界": None,
            "不可直接迁移的内容": [],
            "检索摘要": "直接拿起杯子的开场",
        },
        "复刻依据": _replication(),
        "不确定项": [
            {
                "涉及的模块或记录标识": "完整开场",
                "说明": "结束边界未知",
                "对参考或复刻的影响": "不能确定交接状态",
                "需补充的证据": "继续观察原片",
            }
        ],
    }


def _make_evidence(root: Path) -> EvidenceManifest:
    root.mkdir()
    source = root / "source.mp4"
    source.write_bytes(b"self-contained original video fixture")
    frames = root / "frames"
    frames.mkdir()
    frame_records: list[dict[str, Any]] = []
    for index, time in enumerate([0.0, 1.0, 2.0, 3.0, 3.9], 1):
        frame = frames / f"{index:03d}.jpg"
        frame.write_bytes(f"real frame at {time}".encode())
        frame_records.append(
            {
                "path": frame.relative_to(root).as_posix(),
                "sha256": digest(frame),
                "size": frame.stat().st_size,
                "kind": "frame",
                "time": time,
            }
        )
    audio = root / "audio.wav"
    audio.write_bytes(b"audio fixture")
    manifest = EvidenceManifest.model_validate(
        {
            "source_id": digest(source),
            "media": {
                "duration": 4.0,
                "width": 16,
                "height": 16,
                "has_audio": True,
                "rotation": 0,
                "frame_times": [0.0, 1.0, 2.0, 3.0, 3.9],
            },
            "files": [
                {
                    "path": "source.mp4",
                    "sha256": digest(source),
                    "size": source.stat().st_size,
                    "kind": "source",
                },
                *frame_records,
                {
                    "path": "audio.wav",
                    "sha256": digest(audio),
                    "size": audio.stat().st_size,
                    "kind": "audio",
                },
            ],
        }
    )
    write_manifest(root, manifest)
    (root / "provider-token.txt").write_text("must not be exported")
    return manifest


def test_result_types_are_strict_and_serialize_with_chinese_keys() -> None:
    assert result_type("full") is FullVideoResult
    assert result_type("opening") is OpeningResult
    with pytest.raises(ValueError, match="exactly"):
        result_type("both")

    parsed = validate_result(_full_payload(), "full", 4.0)
    serialized = parsed.model_dump()
    assert "来源视频标识" in serialized
    assert "source_id" not in serialized
    assert "镜头列表" in serialized

    bad = _full_payload()
    bad["unexpected"] = True
    with pytest.raises(ValidationError):
        validate_result(bad, "full", 4.0)
    bad = _full_payload()
    bad["来源视频标识"] = "   "
    with pytest.raises(ValidationError):
        validate_result(bad, "full", 4.0)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda value: (
                value["镜头列表"][1].__setitem__("开始秒", 2.1),
                value["镜头列表"][1]["动作过程"]["动作节点"][0].__setitem__("开始秒", 2.1),
                value["镜头列表"][1]["镜头表现"]["相机运动"][0].__setitem__("开始秒", 2.1),
                value["镜头列表"][1]["动态证据片段"][0].__setitem__("开始秒", 2.1),
            ),
            "coverage has a gap",
        ),
        (
            lambda value: value["镜头列表"][0]["动作过程"]["动作节点"][0].__setitem__(
                "结束秒", 2.1
            ),
            "outside shot",
        ),
        (
            lambda value: value["镜头列表"][0].__setitem__("人物标识", ["missing"]),
            "unresolved references",
        ),
    ],
)
def test_full_validation_rejects_timeline_and_reference_errors(mutate: Any, message: str) -> None:
    payload = _full_payload()
    mutate(payload)
    with pytest.raises(ValueError, match=message):
        validate_result(payload, "full", 4.0)


def test_opening_requires_exact_first_three_and_coherent_unknown_end() -> None:
    assert isinstance(validate_result(_opening_payload(), "opening", 4.0), OpeningResult)

    short = _opening_payload()
    short["已观察范围"][0]["结束秒"] = 2.0
    short["开场范围"]["实际前三秒"]["结束秒"] = 2.0
    short["开场镜头列表"] = [_shot("shot_1", 0.0, 2.0, None, frame_time=1.0)]
    short["吸引机制"]["关联镜头标识"] = ["shot_1"]
    assert isinstance(validate_result(short, "opening", 2.0), OpeningResult)

    bad = _opening_payload()
    bad["开场范围"]["实际前三秒"]["结束秒"] = 2.9
    with pytest.raises(ValueError, match=r"min\(3"):
        validate_result(bad, "opening", 4.0)

    bad = _opening_payload()
    bad["后文承接"]["核对状态"] = "已核对无承接"
    with pytest.raises(ValueError, match="未核对"):
        validate_result(bad, "opening", 4.0)


def test_known_complete_opening_requires_matching_end_state() -> None:
    payload = _opening_payload()
    payload["开场范围"]["完整开场"]["结束秒"] = 3.2
    payload["后文承接"]["开场结束状态"] = {
        "原片时间秒": 3.2,
        "人物产品与构图": "人物仍持杯",
        "仍在延续的动作标识": ["action_2"],
        "仍在延续的视听记录标识": [],
    }
    assert isinstance(validate_result(payload, "opening", 4.0), OpeningResult)

    payload["后文承接"]["开场结束状态"]["原片时间秒"] = 3.1
    with pytest.raises(ValueError, match="must match"):
        validate_result(payload, "opening", 4.0)


def test_export_is_self_contained_binds_only_real_frames_and_validates(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence = _make_evidence(evidence_dir)
    payload = _full_payload(evidence.source_id)
    # A caller/model-supplied media reference is discarded and rebuilt.
    payload["镜头列表"][0]["关键帧"][0]["媒体引用"] = {
        "路径": "media/frames/fake.jpg",
        "原片时间秒": 0.02,
        "SHA256": "f" * 64,
    }
    output = tmp_path / "result-package"

    assert (
        export_result(
            result=payload,
            mode="full",
            evidence_dir=evidence_dir,
            output=output,
            analysis_id="analysis-1",
            model="model-1",
            provider="provider-1",
            call_ids=["call-1"],
        )
        == output
    )

    manifest = validate_package(output)
    assert manifest["format_version"] == "1.0.0"
    assert manifest["source_id"] == evidence.source_id
    assert manifest["review_status"] == "human_pending"
    assert len(manifest["warnings"]) == 1
    paths = {item["path"] for item in manifest["files"]}
    assert "full-video.json" in paths
    assert "media/source.mp4" in paths
    assert "media/frames/001.jpg" in paths
    assert not any("audio" in path or "token" in path for path in paths)

    exported = json.loads((output / "full-video.json").read_text())
    first_ref = exported["镜头列表"][0]["关键帧"][0]["媒体引用"]
    second_ref = exported["镜头列表"][1]["关键帧"][0]["媒体引用"]
    assert first_ref["路径"] == "media/frames/001.jpg"
    assert first_ref["原片时间秒"] == 0.0
    assert first_ref["SHA256"] == digest(output / first_ref["路径"])
    assert second_ref is None
    assert not (output / "audio.wav").exists()
    assert not (output / "provider-token.txt").exists()

    with pytest.raises(ValueError, match="already exists"):
        export_result(
            result=payload,
            mode="full",
            evidence_dir=evidence_dir,
            output=output,
            analysis_id="analysis-2",
            model="model-1",
            provider="provider-1",
            call_ids=[],
        )


@pytest.mark.parametrize("mode", ["full", "opening"])
def test_review_queue_preserves_claims_and_package(tmp_path: Path, mode: str) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence = _make_evidence(evidence_dir)
    payload = (_full_payload if mode == "full" else _opening_payload)(evidence.source_id)
    output = export_result(
        result=payload,
        mode=mode,
        evidence_dir=evidence_dir,
        output=tmp_path / "package",
        analysis_id="review-analysis",
        model="model",
        provider="provider",
        call_ids=[],
    )
    before = {p.relative_to(output): digest(p) for p in output.rglob("*") if p.is_file()}
    report = review_package(output)
    assert report["review_status"] == "pending"
    assert report["package_integrity"] == "passed"
    assert report["observed_ranges"] == payload["已观察范围"]
    assert report["opening_range"] == payload.get("开场范围")
    assert report["follow_up"] == payload.get("后文承接")
    queue = report["queue"]
    assert queue
    starts = [item["start"] for item in queue if item["start"] is not None]
    assert starts == sorted(starts)
    assert all(item["status"] == "pending" for item in queue)
    assert len({item["review_id"] for item in queue}) == len(queue)
    timeline = payload["口播声音与后期包装" if mode == "full" else "开场视听时序"]
    for kind, key in (
        ("speech", "口播对白轨"),
        ("sound", "声音轨"),
        ("overlay", "文字与包装轨"),
        ("sync", "视听同步点"),
    ):
        claims = [item["claim"] for item in queue if item["kind"] == kind]
        assert len(claims) == len(timeline[key])
        assert all(claim in claims for claim in timeline[key])
    assert before == {p.relative_to(output): digest(p) for p in output.rglob("*") if p.is_file()}
    (output / report["source_media"]).write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="integrity"):
        review_package(output)


def test_package_validation_detects_integrity_and_inventory_changes(tmp_path: Path) -> None:
    evidence_dir = tmp_path / "evidence"
    evidence = _make_evidence(evidence_dir)
    output = export_result(
        result=_opening_payload(evidence.source_id),
        mode="opening",
        evidence_dir=evidence_dir,
        output=tmp_path / "opening-package",
        analysis_id="analysis-opening",
        model="model-1",
        provider="provider-1",
        call_ids=[],
    )
    (output / "opening.json").write_text("{}")
    with pytest.raises(ValueError, match="integrity failure"):
        validate_package(output)

    second = export_result(
        result=_opening_payload(evidence.source_id),
        mode="opening",
        evidence_dir=evidence_dir,
        output=tmp_path / "opening-package-2",
        analysis_id="analysis-opening-2",
        model="model-1",
        provider="provider-1",
        call_ids=[],
    )
    (second / "raw-provider-response.json").write_text("{}")
    with pytest.raises(ValueError, match="inventory mismatch"):
        validate_package(second)


def test_generated_contracts_are_deterministic_and_current(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_paths = generate_contracts(first)
    second_paths = generate_contracts(second)
    assert [path.name for path in first_paths] == [
        "full-video.schema.json",
        "opening.schema.json",
        "manifest.schema.json",
    ]
    for first_path, second_path in zip(first_paths, second_paths, strict=True):
        assert first_path.read_bytes() == second_path.read_bytes()
        tracked = Path(__file__).parents[1] / "contracts" / "1.0.0" / first_path.name
        assert first_path.read_bytes() == tracked.read_bytes()
        schema = cast("dict[str, Any]", json.loads(first_path.read_text()))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
