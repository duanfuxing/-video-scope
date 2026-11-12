"""Version 1.0.0 business result and package manifest models."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

NonEmptyText = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z][A-Za-z0-9_.:-]*$",
    ),
]
Sha256 = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{64}$")]
Second = Annotated[float, Field(ge=0)]
UnitValue = Annotated[float, Field(ge=0, le=1)]


class BusinessModel(BaseModel):
    """Strict model accepting Python names or Chinese business aliases."""

    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        allow_inf_nan=False,
        validate_by_alias=True,
        validate_by_name=True,
        serialize_by_alias=True,
    )


class TimeRange(BusinessModel):
    start: Second = Field(alias="开始秒")
    end: Second = Field(alias="结束秒")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            raise ValueError("结束秒不得早于开始秒")
        return self


class OptionalTimeRange(BusinessModel):
    start: Second | None = Field(alias="开始秒")
    end: Second | None = Field(alias="结束秒")

    @model_validator(mode="after")
    def _paired_and_ordered(self) -> Self:
        if (self.start is None) != (self.end is None):
            raise ValueError("开始秒和结束秒必须同时为 null 或同时为数值")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("结束秒不得早于开始秒")
        return self


class ObservedRange(TimeRange):
    checked_content: Annotated[list[NonEmptyText], Field(min_length=1)] = Field(
        alias="已核对内容"
    )


class ContentStage(TimeRange):
    stage_id: Identifier = Field(alias="阶段标识")
    actual_content: NonEmptyText | None = Field(alias="实际内容")
    expressive_role: NonEmptyText | None = Field(alias="表达作用")


class CreativeStructure(BusinessModel):
    audience_and_context: NonEmptyText | None = Field(alias="人群与情境")
    need_motivation: NonEmptyText | None = Field(alias="需求动机")
    core_selling_point: NonEmptyText | None = Field(alias="核心买点")
    core_idea: NonEmptyText | None = Field(alias="核心创意")
    conversion_and_close: NonEmptyText | None = Field(alias="转化与收尾")
    stages: list[ContentStage] = Field(alias="内容阶段")


class Character(BusinessModel):
    character_id: Identifier = Field(alias="人物标识")
    name: NonEmptyText | None = Field(alias="称呼")
    visible_traits: NonEmptyText | None = Field(alias="可见特征")
    content_role: NonEmptyText | None = Field(alias="内容作用")


class Scene(BusinessModel):
    scene_id: Identifier = Field(alias="场景标识")
    name: NonEmptyText | None = Field(alias="名称")
    spatial_environment: NonEmptyText | None = Field(alias="空间与环境特征")


class ProductObject(BusinessModel):
    object_id: Identifier = Field(alias="对象标识")
    name: NonEmptyText | None = Field(alias="名称")
    type: NonEmptyText | None = Field(alias="类型")
    visible_traits: NonEmptyText | None = Field(alias="可见特征")
    content_role: NonEmptyText | None = Field(alias="内容作用")


class EntityRelationship(BusinessModel):
    entity_ids: Annotated[list[Identifier], Field(min_length=2)] = Field(
        alias="关联实体标识"
    )
    relationship: NonEmptyText = Field(alias="关系说明")


class Entities(BusinessModel):
    characters: list[Character] = Field(alias="人物")
    scenes: list[Scene] = Field(alias="场景")
    objects: list[ProductObject] = Field(alias="产品与道具")
    relationships: list[EntityRelationship] = Field(alias="关键关系")


class EntityState(BusinessModel):
    entity_id: Identifier = Field(alias="实体标识")
    screen_position: NonEmptyText | None = Field(alias="画面位置")
    pose_and_visibility: NonEmptyText | None = Field(alias="朝向姿态与可见状态")
    held_or_contacted_entity_ids: list[Identifier] = Field(alias="持有或接触的实体标识")


class Action(TimeRange):
    action_id: Identifier = Field(alias="动作标识")
    actor_entity_ids: Annotated[list[Identifier], Field(min_length=1)] = Field(
        alias="执行实体标识"
    )
    target_entity_ids: list[Identifier] = Field(alias="作用对象标识")
    change: NonEmptyText = Field(alias="动作与状态变化")


class ActionProcess(BusinessModel):
    initial_states: list[EntityState] = Field(alias="起始状态")
    actions: list[Action] = Field(alias="动作节点")
    final_states: list[EntityState] = Field(alias="结束状态")


class CameraMovement(TimeRange):
    movement: NonEmptyText = Field(alias="运动表现")
    speed_and_pauses: NonEmptyText | None = Field(alias="速度变化与停顿")


class ShotPresentation(BusinessModel):
    shot_size: NonEmptyText | None = Field(alias="景别")
    camera_angle: NonEmptyText | None = Field(alias="机位角度")
    initial_composition: NonEmptyText | None = Field(alias="起始构图")
    final_composition: NonEmptyText | None = Field(alias="结束构图")
    camera_movements: list[CameraMovement] = Field(alias="相机运动")
    speed_treatment: NonEmptyText | None = Field(alias="画面速度处理")


class LightColorMaterial(BusinessModel):
    lighting_and_shadow: NonEmptyText | None = Field(alias="主要光线与阴影")
    tone_and_color: NonEmptyText | None = Field(alias="影调与色彩")
    material_details: NonEmptyText | None = Field(alias="材质与关键细节")


class Transition(BusinessModel):
    previous_shot_id: Identifier | None = Field(alias="前镜标识")
    method: NonEmptyText = Field(alias="方式")
    start: Second | None = Field(alias="开始秒")
    end: Second | None = Field(alias="结束秒")
    presentation: NonEmptyText | None = Field(alias="衔接表现")
    continuous_states: list[NonEmptyText] = Field(alias="需保持的连续状态")
    intentional_changes: list[NonEmptyText] = Field(alias="刻意改变的状态")

    @model_validator(mode="after")
    def _valid_interval(self) -> Self:
        if (self.start is None) != (self.end is None):
            raise ValueError("衔接开始秒和结束秒必须同时为 null 或同时为数值")
        if self.start is not None and self.end is not None and self.end < self.start:
            raise ValueError("衔接结束秒不得早于开始秒")
        return self


class MediaReference(BusinessModel):
    path: NonEmptyText = Field(alias="路径")
    source_time: Second = Field(alias="原片时间秒")
    sha256: Sha256 = Field(alias="SHA256")

    @model_validator(mode="after")
    def _safe_frame_path(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or ":" in self.path
            or path.as_posix() != self.path
            or len(path.parts) < 3
            or path.parts[:2] != ("media", "frames")
        ):
            raise ValueError("媒体引用路径必须是 media/frames 下的规范相对 POSIX 路径")
        return self


class KeyFrame(BusinessModel):
    source_time: Second = Field(alias="原片时间秒")
    purpose: NonEmptyText = Field(alias="用途")
    media_reference: MediaReference | None = Field(alias="媒体引用")
    contains_post_text_or_sticker: bool | None = Field(alias="含后期文字或贴片")


class DynamicEvidence(TimeRange):
    checked_content: NonEmptyText = Field(alias="核对内容")


class ShotBase(TimeRange):
    shot_id: Identifier = Field(alias="镜头标识")
    character_ids: list[Identifier] = Field(alias="人物标识")
    scene_id: Identifier | None = Field(alias="场景标识")
    object_ids: list[Identifier] = Field(alias="对象标识")
    spatial_relationship: NonEmptyText | None = Field(alias="主体空间关系")
    action_process: ActionProcess = Field(alias="动作过程")
    presentation: ShotPresentation = Field(alias="镜头表现")
    light_color_material: LightColorMaterial = Field(alias="光线色彩与材质表现")
    transition: Transition = Field(alias="与前镜衔接")
    key_frames: list[KeyFrame] = Field(alias="关键帧")
    dynamic_evidence: list[DynamicEvidence] = Field(alias="动态证据片段")


class FullShot(ShotBase):
    stage_id: Identifier = Field(alias="所属阶段标识")


class OpeningShot(ShotBase):
    pass


class TimedPhrase(OptionalTimeRange):
    text: NonEmptyText = Field(alias="原文片段")


class Speech(TimeRange):
    record_id: Identifier = Field(alias="记录标识")
    speaker_character_id: Identifier | None = Field(alias="说话人物标识")
    text: NonEmptyText = Field(alias="原文")
    delivery: NonEmptyText | None = Field(alias="说话方式")
    tone_speed_pauses: NonEmptyText | None = Field(alias="语气语速与停顿")
    key_phrases: list[TimedPhrase] = Field(alias="关键字句时间")


class Sound(TimeRange):
    record_id: Identifier = Field(alias="记录标识")
    type: NonEmptyText = Field(alias="类型")
    actual_sound: NonEmptyText = Field(alias="实际声音")
    rhythm_and_relative_volume: NonEmptyText | None = Field(alias="节奏与相对音量变化")
    shot_ids: list[Identifier] = Field(alias="配合的镜头标识")
    speech_ids: list[Identifier] = Field(alias="配合的口播标识")
    relationship: NonEmptyText | None = Field(alias="配合关系")


class NormalizedRegion(BusinessModel):
    x: UnitValue | None
    y: UnitValue | None
    width: UnitValue | None
    height: UnitValue | None

    @model_validator(mode="after")
    def _inside_frame(self) -> Self:
        if self.x is not None and self.width is not None and self.x + self.width > 1:
            raise ValueError("x + width 不得超过 1")
        if self.y is not None and self.height is not None and self.y + self.height > 1:
            raise ValueError("y + height 不得超过 1")
        return self


class VisualLayout(BusinessModel):
    position: NonEmptyText | None = Field(alias="位置说明")
    normalized_region: NormalizedRegion = Field(alias="归一化区域")
    layering_and_occlusion: NonEmptyText | None = Field(alias="层级与遮挡关系")


class VisualStyle(BusinessModel):
    font_or_form: NonEmptyText | None = Field(alias="字体或元素形态")
    color_outline_background: NonEmptyText | None = Field(alias="颜色描边与底框")
    size_and_emphasis: NonEmptyText | None = Field(alias="大小与强调方式")


class OverlayChange(TimeRange):
    presentation: NonEmptyText = Field(alias="出现变化或退出表现")


class Overlay(TimeRange):
    record_id: Identifier = Field(alias="记录标识")
    type: NonEmptyText = Field(alias="类型")
    text_or_content: NonEmptyText = Field(alias="文字原文或元素内容")
    layout: VisualLayout = Field(alias="画面布局")
    style: VisualStyle = Field(alias="视觉样式")
    changes: list[OverlayChange] = Field(alias="变化过程")
    shot_ids: list[Identifier] = Field(alias="配合的镜头标识")
    speech_ids: list[Identifier] = Field(alias="配合的口播标识")
    relationship: NonEmptyText | None = Field(alias="配合关系")


class SyncPoint(BusinessModel):
    source_time: Second | None = Field(alias="原片时间秒")
    shot_ids: list[Identifier] = Field(alias="关联镜头标识")
    action_ids: list[Identifier] = Field(alias="关联动作标识")
    audiovisual_record_ids: list[Identifier] = Field(alias="关联视听记录标识")
    relationship: NonEmptyText = Field(alias="同步关系")


class AudiovisualTimeline(BusinessModel):
    speeches: list[Speech] = Field(alias="口播对白轨")
    sounds: list[Sound] = Field(alias="声音轨")
    overlays: list[Overlay] = Field(alias="文字与包装轨")
    sync_points: list[SyncPoint] = Field(alias="视听同步点")


class ExpressionMethod(BusinessModel):
    method_id: Identifier = Field(alias="方法标识")
    name: NonEmptyText = Field(alias="名称")
    type: NonEmptyText = Field(alias="类型")
    purpose: NonEmptyText = Field(alias="表达目的")
    implementation: NonEmptyText = Field(alias="具体做法")
    shot_ids: list[Identifier] = Field(alias="关联镜头标识")
    audiovisual_record_ids: list[Identifier] = Field(alias="关联视听记录标识")
    context_relationship: NonEmptyText | None = Field(alias="前后文关系")
    dependencies: list[NonEmptyText] = Field(alias="成立依赖")


class FullReferenceUsage(BusinessModel):
    reusable_points: list[NonEmptyText] = Field(alias="可借鉴点")
    applicable_conditions: list[NonEmptyText] = Field(alias="适用条件")
    required_dependencies: list[NonEmptyText] = Field(alias="必要依赖")
    nontransferable_content: list[NonEmptyText] = Field(alias="不可直接迁移的内容")
    retrieval_summary: NonEmptyText = Field(alias="检索摘要")


class PreservationSuggestion(BusinessModel):
    shot_ids: list[Identifier] = Field(alias="关联镜头标识")
    audiovisual_record_ids: list[Identifier] = Field(alias="关联视听记录标识")
    suggestion: NonEmptyText = Field(alias="建议保留的关系或表现")
    expressive_role: NonEmptyText = Field(alias="对表达的作用")


class ReplacementConstraint(BusinessModel):
    entity_id: Identifier = Field(alias="实体标识")
    conditions: Annotated[list[NonEmptyText], Field(min_length=1)] = Field(
        alias="替换时需满足的条件"
    )


class MaterialDependency(BusinessModel):
    shot_ids: list[Identifier] = Field(alias="关联镜头标识")
    audiovisual_record_ids: list[Identifier] = Field(alias="关联视听记录标识")
    purpose: NonEmptyText = Field(alias="用途")
    required_material: NonEmptyText = Field(alias="所需素材")
    must_use_real_material: bool | None = Field(alias="是否必须使用真实素材")
    reason: NonEmptyText | None = Field(alias="原因")


class ReplicationBasis(BusinessModel):
    preservation_suggestions: list[PreservationSuggestion] = Field(alias="表现保留建议")
    replacement_constraints: list[ReplacementConstraint] = Field(alias="元素替换约束")
    material_dependencies: list[MaterialDependency] = Field(alias="素材依赖")


class Uncertainty(BusinessModel):
    affected_module_or_record_id: NonEmptyText = Field(alias="涉及的模块或记录标识")
    description: NonEmptyText = Field(alias="说明")
    impact: NonEmptyText = Field(alias="对参考或复刻的影响")
    needed_evidence: NonEmptyText = Field(alias="需补充的证据")


class FullVideoResult(BusinessModel):
    source_id: NonEmptyText = Field(alias="来源视频标识")
    observed_ranges: Annotated[list[ObservedRange], Field(min_length=1)] = Field(
        alias="已观察范围"
    )
    creative_structure: CreativeStructure = Field(alias="整片创意与内容结构")
    entities: Entities = Field(alias="人物场景与产品关系")
    shots: Annotated[list[FullShot], Field(min_length=1)] = Field(alias="镜头列表")
    audiovisual_timeline: AudiovisualTimeline = Field(alias="口播声音与后期包装")
    expression_methods: list[ExpressionMethod] = Field(alias="关键表达方法")
    reference_usage: FullReferenceUsage = Field(alias="整片参考用法")
    replication_basis: ReplicationBasis = Field(alias="复刻依据")
    uncertainties: list[Uncertainty] = Field(alias="不确定项")


class ActualFirstThreeSeconds(TimeRange):
    pass


class CompleteOpening(BusinessModel):
    start: Second = Field(alias="开始秒")
    end: Second | None = Field(alias="结束秒")
    end_basis: NonEmptyText = Field(alias="结束依据")

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end is not None and self.end < self.start:
            raise ValueError("完整开场结束秒不得早于开始秒")
        return self


class OpeningRange(BusinessModel):
    actual_first_three_seconds: ActualFirstThreeSeconds = Field(alias="实际前三秒")
    complete_opening: CompleteOpening = Field(alias="完整开场")


class WholeVideoContext(BusinessModel):
    theme: NonEmptyText | None = Field(alias="视频主题")
    product_and_main_expression: NonEmptyText | None = Field(alias="产品与主要表达")
    audience_and_context: NonEmptyText | None = Field(alias="开场面向的人群与情境")


class AttractionMechanism(BusinessModel):
    first_three_seconds: NonEmptyText = Field(alias="实际前三秒表现")
    complete_opening_mechanism: NonEmptyText | None = Field(alias="完整开场如何成立")
    first_glance_hook: NonEmptyText = Field(alias="第一眼吸引点")
    viewing_expectation: NonEmptyText | None = Field(alias="建立的观看期待")
    audiovisual_coordination: NonEmptyText | None = Field(alias="视听配合方式")
    shot_ids: list[Identifier] = Field(alias="关联镜头标识")
    audiovisual_record_ids: list[Identifier] = Field(alias="关联视听记录标识")


class OpeningEndState(BusinessModel):
    source_time: Second = Field(alias="原片时间秒")
    composition: NonEmptyText = Field(alias="人物产品与构图")
    continuing_action_ids: list[Identifier] = Field(alias="仍在延续的动作标识")
    continuing_audiovisual_record_ids: list[Identifier] = Field(
        alias="仍在延续的视听记录标识"
    )


class FollowUpEvidence(TimeRange):
    actual_content: NonEmptyText = Field(alias="实际内容")
    key_frames: list[KeyFrame] = Field(alias="关键帧")


class FollowUp(BusinessModel):
    check_status: Literal["已核对有承接", "已核对无承接", "未核对"] = Field(
        alias="核对状态"
    )
    opening_end_state: OpeningEndState | None = Field(alias="开场结束状态")
    immediate_transition: NonEmptyText | None = Field(alias="与紧接后文的衔接")
    continuation_explanation: NonEmptyText | None = Field(alias="承接说明")
    relationship_to_selling_point: NonEmptyText | None = Field(alias="与产品买点的关系")
    evidence: list[FollowUpEvidence] = Field(alias="证据片段")


class OpeningReferenceUsage(BusinessModel):
    reusable_points: list[NonEmptyText] = Field(alias="可借鉴点")
    suitable_opening_and_follow_up: NonEmptyText | None = Field(alias="适用的口播开头与后文")
    required_dependencies: list[NonEmptyText] = Field(alias="必要依赖")
    original_line_dependency: NonEmptyText | None = Field(alias="对原台词的依赖")
    other_element_dependencies: NonEmptyText | None = Field(alias="对音乐文字及其他元素的依赖")
    unchanged_voiceover_boundary: NonEmptyText | None = Field(alias="不改当前口播时的借鉴边界")
    nontransferable_content: list[NonEmptyText] = Field(alias="不可直接迁移的内容")
    retrieval_summary: NonEmptyText = Field(alias="检索摘要")


class OpeningResult(BusinessModel):
    source_id: NonEmptyText = Field(alias="来源视频标识")
    observed_ranges: Annotated[list[ObservedRange], Field(min_length=1)] = Field(
        alias="已观察范围"
    )
    opening_range: OpeningRange = Field(alias="开场范围")
    whole_video_context: WholeVideoContext = Field(alias="必要的整片上下文")
    entities: Entities = Field(alias="人物场景与产品关系")
    shots: Annotated[list[OpeningShot], Field(min_length=1)] = Field(alias="开场镜头列表")
    audiovisual_timeline: AudiovisualTimeline = Field(alias="开场视听时序")
    attraction_mechanism: AttractionMechanism = Field(alias="吸引机制")
    follow_up: FollowUp = Field(alias="后文承接")
    reference_usage: OpeningReferenceUsage = Field(alias="开场参考用法")
    replication_basis: ReplicationBasis = Field(alias="复刻依据")
    uncertainties: list[Uncertainty] = Field(alias="不确定项")


class PackageFile(BusinessModel):
    path: NonEmptyText
    sha256: Sha256
    size: Annotated[int, Field(gt=0)]
    kind: Literal["result", "source", "frame"]
    source_time: Second | None = None

    @model_validator(mode="after")
    def _safe_inventory_path(self) -> Self:
        path = PurePosixPath(self.path)
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in self.path
            or ":" in self.path
            or path.as_posix() != self.path
        ):
            raise ValueError("package file path must be a normalized relative POSIX path")
        if (self.kind == "frame") != (self.source_time is not None):
            raise ValueError("only frame files must have source_time")
        if self.kind == "source" and (
            len(path.parts) != 2 or path.parts[0] != "media" or not path.name.startswith("source")
        ):
            raise ValueError("source file must be stored as media/source<extension>")
        if self.kind == "frame" and (
            len(path.parts) < 3 or path.parts[:2] != ("media", "frames")
        ):
            raise ValueError("frame files must be stored below media/frames")
        return self


class PackageManifest(BusinessModel):
    format_version: Literal["1.0.0"] = "1.0.0"
    source_id: Sha256
    analysis_id: NonEmptyText
    mode: Literal["full", "opening"]
    model: NonEmptyText
    provider: NonEmptyText
    call_ids: list[NonEmptyText]
    duration: Annotated[float, Field(gt=0)]
    files: Annotated[list[PackageFile], Field(min_length=3)]
    review_status: Literal["human_pending"] = "human_pending"
    warnings: list[NonEmptyText]

    @model_validator(mode="after")
    def _inventory(self) -> Self:
        paths = [item.path for item in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate package file paths")
        if len(self.call_ids) != len(set(self.call_ids)):
            raise ValueError("duplicate call_ids")
        if sum(item.kind == "result" for item in self.files) != 1:
            raise ValueError("package requires exactly one result file")
        if sum(item.kind == "source" for item in self.files) != 1:
            raise ValueError("package requires exactly one source file")
        if not any(item.kind == "frame" for item in self.files):
            raise ValueError("package requires at least one real frame")
        return self


_CONTRACT_MODELS: tuple[tuple[str, type[BaseModel]], ...] = (
    ("full-video.schema.json", FullVideoResult),
    ("opening.schema.json", OpeningResult),
    ("manifest.schema.json", PackageManifest),
)


def generate_contracts(directory: Path) -> list[Path]:
    """Generate deterministic version 1.0.0 JSON Schemas from the Pydantic models."""

    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for filename, model in _CONTRACT_MODELS:
        schema = model.model_json_schema(by_alias=True, mode="serialization")
        schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
        schema["$id"] = f"https://video-scope.local/contracts/1.0.0/{filename}"
        path = directory / filename
        path.write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written
