"""Business validation for version 1.0.0 VideoScope results."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

from pydantic import BaseModel

from video_scope.result_models import (
    AudiovisualTimeline,
    Entities,
    FullVideoResult,
    OpeningResult,
    ReplicationBasis,
    ShotBase,
)

_TIME_TOLERANCE = 0.001
_MEDIA_REFERENCE_TOLERANCE = 0.05


def result_type(mode: str) -> type[BaseModel]:
    """Return the formal result type for one analysis mode."""

    if mode == "full":
        return FullVideoResult
    if mode == "opening":
        return OpeningResult
    raise ValueError("mode must be exactly 'full' or 'opening'")


def _unique(values: Iterable[str], location: str) -> set[str]:
    items = list(values)
    if len(items) != len(set(items)):
        raise ValueError(f"duplicate identifiers or references in {location}")
    return set(items)


def _require_refs(values: Iterable[str], targets: set[str], location: str) -> None:
    references = list(values)
    _unique(references, location)
    missing = sorted(set(references) - targets)
    if missing:
        raise ValueError(f"unresolved references in {location}: {', '.join(missing)}")


def _check_point(value: float | None, duration: float, location: str) -> None:
    if value is not None and not 0 <= value <= duration:
        raise ValueError(f"time outside source duration in {location}: {value}")


def _check_interval(start: float, end: float, duration: float, location: str) -> None:
    _check_point(start, duration, f"{location}.开始秒")
    _check_point(end, duration, f"{location}.结束秒")


def _check_shot_times(shots: Sequence[ShotBase], duration: float) -> None:
    for shot in shots:
        location = f"镜头 {shot.shot_id}"
        _check_interval(shot.start, shot.end, duration, location)
        for action in shot.action_process.actions:
            _check_interval(
                action.start,
                action.end,
                duration,
                f"{location}.动作 {action.action_id}",
            )
            if action.start < shot.start or action.end > shot.end:
                raise ValueError(f"action {action.action_id} is outside shot {shot.shot_id}")
        for index, movement in enumerate(shot.presentation.camera_movements):
            _check_interval(
                movement.start, movement.end, duration, f"{location}.相机运动[{index}]"
            )
            if movement.start < shot.start or movement.end > shot.end:
                raise ValueError(f"camera movement is outside shot {shot.shot_id}")
        for index, frame in enumerate(shot.key_frames):
            _check_point(frame.source_time, duration, f"{location}.关键帧[{index}]")
            if not shot.start <= frame.source_time <= shot.end:
                raise ValueError(f"key frame is outside shot {shot.shot_id}")
            if frame.media_reference is not None:
                _check_point(
                    frame.media_reference.source_time,
                    duration,
                    f"{location}.关键帧[{index}].媒体引用",
                )
                if (
                    abs(frame.media_reference.source_time - frame.source_time)
                    > _MEDIA_REFERENCE_TOLERANCE
                ):
                    raise ValueError(f"media reference is too far from key frame in {shot.shot_id}")
        for index, clip in enumerate(shot.dynamic_evidence):
            _check_interval(
                clip.start, clip.end, duration, f"{location}.动态证据片段[{index}]"
            )
            if clip.start < shot.start or clip.end > shot.end:
                raise ValueError(f"dynamic evidence is outside shot {shot.shot_id}")
        transition = shot.transition
        if transition.start is not None and transition.end is not None:
            _check_interval(
                transition.start, transition.end, duration, f"{location}.与前镜衔接"
            )


def _check_audiovisual_times(timeline: AudiovisualTimeline, duration: float) -> None:
    for speech in timeline.speeches:
        _check_interval(speech.start, speech.end, duration, f"口播 {speech.record_id}")
        for index, phrase in enumerate(speech.key_phrases):
            if phrase.start is not None and phrase.end is not None:
                _check_interval(
                    phrase.start,
                    phrase.end,
                    duration,
                    f"口播 {speech.record_id}.关键字句[{index}]",
                )
                if phrase.start < speech.start or phrase.end > speech.end:
                    raise ValueError(f"key phrase is outside speech {speech.record_id}")
    for sound in timeline.sounds:
        _check_interval(sound.start, sound.end, duration, f"声音 {sound.record_id}")
    for overlay in timeline.overlays:
        _check_interval(overlay.start, overlay.end, duration, f"包装 {overlay.record_id}")
        for index, change in enumerate(overlay.changes):
            _check_interval(
                change.start, change.end, duration, f"包装 {overlay.record_id}.变化[{index}]"
            )
            if change.start < overlay.start or change.end > overlay.end:
                raise ValueError(f"overlay change is outside overlay {overlay.record_id}")
    for index, sync in enumerate(timeline.sync_points):
        _check_point(sync.source_time, duration, f"视听同步点[{index}]")


def _check_observation_times(result: FullVideoResult | OpeningResult, duration: float) -> None:
    for index, observed in enumerate(result.observed_ranges):
        _check_interval(observed.start, observed.end, duration, f"已观察范围[{index}]")


def _id_sets(
    result: FullVideoResult | OpeningResult,
) -> tuple[set[str], set[str], set[str], set[str], set[str], set[str]]:
    entities = result.entities
    character_ids = _unique((item.character_id for item in entities.characters), "人物")
    scene_ids = _unique((item.scene_id for item in entities.scenes), "场景")
    object_ids = _unique((item.object_id for item in entities.objects), "产品与道具")
    entity_ids = character_ids | scene_ids | object_ids
    if len(entity_ids) != len(character_ids) + len(scene_ids) + len(object_ids):
        raise ValueError("entity identifiers must be unique across entity types")

    shot_ids = _unique((shot.shot_id for shot in result.shots), "镜头")
    action_ids = _unique(
        (action.action_id for shot in result.shots for action in shot.action_process.actions),
        "动作",
    )
    speech_ids = _unique(
        (speech.record_id for speech in result.audiovisual_timeline.speeches), "口播"
    )
    sound_ids = _unique(
        (sound.record_id for sound in result.audiovisual_timeline.sounds), "声音"
    )
    overlay_ids = _unique(
        (overlay.record_id for overlay in result.audiovisual_timeline.overlays), "文字与包装"
    )
    audiovisual_ids = speech_ids | sound_ids | overlay_ids
    total_ids = entity_ids | shot_ids | action_ids | audiovisual_ids
    expected_count = (
        len(entity_ids)
        + len(shot_ids)
        + len(action_ids)
        + len(speech_ids)
        + len(sound_ids)
        + len(overlay_ids)
    )

    if isinstance(result, FullVideoResult):
        stage_ids = _unique(
            (stage.stage_id for stage in result.creative_structure.stages), "内容阶段"
        )
        method_ids = _unique(
            (method.method_id for method in result.expression_methods), "关键表达方法"
        )
        total_ids |= stage_ids | method_ids
        expected_count += len(stage_ids) + len(method_ids)
    else:
        stage_ids = set()

    if len(total_ids) != expected_count:
        raise ValueError("identifiers must be globally unique within a result")
    return character_ids, scene_ids, object_ids, shot_ids, action_ids, audiovisual_ids


def _check_entity_refs(entities: Entities, entity_ids: set[str]) -> None:
    for index, relationship in enumerate(entities.relationships):
        _require_refs(relationship.entity_ids, entity_ids, f"关键关系[{index}]")


def _check_replication_refs(
    replication: ReplicationBasis,
    entity_ids: set[str],
    shot_ids: set[str],
    audiovisual_ids: set[str],
) -> None:
    for index, suggestion in enumerate(replication.preservation_suggestions):
        _require_refs(suggestion.shot_ids, shot_ids, f"表现保留建议[{index}].镜头")
        _require_refs(
            suggestion.audiovisual_record_ids,
            audiovisual_ids,
            f"表现保留建议[{index}].视听",
        )
    for index, constraint in enumerate(replication.replacement_constraints):
        _require_refs([constraint.entity_id], entity_ids, f"元素替换约束[{index}]")
    for index, dependency in enumerate(replication.material_dependencies):
        _require_refs(dependency.shot_ids, shot_ids, f"素材依赖[{index}].镜头")
        _require_refs(
            dependency.audiovisual_record_ids,
            audiovisual_ids,
            f"素材依赖[{index}].视听",
        )


def _check_references(result: FullVideoResult | OpeningResult) -> None:
    (
        character_ids,
        scene_ids,
        object_ids,
        shot_ids,
        action_ids,
        audiovisual_ids,
    ) = _id_sets(result)
    entity_ids = character_ids | scene_ids | object_ids
    _check_entity_refs(result.entities, entity_ids)

    for index, shot in enumerate(result.shots):
        _require_refs(shot.character_ids, character_ids, f"镜头 {shot.shot_id}.人物")
        if shot.scene_id is not None:
            _require_refs([shot.scene_id], scene_ids, f"镜头 {shot.shot_id}.场景")
        _require_refs(shot.object_ids, object_ids, f"镜头 {shot.shot_id}.对象")
        expected_previous = None if index == 0 else result.shots[index - 1].shot_id
        if shot.transition.previous_shot_id != expected_previous:
            raise ValueError(f"previous shot reference is invalid for shot {shot.shot_id}")
        for state in [
            *shot.action_process.initial_states,
            *shot.action_process.final_states,
        ]:
            _require_refs([state.entity_id], entity_ids, f"镜头 {shot.shot_id}.实体状态")
            _require_refs(
                state.held_or_contacted_entity_ids,
                entity_ids,
                f"镜头 {shot.shot_id}.接触实体",
            )
        for action in shot.action_process.actions:
            _require_refs(
                action.actor_entity_ids, entity_ids, f"动作 {action.action_id}.执行实体"
            )
            _require_refs(
                action.target_entity_ids, entity_ids, f"动作 {action.action_id}.作用对象"
            )

    timeline = result.audiovisual_timeline
    speech_ids = {speech.record_id for speech in timeline.speeches}
    for speech in timeline.speeches:
        if speech.speaker_character_id is not None:
            _require_refs(
                [speech.speaker_character_id], character_ids, f"口播 {speech.record_id}.说话人物"
            )
    for sound in timeline.sounds:
        _require_refs(sound.shot_ids, shot_ids, f"声音 {sound.record_id}.镜头")
        _require_refs(sound.speech_ids, speech_ids, f"声音 {sound.record_id}.口播")
    for overlay in timeline.overlays:
        _require_refs(overlay.shot_ids, shot_ids, f"包装 {overlay.record_id}.镜头")
        _require_refs(overlay.speech_ids, speech_ids, f"包装 {overlay.record_id}.口播")
    for index, sync in enumerate(timeline.sync_points):
        _require_refs(sync.shot_ids, shot_ids, f"视听同步点[{index}].镜头")
        _require_refs(sync.action_ids, action_ids, f"视听同步点[{index}].动作")
        _require_refs(
            sync.audiovisual_record_ids, audiovisual_ids, f"视听同步点[{index}].视听"
        )

    _check_replication_refs(
        result.replication_basis, entity_ids, shot_ids, audiovisual_ids
    )

    if isinstance(result, FullVideoResult):
        stage_ids = {stage.stage_id for stage in result.creative_structure.stages}
        for shot in result.shots:
            _require_refs([shot.stage_id], stage_ids, f"镜头 {shot.shot_id}.所属阶段")
        for method in result.expression_methods:
            _require_refs(method.shot_ids, shot_ids, f"方法 {method.method_id}.镜头")
            _require_refs(
                method.audiovisual_record_ids,
                audiovisual_ids,
                f"方法 {method.method_id}.视听",
            )
    else:
        attraction = result.attraction_mechanism
        _require_refs(attraction.shot_ids, shot_ids, "吸引机制.镜头")
        _require_refs(
            attraction.audiovisual_record_ids, audiovisual_ids, "吸引机制.视听"
        )
        end_state = result.follow_up.opening_end_state
        if end_state is not None:
            _require_refs(end_state.continuing_action_ids, action_ids, "开场结束状态.动作")
            _require_refs(
                end_state.continuing_audiovisual_record_ids,
                audiovisual_ids,
                "开场结束状态.视听",
            )


def _check_contiguous(shots: Sequence[ShotBase], duration: float) -> None:
    if abs(shots[0].start) > _TIME_TOLERANCE:
        raise ValueError("full shot coverage must start at 0")
    for previous, current in zip(shots, shots[1:], strict=False):
        difference = current.start - previous.end
        if abs(difference) > _TIME_TOLERANCE:
            kind = "gap" if difference > 0 else "overlap"
            raise ValueError(f"full shot coverage has a {kind} between shots")
    if abs(shots[-1].end - duration) > _TIME_TOLERANCE:
        raise ValueError("full shot coverage must end at source duration")


def _check_opening(result: OpeningResult, duration: float) -> None:
    opening_range = result.opening_range
    actual = opening_range.actual_first_three_seconds
    expected_end = min(3.0, duration)
    if abs(actual.start) > _TIME_TOLERANCE or abs(actual.end - expected_end) > _TIME_TOLERANCE:
        raise ValueError("实际前三秒 must be 0..min(3, source duration)")

    complete = opening_range.complete_opening
    if abs(complete.start) > _TIME_TOLERANCE:
        raise ValueError("完整开场 must start at 0")
    _check_point(complete.end, duration, "完整开场.结束秒")
    if complete.end is None:
        if result.follow_up.check_status != "未核对":
            raise ValueError("unknown complete opening end requires 未核对 follow-up status")
        if result.follow_up.opening_end_state is not None:
            raise ValueError("unknown complete opening end requires null 开场结束状态")
        if not result.uncertainties:
            raise ValueError("unknown complete opening end must be recorded in 不确定项")
    else:
        end_state = result.follow_up.opening_end_state
        if end_state is None:
            raise ValueError("known complete opening end requires 开场结束状态")
        if abs(end_state.source_time - complete.end) > _TIME_TOLERANCE:
            raise ValueError("开场结束状态 time must match complete opening end")

    coverage_end = max(expected_end, complete.end or 0.0)
    if abs(result.shots[0].start) > _TIME_TOLERANCE:
        raise ValueError("opening shot coverage must start at 0")
    for previous, current in zip(result.shots, result.shots[1:], strict=False):
        difference = current.start - previous.end
        if abs(difference) > _TIME_TOLERANCE:
            kind = "gap" if difference > 0 else "overlap"
            raise ValueError(f"opening shot coverage has a {kind}")
    if result.shots[-1].end < coverage_end - _TIME_TOLERANCE:
        raise ValueError("opening shots do not cover the required opening range")

    _check_point(
        result.follow_up.opening_end_state.source_time
        if result.follow_up.opening_end_state is not None
        else None,
        duration,
        "开场结束状态",
    )
    for index, evidence in enumerate(result.follow_up.evidence):
        _check_interval(evidence.start, evidence.end, duration, f"后文证据[{index}]")
        for frame_index, frame in enumerate(evidence.key_frames):
            _check_point(
                frame.source_time, duration, f"后文证据[{index}].关键帧[{frame_index}]"
            )
            if not evidence.start <= frame.source_time <= evidence.end:
                raise ValueError("follow-up key frame is outside its evidence segment")
            if frame.media_reference is not None:
                _check_point(
                    frame.media_reference.source_time,
                    duration,
                    f"后文证据[{index}].关键帧[{frame_index}].媒体引用",
                )
                if (
                    abs(frame.media_reference.source_time - frame.source_time)
                    > _MEDIA_REFERENCE_TOLERANCE
                ):
                    raise ValueError("follow-up media reference is too far from key frame")


def validate_result(payload: dict[str, Any], mode: str, duration: float) -> BaseModel:
    """Parse and validate a result against source time and reference invariants."""

    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("duration must be a positive finite number")
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration must be a positive finite number")

    model_type = result_type(mode)
    result = model_type.model_validate(payload)
    if not isinstance(result, (FullVideoResult, OpeningResult)):
        raise TypeError("unsupported result model")

    _check_observation_times(result, duration)
    _check_shot_times(result.shots, duration)
    _check_audiovisual_times(result.audiovisual_timeline, duration)
    _check_references(result)

    if isinstance(result, FullVideoResult):
        for index, stage in enumerate(result.creative_structure.stages):
            _check_interval(stage.start, stage.end, duration, f"内容阶段[{index}]")
        _check_contiguous(result.shots, duration)
    else:
        _check_opening(result, duration)
    return result
