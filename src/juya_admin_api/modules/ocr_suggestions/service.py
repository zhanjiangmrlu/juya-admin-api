"""Propose four authoring groups from existing OCR labels and line positions."""

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

SuggestionField = Literal["title", "dialogue", "vocabulary", "chunks"]


class SuggestedLine(BaseModel):
    id: int
    text: str
    location: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    low_confidence: bool
    paragraph: dict[str, Any] = Field(default_factory=dict)


class SuggestedGroup(BaseModel):
    field: SuggestionField
    label: str
    line_ids: list[int] = Field(default_factory=list)
    reason: str


class OcrSuggestions(BaseModel):
    template_type: str
    lines: list[SuggestedLine]
    groups: list[SuggestedGroup]
    unassigned_line_ids: list[int]
    low_confidence_threshold: float = 0.85


def _number(value: object) -> float | None:
    # 功能:读取有限数值,拒绝布尔值、无穷值和非数值输入。
    # 参数:
    #     value: OCR 置信度或位置字段的原始输入,仅接受有限的整数或浮点数。
    # 返回:合法有限数值转换后的浮点数;非法输入为 None。
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def suggest_groups(content: dict[str, Any], template_type: str) -> OcrSuggestions:
    # 功能:按 OCR 标题标签和原图位置生成标题、对话、词汇及语块分组建议。
    # 参数:
    #     content: OCR 候选内容字典,包含完整识别文本和带位置、置信度的识别块。
    #     template_type: 内容模板类型,区分 dialogue 对话与 vocabulary 词汇。
    # 返回:OCR 行、四组采纳建议和未分配行标识。
    raw = content.get("blocks", [])
    if not isinstance(raw, list) or not raw:
        raw = [{"text": line} for line in str(content.get("text", "")).splitlines()]
    lines: list[SuggestedLine] = []
    for index, block in enumerate(raw):
        if not isinstance(block, dict) or not str(block.get("text", "")).strip():
            continue
        position = block.get("location")
        confidence = _number(block.get("confidence"))
        if confidence is not None and not 0 <= confidence <= 1:
            confidence = None
        lines.append(
            SuggestedLine(
                id=index,
                text=str(block["text"]),
                location={
                    key: number
                    for key, value in (position.items() if isinstance(position, dict) else [])
                    if key in {"top", "left", "width", "height"}
                    and (number := _number(value)) is not None
                },
                confidence=confidence,
                low_confidence=confidence is None or confidence < 0.85,
                paragraph=block["paragraph"] if isinstance(block.get("paragraph"), dict) else {},
            )
        )
    labels: list[tuple[SuggestionField, str]] = [
        ("title", "标题"),
        ("dialogue", "说话者/对话"),
        ("vocabulary", "重点词汇"),
        ("chunks", "Useful Chunks"),
    ]
    groups = [
        SuggestedGroup(
            field=field, label=label, reason="按固定标题标签和原图位置提出建议,需人工校对"
        )
        for field, label in labels
    ]
    by_field = {group.field: group for group in groups}
    headings: dict[SuggestionField, set[str]] = {
        "title": {"title", "标题"},
        "dialogue": {"dialogue", "conversation", "对话", "说话者对话"},
        "vocabulary": {"vocabulary", "keyvocabulary", "keywords", "重点词汇", "核心词汇", "词汇"},
        "chunks": {"usefulchunks", "chunks", "常用语块", "实用语块", "语块"},
    }
    # 匿名函数: 按原图从上到下、从左到右排列 OCR 行,缺位置时排后。
    # 参数:
    #     line: 包含识别文本和原图位置的 OCR 建议行。
    # 返回: 顶部位置、左侧位置和原始行标识组成的排序键。
    ordered = sorted(
        lines,
        key=lambda line: (
            line.location.get("top", math.inf),
            line.location.get("left", math.inf),
            line.id,
        ),
    )
    current: SuggestionField | None = None
    assigned: set[int] = set()
    first_heading_top: float | None = None
    positioned_headings: list[tuple[SuggestionField, SuggestedLine]] = []
    for line in ordered:
        normalized = re.sub(r"[\s:\uff1a/\uff0f&-]", "", line.text).casefold()
        heading = next((field for field, labels in headings.items() if normalized in labels), None)
        if heading:
            current = heading
            if "top" in line.location and "left" in line.location:
                positioned_headings.append((heading, line))
            if first_heading_top is None:
                first_heading_top = line.location.get("top")
            continue
        proposed = current
        if "top" in line.location and "left" in line.location:
            eligible = [
                pair
                for pair in positioned_headings
                if pair[1].location["top"] <= line.location["top"]
            ]
            if eligible:
                # 匿名函数: 计算候选分组标题与当前 OCR 行的位置距离,供选择最近标题。
                # 参数:
                #     pair: 候选内容分组字段与对应标题识别行组成的二元组。
                # 返回: 纵向间距与横向距离之和,越小表示标题越接近当前行。
                proposed = min(
                    eligible,
                    key=lambda pair: (
                        line.location["top"]
                        - pair[1].location["top"]
                        + abs(line.location["left"] - pair[1].location["left"])
                    ),
                )[0]
        if proposed and (proposed != "dialogue" or template_type == "dialogue"):
            by_field[proposed].line_ids.append(line.id)
            assigned.add(line.id)
    # Without labels, background text is not guessed as teaching content.
    if first_heading_top is not None:
        above = [
            line
            for line in ordered
            if "top" in line.location
            and line.location["top"] < first_heading_top
            and line.id not in assigned
        ]
        if above:
            by_field["title"].line_ids = [above[0].id]
            by_field["title"].reason = "首个固定标签上方的首行,可能是标题,需核对背景噪声"
            assigned.add(above[0].id)
    return OcrSuggestions(
        template_type=template_type,
        lines=lines,
        groups=groups,
        unassigned_line_ids=[line.id for line in ordered if line.id not in assigned],
    )
