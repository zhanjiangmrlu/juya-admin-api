"""Explicit word-table rows for an administrator-selected existing source draft."""

import hashlib
import json
from collections import Counter
from collections.abc import Mapping
from pathlib import PurePosixPath
from typing import Any

from juya_admin_api.modules.content.schemas import SceneContent, SceneEntry
from juya_admin_api.modules.content_import.parser import (
    ImportLimits,
    canonical_hash,
    read_sheet,
    safe_path,
)
from juya_admin_api.modules.content_import.sources import resolve_entry_source
from juya_admin_api.shared.errors import AppError

TABLE = "词汇表.xlsx"


def vocabulary_rows(
    files: Mapping[str, bytes], kind: str, limits: ImportLimits
) -> list[dict[str, Any]]:
    """Read literal rows with explicit IDs and bounded, declared paths."""
    if TABLE not in files:
        return []
    if kind == "short_reading":
        raise AppError("IMPORT_READING_ENTRIES_FORBIDDEN", "纯阅读短文不接收词汇表", 422)
    rows = read_sheet(files[TABLE], limits)
    if len(rows) > limits.max_items:
        raise AppError("IMPORT_LIMIT_EXCEEDED", "单次词汇表最多1000条", 422)
    keys = Counter(
        row.get("import_entry_id") or row.get("entry_id") or row.get("条目ID", "") for row in rows
    )
    result = []
    for raw in rows:
        row: dict[str, Any] = {
            "row_no": raw["row_no"],
            "import_entry_id": raw.get("import_entry_id")
            or raw.get("entry_id")
            or raw.get("条目ID", ""),
            "entry_type": raw.get("entry_type", ""),
            "english": raw.get("english", ""),
            "meaning_zh": raw.get("meaning_zh") or raw.get("chinese", ""),
            "explanation": raw.get("explanation") or raw.get("explanation_zh", ""),
            "phonetic": raw.get("phonetic", ""),
            "source_sentence": raw.get("source_sentence", ""),
            "source_locator": raw.get("source_locator", ""),
            "source_content_id": raw.get("source_content_id", ""),
            "pronunciation_text": raw.get("pronunciation_text", ""),
            "reading_variant": raw.get("reading_variant", ""),
            "meaning_change": raw.get("meaning_change", ""),
            "issues": [],
        }

        def issue(
            code: str, message: str, severity: str = "ERROR", target: dict[str, Any] = row
        ) -> None:
            target["issues"].append({"code": code, "message": message, "severity": severity})

        if not row["import_entry_id"] or keys[row["import_entry_id"]] > 1:
            issue("IMPORT_DUPLICATE_ENTRY_ID", "同来源条目导入ID为空或重复,请修正或排除")
        if raw["formula_columns"]:
            issue("IMPORT_FORMULA_INVALID", "公式不计算,请使用明确字面值")
        if row["entry_type"] not in {"word", "chunk"}:
            issue("ENTRY_TYPE_INVALID", "条目类型须为word或chunk")
        if not row["english"] or not row["meaning_zh"]:
            issue("ENTRY_TEXT_REQUIRED", "英文及本篇中文释义必需")
        if len(row["import_entry_id"]) > 64:
            issue("IMPORT_ENTRY_ID_INVALID", "条目导入ID过长")
        for field, extensions in (
            ("image_path", {".png", ".jpg", ".jpeg", ".webp"}),
            ("pronunciation_path", {".mp3", ".wav", ".m4a", ".aac"}),
        ):
            row[field] = raw.get(field, "")
            if not row[field]:
                continue
            if field == "image_path" and row["entry_type"] == "chunk":
                issue("CHUNK_IMAGE_INVALID", "语块不接收展示图片,请清空或排除")
            try:
                path = safe_path(row[field], limits)
            except AppError:
                issue("IMPORT_PATH_INVALID", "素材必须使用包内安全相对路径")
                continue
            row[field] = path
            if path not in files or PurePosixPath(path).suffix.casefold() not in extensions:
                issue("IMPORT_RESOURCE_INVALID", "声明的素材路径不存在或类型不符")
            else:
                row[field + "_hash"] = hashlib.sha256(files[path]).hexdigest()
        row["input_hash"] = canonical_hash(
            {key: value for key, value in row.items() if key not in {"issues", "row_no"}}
        )
        result.append(row)
    return result


def resolve_material_row(
    raw: dict[str, Any], body: SceneContent, source_content_id: str
) -> dict[str, Any]:
    """Resolve sources only within the selected draft without guessing repeated sentences."""
    row = {**raw, "issues": [*raw["issues"]]}
    if row.get("source_content_id") and row["source_content_id"] != source_content_id:
        row["issues"].append(
            {
                "code": "MATERIAL_SOURCE_CONTENT_CONFLICT",
                "message": "表格来源内容ID与后台选定目标不同",
                "severity": "ERROR",
            }
        )
    if body.template_type == "dialogue":
        source = resolve_entry_source(row, [sentence.model_dump() for sentence in body.dialogue])
        row.update(
            {
                key: source[key]
                for key in ("source_sentence", "source_sentence_ids", "source_locator")
            }
        )
        row["issues"].extend(source["issues"])
    else:
        locator = row.get("source_locator")
        if isinstance(locator, str) and locator:
            try:
                value = json.loads(locator)
            except ValueError:
                value = {"reference": locator}
            row["source_locator"] = value if isinstance(value, dict) else {"reference": locator}
        else:
            row["source_locator"] = None
        row["source_sentence_ids"] = []
        if not row["source_sentence"]:
            row["issues"].append(
                {
                    "code": "SOURCE_NOT_FOUND",
                    "message": "缺少本篇来源原句,请在草稿校对中补充",
                    "severity": "DRAFT",
                }
            )
    return row


def material_entry(row: dict[str, Any], previous: SceneEntry | None = None) -> SceneEntry:
    """Build candidate text while preserving stable identities and audio configuration."""
    result = previous.model_copy(deep=True) if previous else SceneEntry()
    result.english = row["english"]
    result.chinese = row["meaning_zh"]
    result.phonetic = row["phonetic"]
    result.explanation = row["explanation"]
    result.source_sentence = row["source_sentence"]
    result.source_sentence_ids = row.get("source_sentence_ids", [])
    result.source_locator = row.get("source_locator") or None
    result.pronunciation_text = row["pronunciation_text"]
    result.reading_variant = row["reading_variant"]
    result.meaning_change = row.get("meaning_change") or None
    result.confirmed = False
    return result
