"""Recover display evidence from immutable originals without changing issue applicability."""

from copy import deepcopy
from typing import Any

from juya_admin_api.modules.content_import.parser import materialize_docx_entries, speaker_text
from juya_admin_api.modules.content_import.sources import assign_sentence_ids, resolve_entry_source


def recover_source_diagnostics(
    values: list[dict[str, Any]], snapshot: dict[str, Any]
) -> list[dict[str, Any]]:
    """Attach only uniquely attributable original diagnoses, leaving old records untouched"""
    result = deepcopy(values)
    # Repeated block IDs across multiple documents cannot establish a unique original location.
    if snapshot.get("documents"):
        return result
    blocks = snapshot.get("blocks", [])
    if not isinstance(blocks, list):
        return result
    english = [b for b in blocks if isinstance(b, dict) and b.get("role") == "english"]
    if not english or any(not isinstance(b.get("text"), str) for b in english):
        return result
    dialogue = assign_sentence_ids(
        [
            {
                "speaker": speaker_text(b["text"].strip())[0],
                "english": speaker_text(b["text"].strip())[1],
                "source_locations": [b["id"]],
            }
            for b in english
            if isinstance(b.get("id"), str)
        ]
    )
    if len(dialogue) != len(english):
        return result
    article = {
        "attachments": deepcopy(snapshot.get("attachments", [])),
        "dialogue": dialogue,
        "issues": [],
    }
    materialize_docx_entries(article, "dialogue")
    recovered = article["issues"]
    workbook = snapshot.get("workbook", {})
    for row in workbook.get("entry_rows", []) if isinstance(workbook, dict) else []:
        if not isinstance(row, dict) or not isinstance(row.get("values"), dict):
            continue
        entry: dict[str, Any] = {
            str(k).strip(): str(v).strip() if v is not None else ""
            for k, v in row["values"].items()
        }
        entry["source_locations"] = (
            [f"原始词条表 · 第 {row['row_no']} 行"] if "row_no" in row else []
        )
        for issue in resolve_entry_source(entry, dialogue)["issues"]:
            recovered.append(issue)
    for original in result:
        if original.get("source_diagnostic") or not original.get("code", "").startswith("SOURCE_"):
            continue
        candidates = [
            issue
            for issue in recovered
            if issue["code"] == original["code"]
            and issue["message"] == original.get("message")
            and issue.get("source_diagnostic")
            and (
                not original.get("source_locations")
                or original["source_locations"] == issue.get("source_locations")
            )
        ]
        if len(candidates) == 1:
            original["source_diagnostic"] = candidates[0]["source_diagnostic"]
    return result
