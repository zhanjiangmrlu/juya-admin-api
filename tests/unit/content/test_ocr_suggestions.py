from juya_admin_api.modules.ocr_suggestions.service import suggest_groups


def test_position_labels_and_low_confidence_are_reviewable_without_inventing_translations():
    result = suggest_groups(
        {
            "blocks": [
                {"text": "Useful Chunks", "location": {"top": 300, "left": 5}, "confidence": 0.99},
                {"text": "A Better Way", "location": {"top": 10, "left": 5}, "confidence": 0.98},
                {"text": "Dialogue", "location": {"top": 50, "left": 5}},
                {"text": "A: Hello", "location": {"top": 60, "left": 5}, "confidence": 0.7},
                {"text": "Key Vocabulary", "location": {"top": 200, "left": 5}},
                {"text": "hello", "location": {"top": 220, "left": 5}, "confidence": 0.95},
                {"text": "good morning", "location": {"top": 320, "left": 5}, "confidence": 0.95},
            ]
        },
        "dialogue",
    )
    assert [g.field for g in result.groups] == ["title", "dialogue", "vocabulary", "chunks"]
    assert result.groups[0].line_ids == [1]
    assert result.groups[1].line_ids == [3]
    assert result.groups[2].line_ids == [5]
    assert result.groups[3].line_ids == [6]
    assert result.lines[3].low_confidence
    assert result.lines[3].location["top"] == 60


def test_vocabulary_template_and_missing_positions_do_not_guess_dialogue():
    result = suggest_groups({"blocks": [{"text": "apple"}, {"text": "banana"}]}, "vocabulary")
    assert result.groups[1].line_ids == []
    assert result.unassigned_line_ids == [0, 1]
    assert all(line.low_confidence for line in result.lines)


def test_side_by_side_sections_use_horizontal_position_as_well_as_reading_order():
    result = suggest_groups(
        {
            "blocks": [
                {"text": "Vocabulary", "location": {"top": 200, "left": 10}},
                {"text": "Useful Chunks", "location": {"top": 210, "left": 600}},
                {"text": "apple", "location": {"top": 240, "left": 10}},
                {"text": "a cup of", "location": {"top": 240, "left": 600}},
            ]
        },
        "dialogue",
    )
    assert result.groups[2].line_ids == [2]
    assert result.groups[3].line_ids == [3]
