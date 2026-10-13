"""仅用全文精确词序及可证明的真实时间对齐原句,不插值/均分/改正文。"""

import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from juya_admin_api.integrations.asr.aliyun import ASRCandidate


def tokens(text: str) -> tuple[str, ...]:
    # 功能:归一大小写/字形/标点与点分缩写,保留真实词序和缩写/数字内容。
    value = (
        unicodedata.normalize("NFKC", text).casefold().replace("\u2019", "'").replace("\u2018", "'")
    )
    value = re.sub(r"\b(?:[a-z]\.){2,}", lambda match: match[0].replace(".", ""), value)
    return tuple(re.findall(r"\d+(?:\.\d+)*|[^\W\d_]+(?:'[^\W\d_]+)*", value))


@dataclass(frozen=True)
class Evidence:
    tokens: tuple[str, ...]
    start_ms: int
    end_ms: int


def valid_time(start: int | None, end: int | None, duration: int) -> bool:
    # 功能:验证提供者给出的真实毫秒区间,拒绝bool/缺失/逆序/越界。
    return type(start) is int and type(end) is int and 0 <= start < end <= duration


def review(sources: Sequence[tuple[str, str]], code: str, message: str) -> dict[str, Any]:
    # 功能:异常仅返回完整诊断,不暴露可自动应用的部分时间序列。
    return {
        "alignment_status": "NEEDS_REVIEW",
        "alignment": [],
        "issues": [
            {"sentence_id": identity, "code": code, "message": message}
            for identity in dict.fromkeys(s[0] for s in sources)
        ]
        or [{"sentence_id": "", "code": code, "message": message}],
    }


def align_sentences(
    sources: Sequence[tuple[str, str]], candidate: ASRCandidate, duration_ms: int
) -> dict[str, Any]:
    # 功能:按稳定句ID顺序与全量ASR词序一一对应,仅取真实段/词边界,任何歧义则整批拒绝。
    if (
        not sources
        or len({s[0] for s in sources}) != len(sources)
        or any(not s[0] for s in sources)
    ):
        return review(sources, "SOURCE_ID_INVALID", "原句稳定标识缺失或重复,请核对正文")
    source_tokens = [tokens(s[1]) for s in sources]
    if any(not row for row in source_tokens):
        return review(sources, "SOURCE_TEXT_INVALID", "原句缺少可匹配英文,请核对正文")
    if type(duration_ms) is not int or duration_ms <= 0:
        return review(sources, "AUDIO_DURATION_REQUIRED", "源音频缺少真实解码时长")
    if candidate.issues:
        return review(sources, candidate.issues[0], "ASR证据异常,请核对原始转录/声道/时间")
    if not candidate.segments:
        return review(sources, "ASR_SEGMENTS_MISSING", "ASR缺少带原文的真实段/词时间证据")
    groups: list[Evidence] = []
    segment_tokens: list[str] = []
    previous_end = 0
    for segment in candidate.segments:
        if segment.channel_id != 0 or type(segment.channel_id) is not int:
            return review(sources, "ASR_CHANNEL_INVALID", "ASR仅允许明确的channel0有序时间证据")
        start, end = segment.start_ms, segment.end_ms
        if not valid_time(start, end, duration_ms) or start is None or end is None:
            return review(sources, "ASR_TIME_INVALID", "ASR段时间缺失、逆序或越出源音频")
        if start < previous_end:
            return review(sources, "ASR_TIME_OVERLAP", "ASR段时间重叠或乱序,请人工校正")
        previous_end = end
        text_tokens = tokens(segment.text)
        if not text_tokens:
            return review(sources, "ASR_TEXT_INVALID", "ASR段缺少可匹配英文")
        segment_tokens.extend(text_tokens)
        if not segment.words:
            groups.append(Evidence(text_tokens, start, end))
            continue
        word_tokens: list[str] = []
        word_end = start
        for word in segment.words:
            first, last = word.start_ms, word.end_ms
            if (
                not valid_time(first, last, duration_ms)
                or first is None
                or last is None
                or first < word_end
                or first < start
                or last > end
            ):
                return review(sources, "ASR_WORD_TIME_INVALID", "ASR词时间缺失、越界或重叠")
            word_end = last
            lexical = tokens(word.text)
            if not lexical:
                return review(sources, "ASR_WORD_TEXT_INVALID", "ASR词时间不对应英文词组")
            word_tokens.extend(lexical)
            groups.append(Evidence(lexical, first, last))
        if tuple(word_tokens) != text_tokens:
            return review(sources, "ASR_WORD_TEXT_CONFLICT", "ASR词级证据未完整覆盖段文本")
    recognized = tuple(segment_tokens)
    if tokens(candidate.text) != recognized:
        return review(
            sources, "ASR_TEXT_EVIDENCE_CONFLICT", "转录全文与段证据不一致,禁止丢弃异常后对齐"
        )
    if tuple(token for row in source_tokens for token in row) != recognized:
        return review(sources, "ASR_TEXT_MISMATCH", "识别文本漏词、重复或顺序变化,请核对正文和音频")
    starts: dict[int, int] = {}
    ends: dict[int, int] = {}
    offset = 0
    for group in groups:
        starts[offset] = group.start_ms
        offset += len(group.tokens)
        ends[offset] = group.end_ms
    alignment = []
    offset = 0
    for (identity, _), lexical in zip(sources, source_tokens, strict=True):
        end_offset = offset + len(lexical)
        if offset not in starts or end_offset not in ends:
            return review(
                sources,
                "ASR_BOUNDARY_AMBIGUOUS",
                "原句分界落在无独立词时间的ASR段/词组内,请补词时间或人工校正",
            )
        alignment.append(
            {
                "sentence_id": identity,
                "start_ms": starts[offset],
                "end_ms": ends[end_offset],
                "match_score": 1.0,
            }
        )
        offset = end_offset
    return {"alignment_status": "ALIGNED", "alignment": alignment, "issues": []}
