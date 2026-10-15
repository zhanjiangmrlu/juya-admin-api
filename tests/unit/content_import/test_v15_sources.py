import json

import pytest

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


def test_source_conflict_explains_entry_location_and_both_texts_without_changing_input():
    """Keep the failed declaration and exact body evidence for an actionable comparison"""
    dialogue = assign_sentence_ids(
        [
            {"speaker": "A", "english": "One.", "source_locations": ["paragraph-0003"]},
            {"speaker": "B", "english": "Two.", "source_locations": ["paragraph-0005"]},
        ]
    )
    entry = {
        "import_entry_id": "word-one",
        "english": "one",
        "entry_type": "word",
        "source_sentence": "One!",
        "source_locator": {"sentence_number": 2},
        "source_locations": ["table-0001-row-0002-cell-0001"],
    }
    original = json.loads(json.dumps(entry))
    result = resolve_entry_source(entry, dialogue)
    issue = result["issues"][0]
    assert issue["code"] == "SOURCE_TEXT_CONFLICT"
    diagnostic = issue["source_diagnostic"]
    assert diagnostic["entry_text"] == "one"
    assert diagnostic["entry_id"] == "word-one"
    assert diagnostic["declared_text"] == "One!"
    assert diagnostic["declared_locator"] == {"sentence_number": 2}
    assert diagnostic["actual_text"] == "Two."
    assert diagnostic["actual_sentence_number"] == 2
    assert diagnostic["actual_sentence_id"] == "sentence-0002"
    assert diagnostic["actual_speaker"] == "B"
    assert diagnostic["actual_source_locations"] == ["paragraph-0005"]
    assert diagnostic["source_locations"] == entry["source_locations"]
    assert entry == original
    assert not result["resolved"] and result["source_locator"] is None


@pytest.mark.parametrize(
    "locator,expected",
    [
        ({"sentence_number": 1, "typo": True}, ["typo"]),
        (
            {"turn": 1, "speaker": "A", "source_sentence": "One.", "match_mode": "loose"},
            ["match_mode"],
        ),
    ],
)
def test_invalid_locator_diagnostic_names_only_unknown_fields(locator, expected):
    """Unknown keys are named without making invalid declarations valid"""
    result = resolve_entry_source(
        {"source_locator": locator}, assign_sentence_ids([{"english": "One."}])
    )
    assert result["issues"][0]["code"] == "SOURCE_LOCATOR_INVALID"
    assert result["issues"][0]["source_diagnostic"]["unsupported_fields"] == expected
    assert not result["resolved"]


def test_ambiguous_and_out_of_range_diagnostics_do_not_choose_a_sentence():
    """Show duplicate locations or the supplied invalid value without guessing a match"""
    dialogue = assign_sentence_ids([{"english": "Hello."}, {"english": "Hello."}])
    ambiguous = resolve_entry_source({"source_sentence": "Hello."}, dialogue)
    diagnostic = ambiguous["issues"][0]["source_diagnostic"]
    assert diagnostic["matched_sentence_numbers"] == [1, 2]
    assert diagnostic["actual_sentence_id"] is None
    invalid = resolve_entry_source({"source_locator": 9}, dialogue)
    diagnostic = invalid["issues"][0]["source_diagnostic"]
    assert diagnostic["declared_locator"] == 9
    assert diagnostic["actual_sentence_number"] is None


def test_turn_text_and_speaker_conflicts_report_the_rejected_declaration():
    """Separate speaker evidence and compare the actually conflicting legacy text"""
    dialogue = assign_sentence_ids([{"speaker": "A", "english": "One."}])
    entry = {
        "source_sentence": "One.",
        "source_locator": {"turn": 1, "speaker": "A", "source_sentence": "Other."},
    }
    result = resolve_entry_source(entry, dialogue)
    diagnostic = result["issues"][0]["source_diagnostic"]
    assert diagnostic["declared_text"] == "Other."
    assert diagnostic["declared_speaker"] == "A"
    entry["source_locator"]["source_sentence"] = "One."
    entry["source_locator"]["speaker"] = "B"
    result = resolve_entry_source(entry, dialogue)
    assert result["issues"][0]["code"] == "SOURCE_LOCATOR_CONFLICT"
    diagnostic = result["issues"][0]["source_diagnostic"]
    assert diagnostic["declared_speaker"] == "B"
    assert diagnostic["actual_speaker"] == "A"
    assert (
        resolve_entry_source({"source_locator": 0}, dialogue)["issues"][0]["source_diagnostic"][
            "declared_locator"
        ]
        == 0
    )


@pytest.mark.parametrize("serialized", [False, True])
def test_import_turn_evidence_resolves_duplicate_text_to_canonical_sentence(serialized):
    """Resolve a declared dialogue turn only when its speaker and original text agree"""
    dialogue = assign_sentence_ids(
        [{"speaker": "A", "english": "Hello."}, {"speaker": "B", "english": "Hello."}]
    )
    locator = {"turn": 2, "speaker": "B", "source_sentence": "Hello."}
    entry = {
        "source_sentence": "Hello.",
        "source_locator": json.dumps(locator) if serialized else locator,
    }
    original = json.loads(json.dumps(entry))
    result = resolve_entry_source(entry, dialogue)
    assert result["resolved"]
    assert not result["issues"]
    assert result["source_sentence_ids"] == ["sentence-0002"]
    assert result["source_locator"] == {"sentence_id": "sentence-0002"}
    assert entry == original


@pytest.mark.parametrize(
    ("locator", "extra", "code"),
    [
        ({"turn": 0, "speaker": "B", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 3, "speaker": "B", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": True, "speaker": "B", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2.0, "speaker": "B", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2, "speaker": "A", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_CONFLICT"),
        ({"turn": 2, "speaker": "", "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2, "speaker": 2, "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2, "speaker": "B", "source_sentence": "One."}, {}, "SOURCE_TEXT_CONFLICT"),
        ({"turn": 2, "speaker": "B", "source_sentence": ""}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2, "speaker": "B", "source_sentence": 2}, {}, "SOURCE_LOCATOR_INVALID"),
        (
            {"turn": 2, "speaker": "B", "source_sentence": "Two."},
            {"source_sentence": "One."},
            "SOURCE_TEXT_CONFLICT",
        ),
        (
            {"turn": 2, "speaker": "B", "source_sentence": "Two."},
            {"source_sentence_id": "sentence-0001"},
            "SOURCE_LOCATOR_CONFLICT",
        ),
        ({"turn": 2, "speaker": "B"}, {}, "SOURCE_LOCATOR_INVALID"),
        ({"turn": 2, "source_sentence": "Two."}, {}, "SOURCE_LOCATOR_INVALID"),
        (
            {"turn": 2, "speaker": "B", "source_sentence": "Two.", "unknown": "value"},
            {},
            "SOURCE_LOCATOR_INVALID",
        ),
        (
            {"turn": 2, "speaker": "B", "source_sentence": "Two.", "match_mode": "discontinuous"},
            {},
            "SOURCE_LOCATOR_INVALID",
        ),
        (
            {"turn": 2, "speaker": "B", "source_sentence": "Two.", "sentence_number": 2},
            {},
            "SOURCE_LOCATOR_INVALID",
        ),
    ],
)
def test_import_turn_evidence_never_bypasses_source_validation(locator, extra, code):
    """Keep invalid, incomplete, unknown and conflicting import evidence unresolved"""
    dialogue = assign_sentence_ids(
        [{"speaker": "A", "english": "One."}, {"speaker": "B", "english": "Two."}]
    )
    result = resolve_entry_source({"source_locator": locator, **extra}, dialogue)
    assert not result["resolved"]
    assert result["source_sentence_ids"] == []
    assert result["source_locator"] is None
    assert result["issues"][0]["code"] == code
