"""Pure V1.5 draft identity boundaries; SQL identity allocation is covered separately."""

import pytest
from pydantic import ValidationError

from juya_admin_api.modules.content.production_rules import check_content
from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content.v15_entries import details


def test_pending_same_spelling_is_not_an_identity_and_distinct_sources_are_retained() -> None:
    content = SceneContent(
        schema_version=2,
        template_type="word_card",
        vocabulary=[
            SceneEntry(english="medium", chinese="中杯", source_sentence="A medium coffee."),
            SceneEntry(english="medium", chinese="中等", source_sentence="Medium difficulty."),
        ],
    )
    assert len(content.vocabulary) == 2
    assert content.vocabulary[0].entry_id == content.vocabulary[1].entry_id == ""
    assert content.vocabulary[0].source_sentence != content.vocabulary[1].source_sentence


def test_duplicate_supplied_stable_identity_is_rejected_without_last_write_wins() -> None:
    with pytest.raises(ValidationError):
        SceneContent(
            schema_version=2,
            vocabulary=[
                SceneEntry(entry_id="same", english="medium", chinese="中杯"),
                SceneEntry(entry_id="same", english="medium", chinese="中等"),
            ],
        )


def test_draft_control_confirmation_is_excluded_from_immutable_details_hash() -> None:
    entry = SceneEntry(
        entry_id="entry",
        sense_id="sense",
        source_id="source",
        english="medium",
        chinese="中杯",
        source_sentence="A medium coffee.",
    )
    before = details(entry)
    entry.confirmed = True
    entry.meaning_change = "wording"
    assert details(entry) == before
    assert "confirmed" not in before and "meaning_change" not in before
    entry.chinese = "中等"
    assert details(entry) != before
    assert details(entry)["sense_id"] == "sense"  # SQL allocation, not a client-side guess.


def test_modern_word_card_is_complete_without_dialogue_or_whole_audio() -> None:
    content = SceneContent(
        schema_version=2,
        template_type="word_card",
        title_en="Words",
        title_zh="词卡",
        copyright="Owned",
        source="Owner",
        vocabulary=[SceneEntry(entry_id="entry", english="medium", chinese="中杯")],
    )
    checks = check_content(content, {}, {})
    assert all(check.passed for check in checks)
