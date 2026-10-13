from collections.abc import Mapping

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.domain import PublishCheck
from juya_admin_api.modules.content.schemas import SceneContent


def check_content(
    content: SceneContent,
    assets: Mapping[str, Mapping[str, object]],
    audio_versions: Mapping[str, Mapping[str, object]],
    *,
    require_review: bool = True,
) -> list[PublishCheck]:
    # 功能:检查场景必填内容、素材、音频时间轴和来源引用是否可发布。
    # 参数:
    #     content: 结构化场景内容,含标题、对话、词汇、语块和媒体引用。
    #     assets: 以素材标识索引的素材状态、对象键、尺寸和时长事实。
    #     audio_versions: 以音频版本标识索引的目标归属、素材引用和确认状态。
    #     require_review: 是否要求素材通过内容审核;关闭时仍保留素材完整性检查。
    # 返回:全部发布检查项,涵盖必填内容、媒体资源、时间轴和来源引用。
    def asset_ready(asset_id: str | None, kind: str) -> bool:
        # 功能:检查素材类型、固定对象键和确认审核状态是否满足发布条件。
        # 参数:
        #     asset_id: 素材公开标识,关联已登记的图片或音频。
        #     kind: 期望的素材类型,图片为 images,音频为 audio。
        # 返回:素材是否满足类型、固定对象、审核及图片尺寸要求。
        asset = assets.get(asset_id or "", {})
        return (
            asset.get("status") == "CONFIRMED"
            and str(asset.get("object_key", "")).startswith("sealed/media/")
            and security_status_usable(asset.get("security_status"), require_review=require_review)
            and asset.get("asset_type") == kind
            and (kind != "images" or bool(asset.get("width") and asset.get("height")))
        )

    audio = content.audio
    audio_fact = audio_versions.get(audio.version_id, {}) if audio else {}
    asset = assets.get(audio.asset_id, {}) if audio else {}
    whole_ready = bool(
        audio
        and asset_ready(audio.asset_id, "audio")
        and audio_fact.get("target_id") == audio.target_id
        and audio_fact.get("asset_id") == audio.asset_id
        and (
            content.schema_version != 2
            or audio_fact.get("target_type")
            in {
                "WHOLE",
                "FULL",
                "SCENE",
                "scene",
                "whole",
                "dialogue",
                "DIALOGUE",
                "SHORT_READING",
                "short_reading",
            }
        )
        and audio_fact.get("status") in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}
        and asset.get("duration_ms") == audio.duration_ms
        and audio.duration_ms > 0
    )
    timings = bool(
        whole_ready
        and content.dialogue
        and audio
        and all(
            row.start_ms is not None
            and row.end_ms is not None
            and 0 <= row.start_ms < row.end_ms <= audio.duration_ms
            and row.audio_version_id == audio.version_id
            and row.timing_confirmed
            for row in content.dialogue
        )
        and all(
            previous.end_ms is not None
            and following.start_ms is not None
            and previous.end_ms <= following.start_ms
            for previous, following in zip(content.dialogue, content.dialogue[1:], strict=False)
        )
    )
    is_dialogue = content.template_type == "dialogue"
    modern = content.schema_version == 2
    slices = all(
        audio is not None
        and row.start_ms is not None
        and row.end_ms is not None
        and 0 <= row.start_ms < row.end_ms <= audio.duration_ms
        and row.slice_target_id
        and row.slice_version
        and row.slice_dependency_hash
        and audio_versions.get(row.slice_version, {}).get("target_id") == row.slice_target_id
        and audio_versions.get(row.slice_version, {}).get("slice_confirmed") is True
        and audio_versions.get(row.slice_version, {}).get("status")
        in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}
        and audio_versions.get(row.slice_version, {}).get("dependency_hash")
        == row.slice_dependency_hash
        and audio_versions.get(row.slice_version, {}).get("source_audio_version")
        == (audio.version_id if audio else None)
        and audio_versions.get(row.slice_version, {}).get("sentence_id") == row.id
        and audio_versions.get(row.slice_version, {}).get("start_ms") == row.start_ms
        and audio_versions.get(row.slice_version, {}).get("end_ms") == row.end_ms
        and audio_versions.get(row.slice_version, {}).get("source_sha256") == asset.get("sha256")
        and bool(row.boundary_verified_by)
        and row.slice_dependency_hash
        == dependency_hash(
            sentence_id=row.id,
            english=row.english,
            source_sha256=str(asset.get("sha256", "")),
            source_audio_version=audio.version_id,
            boundary=Boundary(row.start_ms, row.end_ms),
            encoding_config=FFmpegSlicer.encoding_config,
        )
        and asset_ready(str(audio_versions.get(row.slice_version, {}).get("asset_id", "")), "audio")
        for row in content.dialogue
    )
    entries = [*content.vocabulary, *content.chunks]
    optional_audio = all(
        (entry.audio_target_id is None and entry.audio_version_id is None)
        or (
            entry.audio_target_id is not None
            and entry.audio_version_id is not None
            and audio_versions.get(entry.audio_version_id, {}).get("target_id")
            == entry.audio_target_id
            and audio_versions.get(entry.audio_version_id, {}).get("status")
            in {"CONFIRMED", "ACTIVE", "SUPERSEDED"}
            and asset_ready(
                str(audio_versions.get(entry.audio_version_id, {}).get("asset_id")), "audio"
            )
        )
        for entry in entries
    )
    sentence_ids = {row.id for row in content.dialogue}
    sources = all(set(entry.source_sentence_ids) <= sentence_ids for entry in entries)
    if modern and is_dialogue:
        sentences = {row.id: row.english for row in content.dialogue}
        sources = sources and all(
            entry.source_sentence_ids
            and entry.source_sentence
            and all(
                entry.english in sentences.get(identity, "")
                for identity in entry.source_sentence_ids
            )
            and entry.source_sentence
            in [sentences[identity] for identity in entry.source_sentence_ids]
            for entry in entries
        )
    values = {
        "TITLE_REQUIRED": bool(content.title_en.strip() and content.title_zh.strip()),
        "ORIGINAL_IMAGE_REQUIRED": (modern and content.original_image_asset_id is None)
        or asset_ready(content.original_image_asset_id, "images"),
        "DIALOGUE_REQUIRED": (modern and not is_dialogue)
        or bool(
            content.dialogue
            and all(
                row.english.strip() and row.chinese.strip() and row.speaker.strip()
                for row in content.dialogue
            )
        ),
        "VOCABULARY_REQUIRED": bool(
            (content.vocabulary or (modern and is_dialogue))
            and all(
                item.english.strip() and item.chinese.strip() and item.entry_id
                for item in content.vocabulary
            )
        ),
        "CHUNKS_REQUIRED": bool(
            (content.chunks or modern)
            and all(
                item.english.strip() and item.chinese.strip() and item.entry_id
                for item in content.chunks
            )
        ),
        "COPYRIGHT_SOURCE_REQUIRED": bool(content.copyright.strip() and content.source.strip()),
        "AUDIO_MISSING": (modern and not is_dialogue and audio is None) or whole_ready,
        "SENTENCE_TIMING_INVALID": (modern and not is_dialogue) or timings,
        "ENTRY_AUDIO_INVALID": optional_audio,
        "SOURCE_REFERENCE_INVALID": sources,
        "COVER_INVALID": content.cover_asset_id is None
        or (
            content.cover_asset_id != content.original_image_asset_id
            and asset_ready(content.cover_asset_id, "images")
        ),
        "ICONS_INVALID": all(
            entry.icon_asset_id is None or asset_ready(entry.icon_asset_id, "images")
            for entry in entries
        ),
    }
    if modern:
        values["SENTENCE_SLICE_INVALID"] = (not is_dialogue) or bool(content.dialogue and slices)
        values["CHUNK_IMAGE_INVALID"] = all(entry.icon_asset_id is None for entry in content.chunks)
    return [PublishCheck(code, "ERROR", passed) for code, passed in values.items()]
