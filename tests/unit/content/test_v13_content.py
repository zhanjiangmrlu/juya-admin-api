from datetime import UTC, datetime

import pytest

from juya_admin_api.modules.content.domain import Scene, SceneRevision
from juya_admin_api.modules.content.repository import InMemoryContentRepository
from juya_admin_api.modules.content.service import ContentService
from juya_admin_api.shared.errors import AppError


@pytest.mark.parametrize("require_review", [False, True])
def test_skipped_assets_follow_review_switch_without_skipping_file_facts(
    require_review: bool,
) -> None:
    from juya_admin_api.modules.content.production_rules import check_content
    from juya_admin_api.modules.content.schemas import SceneContent

    content = SceneContent(original_image_asset_id="image")
    assets = {
        "image": {
            "status": "CONFIRMED",
            "security_status": "SKIPPED",
            "asset_type": "images",
            "object_key": "sealed/media/images/fixture.png",
            "width": 32,
            "height": 24,
        }
    }
    checks = check_content(content, assets, {}, require_review=require_review)
    assert next(c for c in checks if c.code == "ORIGINAL_IMAGE_REQUIRED").passed is (
        not require_review
    )
    assets["image"]["width"] = 0
    checks = check_content(content, assets, {}, require_review=require_review)
    assert not next(c for c in checks if c.code == "ORIGINAL_IMAGE_REQUIRED").passed


def test_learning_original_cannot_be_reused_as_public_preview_cover() -> None:
    from juya_admin_api.modules.content.production_rules import check_content
    from juya_admin_api.modules.content.schemas import SceneContent

    content = SceneContent(original_image_asset_id="original", cover_asset_id="original")
    assets = {
        "original": {
            "status": "CONFIRMED",
            "security_status": "PASSED",
            "asset_type": "images",
            "width": 100,
            "height": 100,
        }
    }
    cover_check = next(
        check for check in check_content(content, assets, {}) if check.code == "COVER_INVALID"
    )
    assert not cover_check.passed


@pytest.mark.asyncio
async def test_empty_check_cache_cannot_publish_empty_draft() -> None:
    repo = InMemoryContentRepository()
    repo.scenes["scene"] = Scene("scene", "series")
    repo.revisions["draft"] = SceneRevision("draft", "scene", None)
    with pytest.raises(AppError, match="发布"):
        await ContentService(repo).publish_revision("draft", "admin", "key", datetime.now(UTC))
    assert repo.scenes["scene"].published_revision_id is None


def test_phrase_matching_wins_and_unlisted_inflections_stay_plain() -> None:
    from juya_admin_api.modules.content.schemas import SceneContent
    from juya_admin_api.modules.content.text_spans import build_clickable_spans

    content = SceneContent.model_validate(
        {
            "dialogue": [{"id": "s1", "english": "Put together the boxes and packed it."}],
            "vocabulary": [
                {"entry_id": "word", "english": "put", "chinese": "放"},
                {"entry_id": "pack", "english": "pack", "chinese": "打包"},
            ],
            "chunks": [{"entry_id": "phrase", "english": "put together", "chinese": "组装"}],
        }
    )
    result = build_clickable_spans(content)
    spans = result.dialogue[0].clickable_spans
    assert [(span.entry_id, span.start, span.end) for span in spans] == [("phrase", 0, 12)]


def test_replacing_whole_audio_invalidates_existing_timings() -> None:
    from juya_admin_api.modules.content.schemas import SceneContent, normalize_audio_change

    old = SceneContent.model_validate(
        {
            "audio": {"target_id": "t", "version_id": "a1", "asset_id": "f1", "duration_ms": 3000},
            "dialogue": [
                {
                    "id": "s1",
                    "english": "Hello",
                    "start_ms": 100,
                    "end_ms": 1500,
                    "audio_version_id": "a1",
                    "timing_confirmed": True,
                }
            ],
        }
    )
    new = old.model_copy(deep=True)
    new.audio = new.audio.model_copy(update={"version_id": "a2", "asset_id": "f2"})
    result = normalize_audio_change(old, new)
    assert result.dialogue[0].start_ms is None
    assert result.dialogue[0].end_ms is None
    assert not result.dialogue[0].timing_confirmed


def test_reordering_sentences_keeps_source_locators_stable() -> None:
    from juya_admin_api.modules.content.schemas import SceneContent
    from juya_admin_api.modules.content.text_spans import build_clickable_spans

    source = SceneContent.model_validate(
        {
            "dialogue": [{"id": "a", "english": "hello"}, {"id": "b", "english": "hello"}],
            "vocabulary": [{"entry_id": "w", "english": "hello", "chinese": "你好"}],
        }
    )
    before = build_clickable_spans(source)
    source.dialogue.reverse()
    after = build_clickable_spans(source)
    assert {row.id: row.clickable_spans[0].source_locator for row in before.dialogue} == {
        row.id: row.clickable_spans[0].source_locator for row in after.dialogue
    }
