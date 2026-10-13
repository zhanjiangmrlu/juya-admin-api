"""ASR必须基于真实词/段时间与全文顺序建立稳定句子对应,禁止均分或猜测。"""

import copy
import json
from pathlib import Path

import httpx
import pytest

from juya_admin_api.integrations.asr.aliyun import AliyunASR, ASRCandidate, ASRSegment, ASRWord
from juya_admin_api.modules.content.schemas import SceneContent
from juya_admin_api.modules.content.v15_candidates import dependency


def response(segments, text=None, channel=0):
    # 功能:构造官方channel0转录shape,保留多token词组及原毫秒时间。
    return {
        "transcripts": [
            {
                "channel_id": channel,
                "text": text if text is not None else " ".join(s["text"] for s in segments),
                "sentences": segments,
            }
        ]
    }


def segment(text, start, end, words=None):
    # 功能:构造实际段时间证据,无words时不制造词级边界。
    result = {"text": text, "begin_time": start, "end_time": end}
    if words is not None:
        result["words"] = [{"text": t, "begin_time": a, "end_time": b} for t, a, b in words]
    return result


async def parsed(body):
    # 功能:通过真实HTTP解析适配器消费受控官方响应,不调用付费服务。
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    ) as http:
        return await AliyunASR(http, "test", result_hosts=("result.test",)).parse_result(
            "https://result.test/asr.json"
        )


@pytest.mark.asyncio
async def test_parser_keeps_text_word_time_and_channel_evidence():
    # 功能:识别器必须保留原句/原词及实际毫秒证据,不仅返回匿名边界。
    candidate = await parsed(
        response([segment("Hello there.", 100, 800, [("Hello", 100, 350), ("there", 400, 800)])])
    )
    assert candidate.text == "Hello there."
    assert len(candidate.segments) == 1
    assert candidate.segments[0].text == "Hello there." and candidate.segments[0].channel_id == 0
    assert [(w.text, w.start_ms, w.end_ms) for w in candidate.segments[0].words] == [
        ("Hello", 100, 350),
        ("there", 400, 800),
    ]
    assert candidate.issues == ()


def test_asr_dependency_preserves_paid_result_across_unrelated_edits():
    # 功能:标题/译文/词条及编辑号变化不重复花费ASR,正文/源音频/边界变化必须失效。
    content = SceneContent.model_validate(
        {
            "dialogue": [{"id": "s", "english": "Hello", "chinese": "你好"}],
            "audio": {"target_id": "t", "version_id": "v", "asset_id": "a", "duration_ms": 1000},
        }
    )
    changed = copy.deepcopy(content)
    changed.title_en = "New title"
    changed.dialogue[0].chinese = "您好"
    assert dependency(content, 1, "ASR_BOUNDARY", "sha") == dependency(
        changed, 9, "ASR_BOUNDARY", "sha"
    )
    assert dependency(content, 1, "AI_LEXICON") != dependency(changed, 9, "AI_LEXICON")
    changed.dialogue[0].start_ms, changed.dialogue[0].end_ms = 50, 900
    assert dependency(content, 1, "ASR_BOUNDARY", "sha") != dependency(
        changed, 9, "ASR_BOUNDARY", "sha"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources,segments,expected",
    [
        ([("s1", "Hello there.")], [segment("Hello there!", 20, 900)], [("s1", 20, 900)]),
        (
            [("s1", "Hello there.")],
            [segment("Hello", 20, 300), segment("there.", 400, 900)],
            [("s1", 20, 900)],
        ),
        (
            [("s1", "Hello."), ("s2", "There!")],
            [segment("Hello. There!", 20, 900, [("Hello", 20, 300), ("There", 400, 900)])],
            [("s1", 20, 300), ("s2", 400, 900)],
        ),
        (
            [("s1", "Go go."), ("s2", "Go go!")],
            [
                segment(
                    "Go go go go.",
                    10,
                    800,
                    [("Go", 10, 150), ("go", 200, 350), ("go", 400, 550), ("go", 600, 800)],
                )
            ],
            [("s1", 10, 350), ("s2", 400, 800)],
        ),
        (
            [("s1", "Dr. Lee\u2019s in the U.S.")],
            [segment("dr lee's in the US", 10, 900)],
            [("s1", 10, 900)],
        ),
        (
            [("s1", "This is good."), ("s2", "Yes.")],
            [
                segment(
                    "This is good. Yes.",
                    10,
                    900,
                    [("This is", 10, 300), ("good", 350, 600), ("Yes", 700, 900)],
                )
            ],
            [("s1", 10, 600), ("s2", 700, 900)],
        ),
    ],
)
async def test_exact_coverage_derives_only_real_monotone_sentence_boundaries(
    sources, segments, expected
):
    # 功能:拆段/合句/重复句及标点缩写用完整token顺序对应,时间完全来自真实证据。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    candidate = await parsed(response(segments))
    result = align_sentences(sources, candidate, 1000)
    assert result["alignment_status"] == "ALIGNED" and result["issues"] == []
    assert [(r["sentence_id"], r["start_ms"], r["end_ms"]) for r in result["alignment"]] == expected
    assert all(r["match_score"] == 1.0 for r in result["alignment"])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources,body",
    [
        ([("a", "Hello."), ("b", "There.")], response([segment("Hello there.", 10, 900)])),
        (
            [("a", "Hello."), ("b", "There.")],
            response([segment("Hello there.", 10, 900, [("Hello there", 10, 900)])]),
        ),
        ([("a", "Hello there.")], response([segment("Hello", 10, 300)])),
        ([("a", "Hello there.")], response([segment("There Hello", 10, 900)])),
        ([("a", "Hello there.")], response([segment("Hello there there", 10, 900)])),
        (
            [("a", "Hello there.")],
            response([segment("Hello there", 10, 900, [("Hello", 10, 300)])]),
        ),
        (
            [("a", "Hello there.")],
            response([segment("Hello", 10, 500), segment("there", 400, 900)]),
        ),
        ([("a", "Hello there.")], response([segment("Hello there", 10, 1100)])),
        ([("a", "Hello there.")], response([segment("Hello there", -1, 900)])),
        ([("a", "Hello there.")], response([segment("Hello there", True, 900)])),
        (
            [("a", "Hello there.")],
            response([segment("Hello there", 10, 900, [("Hello", 10, 500), ("there", 400, 900)])]),
        ),
        (
            [("a", "Hello there.")],
            response([segment("Hello there", 10, 900, [("Hello", 5, 300), ("there", 400, 900)])]),
        ),
        ([("a", "Hello there.")], response([segment("Hello there", 10, 900)], channel=1)),
        ([("a", "Hello there.")], response([segment("Hello there", 10, 900)], channel=None)),
        ([("a", "Hello there.")], response([segment("Hello there", 10, 900)], text="Hello")),
        (
            [("a", "Hello."), ("a", "There.")],
            response([segment("Hello", 10, 300), segment("there", 400, 900)]),
        ),
    ],
)
async def test_missing_ambiguous_or_contradictory_evidence_is_never_partially_applied(
    sources, body
):
    # 功能:没有可靠真实时间/全文覆盖/唯一稳定ID时只报异常,不返回可自动应用的残缺序列。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    result = align_sentences(sources, await parsed(body), 1000)
    assert result["alignment_status"] == "NEEDS_REVIEW"
    assert result["alignment"] == [] and result["issues"]
    assert all("code" in r and "message" in r and "sentence_id" in r for r in result["issues"])


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [{"begin_time": 900, "end_time": 950}, "invalid"])
async def test_parser_does_not_silently_drop_invalid_trailing_segments(invalid):
    # 功能:有效段后出现无文本/未知结构的段也必须进入复核,不能删掉异常后宣称全量成功。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    body = response([segment("Hello", 10, 800)])
    body["transcripts"][0]["sentences"].append(invalid)
    candidate = await parsed(body)
    result = align_sentences([("s", "Hello")], candidate, 1000)
    assert result["alignment_status"] == "NEEDS_REVIEW" and result["alignment"] == []
    assert candidate.issues


def test_live_transcript_zero_duration_function_words_preserve_original_sentence_bounds():
    # 功能:真实转录8100/11240ms零时长a仅绑定邻接真实词组,四句边界仍取原供应商时间。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    fixture = json.loads(
        (Path(__file__).parents[2] / "fixtures/asr/zero-duration-live-transcript.json").read_text(
            encoding="utf-8"
        )
    )
    candidate = ASRCandidate(
        fixture["text"],
        (),
        segments=tuple(
            ASRSegment(
                row["text"],
                row["start_ms"],
                row["end_ms"],
                tuple(ASRWord(**word) for word in row["words"]),
                row["channel_id"],
            )
            for row in fixture["segments"]
        ),
    )
    result = align_sentences(
        [(row["sentence_id"], row["english"]) for row in fixture["sources"]],
        candidate,
        fixture["duration_ms"],
    )
    assert result["alignment_status"] == "ALIGNED" and result["issues"] == []
    assert [(row["start_ms"], row["end_ms"]) for row in result["alignment"]] == [
        (400, 4400),
        (5380, 8740),
        (10920, 12520),
        (13640, 17080),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "words",
    [
        [("by", 10, 100), ("a", 100, 100), ("giant", 100, 300)],
        [("a", 10, 10), ("giant", 10, 300)],
        [("by", 10, 300), ("a", 300, 300)],
        [("by", 10, 100), ("a", 100, 100), ("very", 100, 100), ("giant", 100, 300)],
        [("by", 10, 100), ("a", 100, 100), ("giant", 200, 300)],
        [("by", 10, 100), ("a", 200, 200), ("giant", 200, 300)],
    ],
)
async def test_zero_word_uses_only_touching_positive_word_outer_bounds(words):
    # 功能:段内零词或同点零词链仅与正时长邻词合并,不增加任何词级时间。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    text = " ".join(row[0] for row in words)
    candidate = await parsed(response([segment(text, 10, 300, words)]))
    result = align_sentences([("s", text)], candidate, 1000)
    assert result["alignment_status"] == "ALIGNED"
    assert [(row["start_ms"], row["end_ms"]) for row in result["alignment"]] == [(10, 300)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sources,segments,code",
    [
        (
            [("left", "by"), ("right", "a giant")],
            [
                segment(
                    "by a giant", 10, 300, [("by", 10, 100), ("a", 100, 100), ("giant", 100, 300)]
                )
            ],
            "ASR_BOUNDARY_AMBIGUOUS",
        ),
        (
            [("left", "by a"), ("right", "giant")],
            [
                segment(
                    "by a giant", 10, 300, [("by", 10, 100), ("a", 100, 100), ("giant", 100, 300)]
                )
            ],
            "ASR_BOUNDARY_AMBIGUOUS",
        ),
        (
            [("left", "a"), ("right", "giant")],
            [segment("a giant", 10, 300, [("a", 10, 10), ("giant", 10, 300)])],
            "ASR_BOUNDARY_AMBIGUOUS",
        ),
        (
            [("left", "by"), ("right", "a")],
            [segment("by a", 10, 300, [("by", 10, 300), ("a", 300, 300)])],
            "ASR_BOUNDARY_AMBIGUOUS",
        ),
        (
            [("s", "by a giant")],
            [
                segment(
                    "by a giant", 10, 300, [("by", 10, 100), ("a", 150, 150), ("giant", 200, 300)]
                )
            ],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "a giant")],
            [segment("a giant", 10, 300, [("a", 10, 10), ("giant", 20, 300)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "by a")],
            [segment("by", 10, 100), segment("a", 100, 300, [("a", 100, 100)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "a the")],
            [segment("a the", 10, 300, [("a", 100, 100), ("the", 100, 100)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "by a giant")],
            [segment("by a giant", 10, 300, [("by", 10, 100), ("a", 90, 90), ("giant", 100, 300)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "by a giant")],
            [
                segment(
                    "by a giant", 10, 300, [("by", 10, 100), ("a", 100, 100), ("giant", 99, 300)]
                )
            ],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "a giant")],
            [segment("a giant", 10, 300, [("a", 301, 301), ("giant", 301, 350)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "by a giant")],
            [
                segment(
                    "by a giant", 10, 300, [("by", 10, 100), ("a", 200, 100), ("giant", 200, 300)]
                )
            ],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "a giant")],
            [segment("a giant", 10, 300, [("a", True, True), ("giant", 10, 300)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "a giant")],
            [segment("a giant", 10, 300, [("a", None, None), ("giant", 10, 300)])],
            "ASR_WORD_TIME_INVALID",
        ),
        (
            [("s", "by a giant")],
            [segment("by a giant", 10, 300, [("by", 10, 100), ("giant", 100, 300)])],
            "ASR_WORD_TEXT_CONFLICT",
        ),
    ],
)
async def test_zero_word_never_supplies_an_independent_or_guessed_sentence_boundary(
    sources, segments, code
):
    # 功能:合并组内句界/无邻接锚点/全零/跨段锚点/非法时间或漏词都保持整批拒绝。
    from juya_admin_api.modules.content.asr_alignment import align_sentences

    result = align_sentences(sources, await parsed(response(segments)), 1000)
    assert result["alignment_status"] == "NEEDS_REVIEW" and result["alignment"] == []
    assert {row["code"] for row in result["issues"]} == {code}
