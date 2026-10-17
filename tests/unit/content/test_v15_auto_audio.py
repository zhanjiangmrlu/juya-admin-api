from copy import deepcopy

from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_auto_audio import audio_input_hash, needs_alignment


def content():
    return SceneContent.model_validate(
        {
            "schema_version": 2,
            "template_type": "dialogue",
            "title_en": "Hello",
            "title_zh": "你好",
            "audio": {
                "target_id": "whole",
                "version_id": "source",
                "asset_id": "asset",
                "duration_ms": 3000,
            },
            "dialogue": [{"id": "a", "speaker": "A", "english": "Hello there.", "chinese": "你好"}],
        }
    )


def test_automatic_input_changes_only_with_audio_or_ordered_original_sentences():
    original = content()
    edited = deepcopy(original)
    edited.title_en = "Other title"
    edited.dialogue[0].chinese = "别的翻译"
    assert audio_input_hash(original) == audio_input_hash(edited)
    edited.dialogue[0].english = "Different words"
    assert audio_input_hash(original) != audio_input_hash(edited)
    edited = deepcopy(original)
    edited.audio.version_id = "replacement"
    assert audio_input_hash(original) != audio_input_hash(edited)


def test_complete_verified_boundaries_do_not_start_asr_again():
    original = content()
    assert needs_alignment(original)
    row = original.dialogue[0]
    row.start_ms, row.end_ms, row.audio_version_id = 10, 2900, "source"
    row.timing_confirmed, row.boundary_verified_by = True, "admin"
    assert not needs_alignment(original)
    row.audio_version_id = "stale"
    assert needs_alignment(original)
