import importlib.util

import pytest
from pydantic import ValidationError


def test_v15_snapshot_identity_is_scoped_to_user_type_entry_sense_source() -> None:
    assert importlib.util.find_spec("juya_admin_api.infrastructure.db.v15_schema") is not None
    from juya_admin_api.infrastructure.db.v15_schema import metadata

    table = metadata.tables["favorite_snapshot"]
    unique = [
        tuple(col.name for col in c.columns)
        for c in table.constraints
        if c.__class__.__name__ == "UniqueConstraint"
    ]
    assert ("user_id", "entry_type", "entry_id", "sense_id", "source_id") in unique
    assert "normalized_key" not in table.c
    assert "short_reading_progress" in metadata.tables
    assert "favorite_resource_reference" in metadata.tables


def test_v15_entry_type_conversion_preserves_old_contract() -> None:
    assert importlib.util.find_spec("juya_admin_api.modules.content.v15_contracts") is not None
    from juya_admin_api.modules.content.v15_contracts import entry_type, legacy_entry_type

    assert entry_type("VOCABULARY") == "word"
    assert entry_type("PHRASE") == "chunk"
    assert legacy_entry_type("chunk") == "PHRASE"
    with pytest.raises(ValueError):
        entry_type("unknown")


def test_chunk_rejects_display_image_and_preserves_long_text() -> None:
    assert importlib.util.find_spec("juya_admin_api.modules.content.v15_contracts") is not None
    from juya_admin_api.modules.content.v15_contracts import ConfirmedEntry

    payload = dict(
        entry_id="e",
        sense_id="s",
        source_id="src",
        entry_type="chunk",
        english="a" * 8000,
        meaning_zh="含义",
        source_sentence="a" * 9000,
    )
    assert len(ConfirmedEntry.model_validate(payload).english) == 8000
    with pytest.raises(ValidationError):
        ConfirmedEntry.model_validate({**payload, "image_asset_id": "image"})


def test_short_reading_contract_excludes_scene_fields() -> None:
    assert importlib.util.find_spec("juya_admin_api.modules.content.v15_contracts") is not None
    from juya_admin_api.modules.content.v15_contracts import ShortReadingDetail

    payload = dict(
        content_id="r",
        content_version=1,
        revision_id="rev",
        title_en="A",
        title_zh="甲",
        category="生活",
        topic="邻里",
        level="A2",
        safe_intro="简介",
        paragraphs_en=["Hello"],
        translation_zh="你好",
    )
    assert ShortReadingDetail.model_validate(payload).whole_audio is None
    with pytest.raises(ValidationError):
        ShortReadingDetail.model_validate({**payload, "words": []})
