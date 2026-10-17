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
    """Verify entry source declarations against this article's identified dialogue sentences"""
    original = entry.get("source_sentence", "")
    result: dict[str, Any] = {
        "resolved": False,
        "source_sentence": original,
        "source_sentence_ids": [],
        "source_locator": None,
        "issues": [],
    }
    references: list[int] = []
    locator = entry.get("source_locator")
    turn_evidence: dict[str, Any] | None = None

    def reject(code: str, message: str) -> dict[str, Any]:
        """Attach the rejected declaration and only unambiguous body evidence for this entry"""
        actual_index = references[0] if references and len(set(references)) == 1 else None
        actual = dialogue[actual_index] if actual_index is not None else {}
        declared = original
        if turn_evidence and turn_evidence["source_sentence"] != actual.get("english"):
            declared = turn_evidence["source_sentence"]
        known = {"sentence_id", "sentence_number", "paragraph_number"}
        if isinstance(locator, dict) and set(locator) & {"turn", "speaker", "source_sentence"}:
            known = {"turn", "speaker", "source_sentence"}
        diagnostic = {
            "entry_id": entry.get("import_entry_id") or entry.get("entry_id"),
            "entry_type": entry.get("entry_type"),
            "entry_text": entry.get("english"),
            "source_locations": list(entry.get("source_locations", [])),
            "declared_locator": entry.get("source_locator")
            if "source_locator" in entry
            else (
                {"sentence_id": entry["source_sentence_id"]}
                if entry.get("source_sentence_id")
                else None
            ),
            "declared_text": declared if isinstance(declared, str) else None,
            "declared_speaker": locator.get("speaker")
            if isinstance(locator, dict) and isinstance(locator.get("speaker"), str)
            else None,
            "actual_sentence_id": actual.get("id"),
            "actual_sentence_number": actual_index + 1 if actual_index is not None else None,
            "actual_text": actual.get("english"),
            "actual_speaker": actual.get("speaker"),
            "actual_source_locations": list(actual.get("source_locations", [])),
            "unsupported_fields": sorted(set(locator) - known) if isinstance(locator, dict) else [],
            "matched_sentence_numbers": [
                index + 1
                for index, row in enumerate(dialogue)
                if original and row.get("english") == original
            ],
        }
        result["issues"] = [
            {
                "code": code,
                "severity": "DRAFT",
                "field": "source_locator",
                "message": message,
                "source_diagnostic": diagnostic,
            }
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
    if isinstance(locator, str) and locator.strip():
        locator = locator.strip()
        if not locator.isdecimal():
            try:
                locator = json.loads(locator)
            except (TypeError, ValueError):
                return reject("SOURCE_LOCATOR_INVALID", "来源定位须为序号或JSON对象")
    if locator not in (None, ""):
        if isinstance(locator, dict):
            # The older import shape declares all three facts, not an unverified turn alias.
            if set(locator) == {"turn", "speaker", "source_sentence"}:
                if any(
                    not isinstance(locator[key], str) or not locator[key]
                    for key in ("speaker", "source_sentence")
                ):
                    return reject("SOURCE_LOCATOR_INVALID", "来源说话者和原句必须为非空文本")
                if not position_reference(locator["turn"]):
                    return reject("SOURCE_LOCATOR_INVALID", "来源定位不属于本篇正文")
                turn_evidence = locator
            elif not locator or set(locator) - {
                "sentence_id",
                "sentence_number",
                "paragraph_number",
            }:
                return reject("SOURCE_LOCATOR_INVALID", "来源定位包含不支持的字段")
            else:
                for key, value in locator.items():
                    valid = (
                        identity_reference(value)
                        if key == "sentence_id"
                        else position_reference(value)
                    )
                    if not valid:
                        return reject("SOURCE_LOCATOR_INVALID", "来源定位不属于本篇正文")
        elif not position_reference(locator):
            return reject("SOURCE_LOCATOR_INVALID", "来源序号必须为本篇1起始整数")
    if references and len(set(references)) != 1:
        return reject("SOURCE_LOCATOR_CONFLICT", "显式来源位置互相冲突,请人工校对")
    if references:
        index = references[0]
        if turn_evidence is not None:
            if turn_evidence["speaker"] != dialogue[index].get("speaker"):
                return reject("SOURCE_LOCATOR_CONFLICT", "来源说话者与指定正文位置不一致")
            if turn_evidence["source_sentence"] != dialogue[index].get("english"):
                return reject("SOURCE_TEXT_CONFLICT", "来源原句与指定正文位置不一致")
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
