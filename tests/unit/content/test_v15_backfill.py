from juya_admin_api.modules.content.v15_backfill import build_snapshot


def legacy(**changes):
    return {
        "legacy_source_id": 7,
        "entry_stable_id": "saved-entry",
        "scene_id": "saved-scene",
        "sentence_snapshot": "Original sentence",
        **changes,
    }


def test_unrecoverable_fields_are_explicit_and_never_fake_current_revision_or_position():
    result = build_snapshot(legacy())
    assert result["revision_id"] is None and result["saved_content_version"] is None
    assert result["source_locator"] is None
    assert result["english"] == result["meaning_zh"] == ""
    assert result["legacy_incomplete"]
    assert {
        "source_id",
        "sense_id",
        "source_locator",
        "revision_id",
        "saved_content_version",
        "meaning_zh",
    } <= set(result["missing_fields"])
    assert "image_asset_id" not in result and "pronunciation_target_id" not in result


def test_fixed_saved_snapshot_and_exact_archived_revision_are_used_without_english_merging():
    result = build_snapshot(
        legacy(
            revision_id="rev",
            version_no=8,
            entry_snapshot={
                "english": "bank",
                "chinese": "河岸",
                "phonetic": "bæŋk",
                "explanation": "Saved",
                "sense_id": "sense-old",
            },
            content_snapshot={"dialogue": [{"id": "line-5"}]},
            source_locator="rev|sentence:line-5:entry:saved-entry",
        )
    )
    assert result["english"] == "bank" and result["meaning_zh"] == "河岸"
    assert result["saved_content_version"] == 8 and result["revision_id"] == "rev"
    assert result["source_locator"] == {"sentence_id": "line-5", "entry_id": "saved-entry"}
    another = build_snapshot(legacy(legacy_source_id=8, entry_snapshot={"english": "bank"}))
    assert result["source_id"] != another["source_id"]
    assert result["sense_id"] == "sense-old"
