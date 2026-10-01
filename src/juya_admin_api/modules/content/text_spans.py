"""Match only curated dictionary spellings; phrases own overlapping word ranges."""

import re

from juya_admin_api.modules.content.schemas import ClickableSpan, SceneContent


def build_clickable_spans(content: SceneContent) -> SceneContent:
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
    entries.sort(key=lambda item: (not item[1], -len(item[0].english), item[0].entry_id))
    for entry, _phrase in entries:
        if not entry.entry_id or not entry.english.strip():
            continue
        occurrence = 0
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
                occurrence += 1
                locator = (
                    f"sentence:{result.dialogue[indexes[0]].id}:entry:{entry.entry_id}:{occurrence}"
                )
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
        sentence.clickable_spans.sort(key=lambda span: span.start)
    return result
