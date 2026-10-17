from juya_admin_api.modules.content_production.effective_issues import effective_history


def test_added_audio_rechecks_current_facts_instead_of_retaining_missing_original_diagnostic():
    issue = {"code": "AUDIO_MISSING", "field": "audio_path", "severity": "DRAFT"}
    assert effective_history([issue], {"audio": {"version_id": "fixed"}}, None, [], imported=True)[
        0
    ]["resolved"]
    assert not effective_history([issue], {}, None, [], imported=True)[0]["resolved"]


def test_pairing_ambiguity_and_legacy_full_snapshot_acceptance_fail_closed():
    issue = {"id": "pair", "code": "BILINGUAL_PAIRING_REQUIRED", "field": "dialogue"}
    content = {"dialogue": [{"english": "Hello", "chinese": "你好", "speaker": "A"}]}
    assert not effective_history(
        [issue],
        content,
        "original",
        [dict(issue_id="pair", source_hash="original", candidate_hash="legacy")],
        imported=True,
    )[0]["resolved"]


def test_display_metadata_defaults_do_not_change_immutable_issue_identity():
    from juya_admin_api.modules.content_production.effective_issues import issue_id
    from juya_admin_api.modules.content_production.projection import issues

    historical = {"code": "CATEGORY_REQUIRED"}
    displayed = issues([historical])[0]
    assert displayed.location == ["category"]
    assert displayed.id == issue_id(historical)
