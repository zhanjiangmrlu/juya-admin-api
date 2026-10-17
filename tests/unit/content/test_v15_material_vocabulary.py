from io import BytesIO

from openpyxl import Workbook

from juya_admin_api.modules.content.schemas import DialogueSentence, SceneContent
from juya_admin_api.modules.content.v15_material_vocabulary import (
    resolve_material_row,
    vocabulary_rows,
)
from juya_admin_api.modules.content_import.parser import ImportLimits


def sheet(rows):
    book = Workbook()
    book.active.append(
        ["import_entry_id", "entry_type", "english", "meaning_zh", "source_sentence", "image_path"]
    )
    for row in rows:
        book.active.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_table_uses_declared_ids_and_paths_and_marks_duplicate_keys_and_missing_images():
    rows = vocabulary_rows(
        {
            "词汇表.xlsx": sheet(
                [
                    ["word", "word", "bank", "河岸", "By the bank.", "images/bank.png"],
                    ["word", "word", "bank", "银行", "At the bank.", ""],
                    [
                        "chunk",
                        "chunk",
                        "by the bank",
                        "在河岸旁",
                        "By the bank.",
                        "images/bank.png",
                    ],
                ]
            ),
            "images/bank.png": b"file",
        },
        "dialogue",
        ImportLimits(),
    )
    assert all(
        any(issue["code"] == "IMPORT_DUPLICATE_ENTRY_ID" for issue in row["issues"])
        for row in rows[:2]
    )
    assert any(issue["code"] == "CHUNK_IMAGE_INVALID" for issue in rows[2]["issues"])
    assert rows[1]["image_path"] == ""
    assert rows[0]["image_path_hash"]


def test_source_target_is_explicit_and_repeated_sentences_do_not_guess_locations():
    body = SceneContent(
        schema_version=2,
        dialogue=[
            DialogueSentence(id="one", english="By the bank.", chinese="在河岸旁。"),
            DialogueSentence(id="two", english="By the bank.", chinese="在河岸旁。"),
        ],
    )
    raw = vocabulary_rows(
        {"词汇表.xlsx": sheet([["word", "word", "bank", "河岸", "By the bank.", ""]])},
        "dialogue",
        ImportLimits(),
    )[0]
    result = resolve_material_row(raw, body, "SOURCE")
    assert any(issue["code"] == "SOURCE_AMBIGUOUS" for issue in result["issues"])
    raw["source_locator"] = "2"
    result = resolve_material_row(raw, body, "SOURCE")
    assert result["source_locator"] == {"sentence_id": "two"}
    raw["source_content_id"] = "OTHER"
    result = resolve_material_row(raw, body, "SOURCE")
    assert any(issue["code"] == "MATERIAL_SOURCE_CONTENT_CONFLICT" for issue in result["issues"])
