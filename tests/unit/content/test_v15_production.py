from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.audio_pipeline.slicing import Boundary, FFmpegSlicer, dependency_hash
from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent, normalize_audio_change


def test_v15_dialogue_does_not_require_optional_images_words_chunks() -> None:
    content = SceneContent.model_validate(
        {
            "schema_version": 2,
            "title_en": "Hello",
            "title_zh": "你好",
            "copyright": "permission",
            "source": "author",
            "audio": {"target_id": "t", "version_id": "v", "asset_id": "a", "duration_ms": 1000},
            "dialogue": [
                {
                    "id": "s",
                    "speaker": "A",
                    "english": "Hello",
                    "chinese": "你好",
                    "start_ms": 0,
                    "end_ms": 900,
                    "audio_version_id": "v",
                    "timing_confirmed": True,
                    "slice_target_id": "st",
                    "slice_version": "sv",
                    "slice_dependency_hash": dependency_hash(
                        sentence_id="s",
                        english="Hello",
                        source_sha256="source-sha",
                        source_audio_version="v",
                        boundary=Boundary(0, 900),
                        encoding_config=FFmpegSlicer.encoding_config,
                    ),
                    "boundary_verified_by": "reviewer",
                }
            ],
        }
    )
    assets = {
        "a": dict(
            status="CONFIRMED",
            object_key="sealed/media/audio/a",
            security_status="PASSED",
            asset_type="audio",
            duration_ms=1000,
            sha256="source-sha",
        ),
        "sa": dict(
            status="CONFIRMED",
            object_key="sealed/media/audio/sa",
            security_status="PASSED",
            asset_type="audio",
            duration_ms=900,
        ),
    }
    audio = {
        "v": dict(target_id="t", target_type="WHOLE", asset_id="a", status="CONFIRMED"),
        "sv": dict(
            target_id="st",
            asset_id="sa",
            status="CONFIRMED",
            slice_confirmed=True,
            dependency_hash=content.dialogue[0].slice_dependency_hash,
            source_sha256="source-sha",
            source_audio_version="v",
            sentence_id="s",
            start_ms=0,
            end_ms=900,
        ),
    }
    checks = check_content(content, assets, audio)
    assert all(c.passed for c in checks)
    audio["v"]["target_type"] = "WORD"
    assert not next(
        c for c in check_content(content, assets, audio) if c.code == "AUDIO_MISSING"
    ).passed
    audio["v"]["target_type"] = "WHOLE"
    changed = content.model_copy(deep=True)
    changed.dialogue[0].english = "Changed English"
    assert not next(
        c for c in check_content(changed, assets, audio) if c.code == "SENTENCE_SLICE_INVALID"
    ).passed
    audio["sv"]["slice_confirmed"] = False
    assert not next(
        c for c in check_content(content, assets, audio) if c.code == "SENTENCE_SLICE_INVALID"
    ).passed


def test_v15_same_english_different_entries_and_chunk_no_image() -> None:
    data = {
        "schema_version": 2,
        "vocabulary": [
            {"entry_id": "cup", "english": "medium", "chinese": "中杯"},
            {"entry_id": "size", "english": "medium", "chinese": "中等的"},
        ],
    }
    assert len(SceneContent.model_validate(data).vocabulary) == 2
    with pytest.raises(ValueError):
        SceneContent.model_validate(
            {"schema_version": 2, "chunks": [{"english": "good morning", "icon_asset_id": "image"}]}
        )


def test_v15_text_or_boundary_change_invalidates_slice_even_same_audio() -> None:
    previous = SceneContent.model_validate(
        {
            "schema_version": 2,
            "audio": {"target_id": "t", "version_id": "v", "asset_id": "a", "duration_ms": 1000},
            "dialogue": [
                {
                    "id": "s",
                    "english": "Hello",
                    "start_ms": 0,
                    "end_ms": 900,
                    "audio_version_id": "v",
                    "timing_confirmed": True,
                    "slice_target_id": "st",
                    "slice_version": "sv",
                }
            ],
        }
    )
    proposed = previous.model_copy(deep=True)
    proposed.dialogue[0].english = "Hello again"
    updated = normalize_audio_change(previous, proposed)
    assert not updated.dialogue[0].timing_confirmed
    assert updated.dialogue[0].slice_target_id is None


def test_sample_formula_and_hash_binding() -> None:
    from juya_admin_api.modules.content.v15_proofreading import collection_hash, sample_count

    assert [sample_count(n) for n in (0, 1, 20, 100, 1000)] == [0, 1, 1, 5, 20]
    assert collection_hash({"r": "h"}) != collection_hash({"r": "changed"})


@pytest.mark.asyncio
async def test_module_capability_is_preparation_check_instead_of_type_whitelist() -> None:
    from juya_admin_api.modules.content.domain import Scene
    from juya_admin_api.modules.content.repository import InMemoryContentRepository
    from juya_admin_api.modules.content.service import ContentService

    repo = InMemoryContentRepository()
    for i in range(3):
        repo.scenes[str(i)] = Scene(str(i), "series", status="PUBLISHED", published_revision_id="r")
    service = ContentService(
        repo,
        module_capabilities={
            "scene_learning": True,
            "short_reading": True,
            "long_sentence": False,
            "grammar": False,
        },
    )
    result = await service.save_discovery_config(
        open_scene_ids=("0", "1", "2"),
        preview_by_series={},
        learning_modules={"scene_learning": False, "short_reading": True},
        expected_version=0,
        actor_id="a",
        now=datetime.now(UTC),
    )
    assert result.learning_modules["short_reading"]
