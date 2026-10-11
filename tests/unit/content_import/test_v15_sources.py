from juya_admin_api.modules.content_import.sources import assign_sentence_ids, resolve_entry_source


def test_minimum_source_sentence_forms_stable_canonical_location():
    dialogue = assign_sentence_ids([{"english": "A bank by the river."}, {"english": "Hello."}])
    assert [row["id"] for row in dialogue] == ["sentence-0001", "sentence-0002"]
    result = resolve_entry_source({"source_sentence": "A bank by the river."}, dialogue)
    assert result["resolved"]
    assert result["source_sentence_ids"] == ["sentence-0001"]
    assert result["source_locator"] == {"sentence_id": "sentence-0001"}


def test_ambiguous_source_is_not_guessed_and_locator_may_disambiguate():
    dialogue = assign_sentence_ids([{"english": "Hello."}, {"english": "Hello."}])
    assert not resolve_entry_source({"source_sentence": "Hello."}, dialogue)["resolved"]
    result = resolve_entry_source(
        {"source_sentence": "Hello.", "source_locator": '{"sentence_number":2}'}, dialogue
    )
    assert result["source_sentence_ids"] == ["sentence-0002"]


def test_conflicting_explicit_locations_or_text_are_rejected():
    dialogue = assign_sentence_ids([{"english": "One."}, {"english": "Two."}])
    for entry in (
        {"source_sentence": "One.", "source_locator": 2},
        {"source_locator": {"sentence_id": "sentence-0001", "sentence_number": 2}},
        {"source_sentence_id": "sentence-0001", "source_locator": "2"},
        {"source_locator": "0"},
        {"source_locator": "{broken"},
    ):
        result = resolve_entry_source(entry, dialogue)
        assert not result["resolved"]
        assert result["source_sentence_ids"] == []
        assert result["issues"]


def test_locator_aliases_resolve_same_identity_without_changing_original_text():
    dialogue = assign_sentence_ids([{"english": "One."}, {"english": "Two."}])
    for locator in (2, "2", {"paragraph_number": 2}, {"sentence_id": "sentence-0002"}):
        result = resolve_entry_source({"source_locator": locator}, dialogue)
        assert result["resolved"]
        assert result["source_sentence"] == "Two."
        assert result["source_locator"] == {"sentence_id": "sentence-0002"}
