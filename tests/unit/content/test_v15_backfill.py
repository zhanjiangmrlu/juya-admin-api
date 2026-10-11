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
    assert result["image_asset_id"] is None and result["pronunciation_target_id"] is None
    assert result["resource_version"] is None


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


def test_saved_fixed_resources_win_and_exact_entry_archive_recovers_without_text_guessing():
    saved = build_snapshot(
        legacy(
            entry_snapshot={
                "image_asset_id": "saved-image",
                "pronunciation_target_id": "saved-target",
                "resource_version": "saved-version",
            }
        )
    )
    assert (
        saved["image_asset_id"],
        saved["pronunciation_target_id"],
        saved["resource_version"],
    ) == ("saved-image", "saved-target", "saved-version")
    archived = build_snapshot(
        legacy(
            revision_id="archive",
            version_no=1,
            content_snapshot={
                "vocabulary": [
                    {"entry_id": "other", "english": "bank", "icon_asset_id": "wrong"},
                    {
                        "entry_id": "saved-entry",
                        "icon_asset_id": "exact-image",
                        "pronunciation_target_id": "exact-target",
                        "resource_version": "exact-version",
                    },
                ]
            },
        )
    )
    assert archived["image_asset_id"] == "exact-image"
    assert archived["pronunciation_target_id"] == "exact-target"
    unknown = build_snapshot(
        legacy(
            content_snapshot={
                "vocabulary": [{"entry_id": "saved-entry", "icon_asset_id": "unproven-revision"}]
            }
        )
    )
    assert unknown["image_asset_id"] is None


def test_partial_saved_audio_pair_never_mixes_with_archive_or_guesses_resource_version():
    result = build_snapshot(
        legacy(
            entry_snapshot={"pronunciation_target_id": "old"},
            revision_id="r",
            version_no=2,
            content_snapshot={
                "vocabulary": [
                    {
                        "entry_id": "saved-entry",
                        "pronunciation_target_id": "different",
                        "resource_version": "new",
                    }
                ]
            },
        )
    )
    assert result["pronunciation_target_id"] == "old"
    assert result["resource_version"] is None
    assert "resource_version" in result["missing_fields"]


def test_nested_saved_pronunciation_and_archived_scene_audio_version_are_fixed():
    result = build_snapshot(
        legacy(
            entry_snapshot={"pronunciation": {"target_id": "saved", "resource_version": "v-old"}}
        )
    )
    assert result["pronunciation_target_id"] == "saved" and result["resource_version"] == "v-old"
    archived = build_snapshot(
        legacy(
            revision_id="r",
            version_no=2,
            content_snapshot={
                "vocabulary": [
                    {
                        "entry_id": "saved-entry",
                        "pronunciation_target_id": "controlled",
                        "audio_version_id": "upload-old",
                    }
                ]
            },
        )
    )
    assert archived["resource_version"] == "upload-old"


def test_legacy_saved_upload_binding_cannot_be_replaced_by_modern_archive_target():
    result = build_snapshot(
        legacy(
            entry_snapshot={
                "audio_target_id": "legacy-upload",
                "audio_version_id": "legacy-version",
            },
            revision_id="r",
            version_no=2,
            content_snapshot={
                "vocabulary": [
                    {
                        "entry_id": "saved-entry",
                        "pronunciation_target_id": "different-controlled",
                        "audio_version_id": "different-version",
                    }
                ]
            },
        )
    )
    assert result["pronunciation_target_id"] == "legacy-upload"
    assert result["resource_version"] == "legacy-version"
