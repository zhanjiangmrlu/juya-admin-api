import json

import httpx
import pytest

from juya_admin_api.integrations.content_ai.aliyun import AliyunContentAI, TextCandidate


@pytest.mark.asyncio
async def test_candidate_request_describes_the_object_contract_expected_by_the_parser():
    def respond(request):
        body = json.loads(request.content)
        assert body["response_format"] == {"type": "json_object"}
        assert body["enable_thinking"] is False
        assert body["max_tokens"] == 32
        system_message, source_message = body["messages"]
        assert system_message["role"] == "system"
        assert source_message["role"] == "user"
        assert json.loads(source_message["content"]) == {"s1": "Hello there."}

        instruction = system_message["content"]
        schema_start = instruction.find("{")
        assert schema_start >= 0, "Provider instructions must include the response object contract"
        example, _ = json.JSONDecoder().raw_decode(instruction[schema_start:])
        assert isinstance(example, dict)
        assert set(example) == {"candidates"}
        assert isinstance(example["candidates"], list)
        assert len(example["candidates"]) == 1
        assert set(example["candidates"][0]) == {"source_id", "english", "chinese"}
        assert all(isinstance(value, str) for value in example["candidates"][0].values())

        return httpx.Response(
            200,
            json={
                "id": "candidate-request",
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                "choices": [
                    {
                        "message": {
                            "content": '{"candidates":[{"source_id":"s1",'
                            '"english":"Hello there.","chinese":"你好。"}]}'
                        }
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await AliyunContentAI(client, "fixture-key").suggest(
            {"s1": "Hello there."}, max_output_tokens=32
        )
    assert result.candidates == (TextCandidate("s1", "Hello there.", "你好。"),)
    assert result.usage == {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9}
    assert result.request_id == "candidate-request"


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (
            '[{"source_id":"s1","english":"Hello there.","chinese":"你好。"}]',
            "candidate response schema",
        ),
        (
            '{"candidates":[{"english":"Hello there.","chinese":"你好。"}]}',
            "candidate response schema",
        ),
        (
            '{"candidates":[{"source_id":"invented","english":"Hello there.","chinese":"你好。"}]}',
            "candidate source mismatch",
        ),
        (
            '{"candidates":[{"source_id":"s1","english":"hello there.","chinese":"你好。"}]}',
            "candidate source mismatch",
        ),
    ],
    ids=["root-array", "missing-source-id", "invented-source", "changed-source-text"],
)
@pytest.mark.asyncio
async def test_candidate_invalid_content_is_rejected_without_coercion(content, reason):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "id": "invalid-candidate-request",
                "usage": {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
                "choices": [{"message": {"content": content}}],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match=reason):
            await AliyunContentAI(client, "fixture-key").suggest({"s1": "Hello there."})


@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 5, "completion_tokens": 4}])
@pytest.mark.asyncio
async def test_candidate_missing_usage_is_rejected_before_returning_candidates(usage):
    def respond(request):
        body = {
            "id": "missing-usage-request",
            "choices": [
                {
                    "message": {
                        "content": '{"candidates":[{"source_id":"s1",'
                        '"english":"Hello there.","chinese":"你好。"}]}'
                    }
                }
            ],
        }
        if usage is not None:
            body["usage"] = usage
        return httpx.Response(200, json=body)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(ValueError, match="candidate usage response invalid"):
            await AliyunContentAI(client, "fixture-key").suggest({"s1": "Hello there."})
