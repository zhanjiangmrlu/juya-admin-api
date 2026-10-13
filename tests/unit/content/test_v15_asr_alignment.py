"""ASR必须基于真实词/段时间与全文顺序建立稳定句子对应,禁止均分或猜测。"""

import copy

import httpx
import pytest

from juya_admin_api.integrations.asr.aliyun import AliyunASR
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
