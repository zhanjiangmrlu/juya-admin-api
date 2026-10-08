"""Match only curated dictionary spellings; phrases own overlapping word ranges."""

import re

from juya_admin_api.modules.content.schemas import ClickableSpan, SceneContent


def build_clickable_spans(content: SceneContent) -> SceneContent:
    # 功能:匹配词典拼写生成可点击范围,优先语块并校验跨句来源。
    # 参数:
    #     content: 结构化场景内容,含标题、对话、词汇、语块和媒体引用。
    # 返回:规范化后的结构化场景内容。
    result = content.model_copy(deep=True)
    starts: list[int] = []
    offset = 0
    for sentence in result.dialogue:
        starts.append(offset)
        offset += len(sentence.english) + 1
        sentence.clickable_spans = []
    text = "\n".join(sentence.english for sentence in result.dialogue)
    occupied: list[tuple[int, int]] = []
    entries = [(entry, True) for entry in result.chunks] + [
        (entry, False) for entry in result.vocabulary
    ]
    # 匿名函数: 使语块优先于词汇、长拼写优先于短拼写,词条标识用于稳定排序。
    # 参数:
    #     item: 词条及是否为语块组成的二元组。
    # 返回: 语块优先级、拼写长度负值及稳定词条标识构成的排序键。
    entries.sort(key=lambda item: (not item[1], -len(item[0].english), item[0].entry_id))
    for entry, _phrase in entries:
        if not entry.entry_id or not entry.english.strip():
            continue
        seen: set[tuple[int, int]] = set()
        for spelling in [entry.english, *entry.variants]:
            parts = spelling.strip().split()
            if not parts:
                continue
            expression = r"(?<!\w)" + r"\s+".join(re.escape(part) for part in parts) + r"(?!\w)"
            for match in re.finditer(expression, text, re.IGNORECASE):
                begin, end = match.span()
                if (begin, end) in seen or any(
                    begin < high and end > low for low, high in occupied
                ):
                    continue
                indexes = [
                    i
                    for i, sentence in enumerate(result.dialogue)
                    if begin < starts[i] + len(sentence.english) and end > starts[i]
                ]
                if len(indexes) > 1 and not all(
                    result.dialogue[i].id in entry.source_sentence_ids for i in indexes
                ):
                    continue
                if entry.source_sentence_ids and not all(
                    result.dialogue[i].id in entry.source_sentence_ids for i in indexes
                ):
                    continue
                seen.add((begin, end))
                occupied.append((begin, end))
                # A source is the sentence context, not its order or character offsets.
                locator = f"sentence:{result.dialogue[indexes[0]].id}:entry:{entry.entry_id}"
                for i in indexes:
                    sentence = result.dialogue[i]
                    sentence.clickable_spans.append(
                        ClickableSpan(
                            start=max(begin - starts[i], 0),
                            end=min(end - starts[i], len(sentence.english)),
                            entry_id=entry.entry_id,
                            entry_version=entry.entry_version,
                            source_locator=locator,
                        )
                    )
    for sentence in result.dialogue:
        # 匿名函数: 按字符起点排列句子内的可点击词条范围。
        # 参数:
        #     span: 对话句子中待排序的可点击词条范围。
        # 返回: 范围起点的字符偏移,从 0 开始。
        sentence.clickable_spans.sort(key=lambda span: span.start)
    return result
