"""Resolve explicit or uniquely matching DOCX sources without guessing duplicate sentences."""

import json
from typing import Any


def assign_sentence_ids(dialogue: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = [
        dict(row) | {"id": row.get("id") or f"sentence-{index:04d}"}
        for index, row in enumerate(dialogue, start=1)
    ]
    ids = [row["id"] for row in result]
    if any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(
        set(ids)
    ):
        raise ValueError("dialogue sentence identities must be distinct strings")
    return result


def resolve_entry_source(entry: dict[str, Any], dialogue: list[dict[str, Any]]) -> dict[str, Any]:
    original = entry.get("source_sentence", "")
    result: dict[str, Any] = {
        "resolved": False,
        "source_sentence": original,
        "source_sentence_ids": [],
        "source_locator": None,
        "issues": [],
    }

    def reject(code: str, message: str) -> dict[str, Any]:
        result["issues"] = [
            {"code": code, "severity": "DRAFT", "field": "source_locator", "message": message}
        ]
        return result

    if not isinstance(original, str):
        return reject("SOURCE_TEXT_INVALID", "来源原句必须是完整文本")
    if not dialogue:
        return reject("SOURCE_NOT_FOUND", "本篇没有可定位的正文句子")
    ids = [row.get("id") for row in dialogue]
    if any(not isinstance(identity, str) or not identity for identity in ids) or len(ids) != len(
        set(ids)
    ):
        return reject("SOURCE_IDS_INVALID", "正文缺少稳定句子标识")
    references: list[int] = []

    def identity_reference(value: Any) -> bool:
        if not isinstance(value, str) or value not in ids:
            return False
        references.append(ids.index(value))
        return True

    def position_reference(value: Any) -> bool:
        if isinstance(value, str) and value.isdecimal():
            value = int(value)
        if type(value) is not int or not 1 <= value <= len(dialogue):
            return False
        references.append(value - 1)
        return True

    explicit = entry.get("source_sentence_id")
    if explicit not in (None, "") and not identity_reference(explicit):
        return reject("SOURCE_LOCATOR_INVALID", "显式来源句子标识不属于本篇")
    locator = entry.get("source_locator")
    if isinstance(locator, str) and locator.strip():
        locator = locator.strip()
        if not locator.isdecimal():
            try:
                locator = json.loads(locator)
            except (TypeError, ValueError):
                return reject("SOURCE_LOCATOR_INVALID", "来源定位须为序号或JSON对象")
    if locator not in (None, ""):
        if isinstance(locator, dict):
            if not locator or set(locator) - {"sentence_id", "sentence_number", "paragraph_number"}:
                return reject("SOURCE_LOCATOR_INVALID", "来源定位包含不支持的字段")
            for key, value in locator.items():
                valid = (
                    identity_reference(value) if key == "sentence_id" else position_reference(value)
                )
                if not valid:
                    return reject("SOURCE_LOCATOR_INVALID", "来源定位不属于本篇正文")
        elif not position_reference(locator):
            return reject("SOURCE_LOCATOR_INVALID", "来源序号必须为本篇1起始整数")
    if references and len(set(references)) != 1:
        return reject("SOURCE_LOCATOR_CONFLICT", "显式来源位置互相冲突,请人工校对")
    if references:
        index = references[0]
        if original and dialogue[index].get("english") != original:
            return reject("SOURCE_TEXT_CONFLICT", "来源原句与指定正文位置不一致")
    else:
        matches = [
            index
            for index, row in enumerate(dialogue)
            if original and row.get("english") == original
        ]
        if not matches:
            return reject("SOURCE_NOT_FOUND", "来源原句未与本篇正文精确匹配")
        if len(matches) != 1:
            return reject("SOURCE_AMBIGUOUS", "来源原句重复,请明确位置并人工校对")
        index = matches[0]
    result.update(
        resolved=True,
        source_sentence=dialogue[index]["english"],
        source_sentence_ids=[ids[index]],
        source_locator={"sentence_id": ids[index]},
    )
    return result
