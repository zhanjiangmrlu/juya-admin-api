from collections.abc import Mapping

from juya_admin_api.integrations.content_security.policy import security_status_usable
from juya_admin_api.modules.content.domain import PublishCheck
from juya_admin_api.modules.content.schemas import SceneContent


def check_content(
    content: SceneContent,
    assets: Mapping[str, Mapping[str, object]],
    audio_versions: Mapping[str, Mapping[str, object]],
    *,
    require_review: bool = True,
) -> list[PublishCheck]:
    def asset_ready(asset_id: str | None, kind: str) -> bool:
        asset = assets.get(asset_id or "", {})
        return (
            asset.get("status") == "CONFIRMED"
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
    values = {
        "TITLE_REQUIRED": bool(content.title_en.strip() and content.title_zh.strip()),
        "ORIGINAL_IMAGE_REQUIRED": asset_ready(content.original_image_asset_id, "images"),
        "DIALOGUE_REQUIRED": bool(
            content.dialogue
            and all(
                row.english.strip() and row.chinese.strip() and row.speaker.strip()
                for row in content.dialogue
            )
        ),
        "VOCABULARY_REQUIRED": bool(
            content.vocabulary
            and all(
                item.english.strip() and item.chinese.strip() and item.entry_id
                for item in content.vocabulary
            )
        ),
        "CHUNKS_REQUIRED": bool(
            content.chunks
            and all(
                item.english.strip() and item.chinese.strip() and item.entry_id
                for item in content.chunks
            )
        ),
        "COPYRIGHT_SOURCE_REQUIRED": bool(content.copyright.strip() and content.source.strip()),
        "AUDIO_MISSING": whole_ready,
        "SENTENCE_TIMING_INVALID": timings,
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
    return [PublishCheck(code, "ERROR", passed) for code, passed in values.items()]
