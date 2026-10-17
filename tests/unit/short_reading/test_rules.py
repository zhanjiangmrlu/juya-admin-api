import pytest
from pydantic import ValidationError

from juya_admin_api.modules.short_reading.schemas import ReadingContent, publication_errors


def test_incomplete_draft_is_valid_but_cannot_publish():
    draft = ReadingContent()
    assert "TITLE_EN_REQUIRED" in publication_errors(draft)
    assert "PARAGRAPHS_REQUIRED" in publication_errors(draft)


def test_pure_reading_rejects_scene_and_word_data():
    for field in ("words", "chunks", "dialogue", "candidates", "tts"):
        with pytest.raises(ValidationError):
            ReadingContent.model_validate({field: []})


def test_complete_text_needs_copyright_and_audio_fixed_confirmation():
    content = ReadingContent(
        title_en="Read",
        title_zh="阅读",
        category="a",
        topic="b",
        level="A1",
        safe_intro="safe",
        paragraphs_en=["One"],
        translation_zh="一",
    )
    assert "COPYRIGHT_REQUIRED" in publication_errors(content)
